"""
Modul: test_agent.py
Tanggung Jawab: menjalankan TEST_AFK_DEVICE secara TERPISAH dari lifecycle
session/order CARRERA-HUB.

TEST INI SENGAJA TIDAK memakai session_agent.SESSIONS, watchdog session,
SYNC_SESSIONS, database order, atau auto-relaunch. Tujuannya murni mengukur
berapa banyak package Roblox yang bisa bertahan AFK di Steal An Egg.
"""
import asyncio
import time

from core.logger import log
from core.deeplink import get_place_intent, get_lobby_intent
from core.launcher import launch_and_wait, activate_freeform, get_pid_quick
from core import process_manager
from core import session_agent
from core.join_verifier import has_recent_disconnect_signal

# Current Steal An Egg (and Collect Rare Pets), verified Sep 2026.
STEAL_AN_EGG_PLACE_ID = "107778070777162"
TEST_TIMEOUT_SECONDS = 45
STATUS_INTERVAL_SECONDS = 15

# test_id -> test state
TESTS = {}
_sender = None


def register_sender(callback) -> None:
    global _sender
    _sender = callback


async def _send(payload: dict) -> None:
    if _sender is None:
        log.warning("TEST_AGENT: sender belum tersedia, status test tidak terkirim: %s", payload.get("type"))
        return
    try:
        await _sender(payload)
    except Exception:
        log.warning("TEST_AGENT: gagal mengirim status test.", exc_info=True)


async def _snapshot(test: dict) -> dict:
    now = time.time()
    packages = {}
    alive = 0
    active = [
        (pkg, info)
        for pkg, info in test["packages"].items()
        if info.get("state") in {"STARTING", "ALIVE"}
    ]
    pid_results = await asyncio.gather(
        *(asyncio.to_thread(get_pid_quick, pkg) for pkg, _ in active),
        return_exceptions=True,
    )
    pid_map = {
        pkg: (pid if isinstance(pid, str) else "")
        for (pkg, _), pid in zip(active, pid_results)
    }

    for pkg, info in test["packages"].items():
        state = info.get("state", "STARTING")
        pid = pid_map.get(pkg, info.get("pid", ""))
        if pid:
            if state not in {"FAILED", "DEAD"}:
                state = "ALIVE"
                alive += 1
        elif state == "ALIVE":
            state = "DEAD"
        info["state"] = state
        info["pid"] = pid or info.get("pid", "-") or "-"
        packages[pkg] = {
            "state": state,
            "pid": info["pid"],
            "freeform": bool(info.get("freeform")),
            "launch_ok": bool(info.get("launch_ok")),
        }

    test["alive"] = alive
    test["peak_alive"] = max(test.get("peak_alive", 0), alive)
    return {
        "type": "TEST_AFK_STATUS",
        "device_id": test["device_id"],
        "test_id": test["test_id"],
        "elapsed_seconds": int(max(0, now - test.get("afk_started_at", test["started_at"]))),
        "alive": alive,
        "total": len(test["packages"]),
        "peak_alive": test.get("peak_alive", alive),
        "packages": packages,
    }


async def _activate_test_freeform_and_restore(test: dict, pkg: str) -> None:
    """Mirror the normal CARRERA Freeform step, but keep state isolated from SESSIONS."""
    try:
        ok, task_id = await asyncio.to_thread(activate_freeform, pkg)
    except Exception:
        ok, task_id = False, None
        log.warning(f"TEST_AGENT: Freeform exception {pkg}.", exc_info=True)

    info = test["packages"][pkg]
    info["freeform"] = bool(ok)
    info["task_id"] = task_id

    if not ok:
        return

    # Same idea as session_agent sibling restore, but ONLY for packages in this test.
    for sibling, sibling_info in test["packages"].items():
        if sibling == pkg or sibling_info.get("state") != "ALIVE":
            continue
        try:
            await asyncio.to_thread(process_manager.restore_foreground, sibling)
        except Exception:
            log.warning(f"TEST_AGENT: gagal restore sibling {sibling} setelah Freeform {pkg}.", exc_info=True)


async def _launch_one(test: dict, pkg: str) -> None:
    """Full staged CARRERA launch flow, without creating a joki session."""
    info = test["packages"][pkg]
    if test["stop_event"].is_set():
        info["state"] = "CANCELLED"
        return

    # 1) LOBBY: launch NORMAL, Smart Wait, baru Freeform.
    try:
        lobby_ok = await asyncio.to_thread(
            launch_and_wait, pkg, get_lobby_intent(), TEST_TIMEOUT_SECONDS,
            False, True,
        )
    except Exception as exc:
        info["state"] = "FAILED"
        info["reason"] = f"LOBBY_EXCEPTION: {exc}"
        log.error(f"TEST_AGENT: lobby exception {pkg}.", exc_info=True)
        return

    if test["stop_event"].is_set():
        pid = get_pid_quick(pkg)
        if pid:
            await asyncio.to_thread(process_manager.kill_pid_direct, pid)
        info["state"] = "CANCELLED"
        info["pid"] = "-"
        return

    if not lobby_ok:
        info["state"] = "FAILED"
        info["reason"] = "LOBBY_LAUNCH_FAILED"
        return

    info["launch_ok"] = True
    info["pid"] = get_pid_quick(pkg) or "-"
    await _activate_test_freeform_and_restore(test, pkg)

    if not get_pid_quick(pkg):
        info["state"] = "DEAD"
        info["reason"] = "PROCESS_DIED_AFTER_LOBBY"
        return

    # 2) TARGET: launch normally, require join signal, then Freeform re-verify.
    try:
        join_status, join_reason = await asyncio.to_thread(
            launch_and_wait,
            pkg,
            get_place_intent(STEAL_AN_EGG_PLACE_ID),
            TEST_TIMEOUT_SECONDS,
            True,
            True,
        )
    except Exception as exc:
        info["state"] = "FAILED"
        info["reason"] = f"JOIN_EXCEPTION: {exc}"
        log.error(f"TEST_AGENT: target join exception {pkg}.", exc_info=True)
        return

    if test["stop_event"].is_set():
        pid = get_pid_quick(pkg)
        if pid:
            await asyncio.to_thread(process_manager.kill_pid_direct, pid)
        info["state"] = "CANCELLED"
        info["pid"] = "-"
        return

    if join_status == "FAILED":
        info["state"] = "FAILED"
        info["reason"] = f"JOIN_FAILED:{join_reason}"
        return

    if join_status == "UNCERTAIN":
        # Same short grace-check concept as normal CARRERA, but no SESSIONS mutation.
        grace_start = time.strftime('%m-%d %H:%M:%S.000')
        await asyncio.sleep(2)
        if not get_pid_quick(pkg):
            info["state"] = "FAILED"
            info["reason"] = "PROCESS_DIED_DURING_VERIFY"
            return
        try:
            has_failure, failure_code = await asyncio.to_thread(
                has_recent_disconnect_signal, pkg, grace_start,
            )
        except Exception:
            has_failure, failure_code = False, None
        if has_failure:
            info["state"] = "FAILED"
            info["reason"] = f"JOIN_ERROR_SIGNAL_{failure_code}"
            return

    await _activate_test_freeform_and_restore(test, pkg)
    info["pid"] = get_pid_quick(pkg) or "-"
    info["state"] = "ALIVE" if info["pid"] != "-" else "DEAD"
    if info["state"] == "DEAD":
        info["reason"] = "PROCESS_DIED_AFTER_TARGET"


async def _monitor_loop(test_id: str) -> None:
    test = TESTS[test_id]

    # TEST AFK WAJIB launch SEQUENTIAL, mengikuti pola launch normal CARRERA:
    # 1 package -> tunggu Roblox aktif -> aktifkan Freeform -> baru lanjut
    # package berikutnya. Android 12/Freeform butuh task package sebelumnya
    # sudah stabil sebelum package lain dibuka. Jangan gunakan asyncio.gather
    # untuk launch package di sini.
    await _send(await _snapshot(test))

    for pkg in test["packages"]:
        if test["stop_event"].is_set():
            break

        task = asyncio.create_task(_launch_one(test, pkg))
        test["launch_tasks"] = [task]
        try:
            await task
        except Exception:
            # _launch_one sudah menangani exception internal, tetapi guard ini
            # memastikan satu package gagal tidak mematikan seluruh test.
            log.error(f"TEST_AGENT: launch task exception {pkg}.", exc_info=True)
            test["packages"][pkg]["state"] = "FAILED"
            test["packages"][pkg]["reason"] = "LAUNCH_TASK_EXCEPTION"
        finally:
            test["launch_tasks"] = []

        if test["stop_event"].is_set():
            break

        # Update setelah SETIAP package selesai launch + Freeform. Jadi staff
        # bisa melihat package pertama sudah ALIVE/FREEFORM sebelum package
        # kedua dibuka.
        await _send(await _snapshot(test))

    if test["stop_event"].is_set():
        return

    # Semua package sudah dicoba. Mulai fase AFK monitoring murni. Package
    # yang mati TIDAK direlaunch.
    while not test["stop_event"].is_set():
        await asyncio.sleep(STATUS_INTERVAL_SECONDS)
        if test["stop_event"].is_set():
            break
        await _send(await _snapshot(test))


async def handle_test_afk(msg: dict, local_device_id: str) -> dict:
    """Entry point TEST_AFK_DEVICE dari bot.

    Satu device hanya boleh punya satu test aktif. Package yang sedang dipakai
    session joki juga ditolak agar test tidak mengganggu order berjalan.
    """
    test_id = str(msg.get("test_id", "")).strip()
    packages = msg.get("packages")
    if not test_id or not isinstance(packages, list):
        return {
            "type": "TEST_AFK_RESULT", "command": "TEST_AFK_DEVICE",
            "device_id": local_device_id, "test_id": test_id,
            "ok": False, "phase": "ERROR", "reason": "MISSING_FIELDS",
        }

    packages = list(dict.fromkeys(str(p).strip() for p in packages if str(p).strip()))
    if not packages:
        return {
            "type": "TEST_AFK_RESULT", "command": "TEST_AFK_DEVICE",
            "device_id": local_device_id, "test_id": test_id,
            "ok": False, "phase": "ERROR", "reason": "NO_PACKAGES",
        }

    # Jangan pernah mengambil alih package yang sedang menjadi session/order.
    busy = [pkg for pkg in packages if pkg in session_agent.SESSIONS]
    if busy:
        return {
            "type": "TEST_AFK_RESULT", "command": "TEST_AFK_DEVICE",
            "device_id": local_device_id, "test_id": test_id,
            "ok": False, "phase": "ERROR",
            "reason": "PACKAGE_BUSY", "packages": busy,
        }

    # Jangan overwrite test lama di device yang sama.
    for existing in TESTS.values():
        if existing.get("device_id") == local_device_id and not existing["stop_event"].is_set():
            return {
                "type": "TEST_AFK_RESULT", "command": "TEST_AFK_DEVICE",
                "device_id": local_device_id, "test_id": test_id,
                "ok": False, "phase": "ERROR", "reason": "TEST_ALREADY_RUNNING",
                "active_test_id": existing.get("test_id"),
            }

    test = {
        "test_id": test_id,
        "device_id": local_device_id,
        "started_at": time.time(),
        "packages": {
            pkg: {
                "state": "STARTING", "pid": "-", "launch_ok": False,
                "freeform": False, "reason": "",
            }
            for pkg in packages
        },
        "alive": 0,
        "peak_alive": 0,
        "stop_event": asyncio.Event(),
        "launch_tasks": [],
    }
    TESTS[test_id] = test
    test["task"] = asyncio.create_task(_monitor_loop(test_id))

    return {
        "type": "TEST_AFK_RESULT", "command": "TEST_AFK_DEVICE",
        "device_id": local_device_id, "test_id": test_id,
        "ok": True, "phase": "STARTED", "total": len(packages),
        "place_id": STEAL_AN_EGG_PLACE_ID,
    }


async def handle_stop_test_afk(msg: dict, local_device_id: str) -> dict:
    test_id = str(msg.get("test_id", "")).strip()
    test = TESTS.get(test_id)
    if not test or test.get("device_id") != local_device_id:
        return {
            "type": "TEST_AFK_RESULT", "command": "STOP_TEST_AFK",
            "device_id": local_device_id, "test_id": test_id,
            "ok": False, "phase": "ERROR", "reason": "TEST_NOT_FOUND",
        }

    test["stop_event"].set()

    # Tunggu launch tasks selesai/cancel-aware sebentar, lalu kill PID test saja.
    launch_tasks = test.get("launch_tasks", [])
    for task in launch_tasks:
        if not task.done():
            # _launch_one checks stop_event and cleans up after launch returns.
            pass

    await asyncio.sleep(0.2)

    # Snapshot TERAKHIR sebelum kill. Jangan mengandalkan heartbeat/status
    # 15 detik sebelumnya untuk menentukan berapa package yang benar-benar
    # masih bertahan saat operator menekan Stop.
    alive_before_stop = 0
    for pkg, info in test["packages"].items():
        pid = await asyncio.to_thread(get_pid_quick, pkg)
        if pid and info.get("state") not in {"FAILED", "CANCELLED"}:
            info["state"] = "ALIVE"
            info["pid"] = pid
            alive_before_stop += 1
        elif info.get("state") == "ALIVE":
            info["state"] = "DEAD"
            info["pid"] = "-"

    test["alive"] = alive_before_stop
    test["peak_alive"] = max(test.get("peak_alive", 0), alive_before_stop)

    # Simpan kondisi TERAKHIR sebelum proses test dimatikan, supaya hasil
    # tetap bisa menunjukkan siapa yang ALIVE/DEAD/FAILED, bukan semuanya
    # berubah menjadi STOPPED setelah cleanup.
    packages = {
        pkg: {
            "state": info.get("state", "UNKNOWN"),
            "launch_ok": bool(info.get("launch_ok")),
            "freeform": bool(info.get("freeform")),
            "reason": info.get("reason", ""),
        }
        for pkg, info in test["packages"].items()
    }

    for pkg, info in test["packages"].items():
        pid = info.get("pid") or ""
        if pid:
            killed = await asyncio.to_thread(process_manager.kill_pid_direct, pid)
            info["stop_killed"] = bool(killed)
        info["state"] = "STOPPED"
        info["pid"] = "-"

    elapsed = int(max(0, time.time() - test["started_at"]))
    peak_alive = test.get("peak_alive", 0)

    result = {
        "type": "TEST_AFK_RESULT", "command": "STOP_TEST_AFK",
        "device_id": local_device_id, "test_id": test_id,
        "ok": True, "phase": "STOPPED", "elapsed_seconds": elapsed,
        "survivors": alive_before_stop, "total": len(packages),
        "peak_alive": peak_alive, "packages": packages,
    }
    TESTS.pop(test_id, None)
    return result
