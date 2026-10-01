"""
Modul: session_agent.py
Tanggung Jawab (PHASE 4 + 4.5 + 6/7 + 8): eksekusi command sesi joki secara
HEADLESS -- tanpa operator perlu membuka menu interaktif CARRERA-HUB.
START_SESSION (Phase 4), username scanner berkala (Phase 4.5, lihat D2),
STOP_SESSION (Phase 6/7), dan SYNC_SESSIONS (Phase 8) sudah diimplementasi.

DESAIN PENTING:
- Modul ini SENGAJA TIDAK memakai core/monitor.py atau core/recovery_manager.py
  (keduanya singleton yang didesain untuk SATU sesi dashboard interaktif yang
  dijalankan operator dari menu). Reuse langsung di sini berisiko bentrok state
  kalau operator suatu saat juga membuka menu manual untuk package lain.
  Sebagai gantinya, modul ini punya watchdog headless SENDIRI yang ringan,
  khusus untuk package yang dikontrol lewat agent/Discord Bot.
- launcher.launch_and_wait() itu blocking (subprocess + sleep) -- semua
  pemanggilannya di sini WAJIB lewat asyncio.to_thread supaya tidak memblokir
  event loop agent_client.py (yang juga harus tetap kirim heartbeat & terima
  command untuk package lain secara bersamaan).
- Modul ini TIDAK PERNAH menyentuh proses/monitoring yang dikelola menu.py.

PHASE 8 -- prinsip SYNC_SESSIONS (lihat reconcile_sync() untuk detail):
- Bot (DB SQLite, persistent) adalah sumber kebenaran untuk "SEHARUSNYA
  session apa saja yang masih berjalan/dalam proses" (business logic/order).
- Device (modul ini) adalah sumber kebenaran untuk "APA YANG BENERAN JALAN
  secara fisik" -- semua keputusan kill/adopt SELALU berdasarkan PID nyata
  lewat process_manager.get_pid(), tidak pernah menebak dari state lama.
- Device TIDAK PERNAH auto-launch package dari hasil sync (itu cuma boleh
  lewat START_SESSION eksplisit) -- kalau proses yang diharapkan ternyata
  sudah mati, device cukup lapor apa adanya, keputusan lanjut ada di bot.
"""
import asyncio
import random
import time

import datetime

from core.logger import log
from core.deeplink import get_intent_url, get_lobby_intent
from core.launcher import (
    get_pid_quick, activate_freeform,
    launch_normal, wait_for_launch_signal,
)
from core.join_verifier import has_recent_disconnect_signal
from core.error_detector import start_error_detector, has_event, get_event
from core import process_manager
from core import username_scanner

DEFAULT_TIMEOUT_SECONDS = 45
WATCHDOG_INTERVAL_SECONDS = 15
# PID hilang harus terkonfirmasi 2x berturut-turut (jeda singkat) sebelum
# recovery -- menghindari recovery palsu (yang akan kill proses hidup) akibat
# satu kali `pidof` meleset.
WATCHDOG_CONFIRM_DELAY_SECONDS = 3
WATCHDOG_STUCK_RECOVERY_TICKS = 2
# Restart manual: tunggu PID BARU muncul setelah relaunch (detik).
RESTART_WAIT_NEW_PID_SECONDS = 25
# Pastikan PID lama benar-benar mati: berapa kali kill ulang kalau masih ada.
KILL_VERIFY_MAX_ROUNDS = 3
WATCHDOG_ALIVE_LOG_EVERY_SECONDS = 300
# Interval scan username berkala (tampilan heartbeat/panel). Dulu konstanta ini
# dipakai di _username_scanner_loop tapi TIDAK pernah didefinisikan -> NameError
# di baris pertama loop, scanner mati diam-diam, username selalu None.
USERNAME_SCAN_INTERVAL_SECONDS = 10

# LIFECYCLE REVISION (lihat CARRERA_JOKI_SESSION_LIFECYCLE_REVISION.txt):
# saat menunggu staff login akun customer, modul ini polling username_scanner
# per sekian detik -- SENGAJA lebih rapat dari USERNAME_SCAN_INTERVAL_SECONDS
# biasa (yang cuma untuk tampilan heartbeat) karena di sini hasilnya
# menggerbang transisi status (WAITING_LOGIN -> ACCOUNT_READY), staff
# menunggu real-time di Discord. Improve poin 3: dipangkas 5->2 detik --
# scan_username_blocking cuma 1x UI dump, cukup ringan buat dipoll lebih
# rapat tanpa membebani device, dan bikin deteksi akun terasa jauh lebih
# live buat staff yang nungguin di Discord.
LOGIN_POLL_INTERVAL_SECONDS = 2

# Batas waktu menunggu staff login akun customer sebelum menyerah dan
# melapor START_FAILED (supaya session tidak nyangkut WAITING_LOGIN selamanya
# kalau staff lupa/order salah). Staff bisa coba lagi lewat tombol "🔄 Coba
# Lagi" (bot.py) tanpa bikin order baru. Dinaikkan dari 15->30 menit (2026,
# permintaan Improve Joki Egg poin 2) supaya staff dikasih toleransi lebih
# panjang sebelum dianggap gagal -- perubahan ini SHARED dengan Treadmill
# (satu jalur Assign Device yang sama), jadi Treadmill ikut dapat toleransi
# 30 menit yang sama, bukan cuma Egg.
LOGIN_WAIT_TIMEOUT_SECONDS = 1800  # 30 menit

# Timeout untuk fase join ke target game SETELAH akun terkonfirmasi benar
# (terpisah dari DEFAULT_TIMEOUT_SECONDS -- dipakai launch_and_wait()
# dengan require_join_signal=True). Improve poin 3: dipangkas 60->35 detik
# -- keyword logcat yang ditunggu nyaris tidak pernah muncul di client
# modifikasi (mis. Delta Lite), jadi RF yang sudah beneran masuk game tidak
# perlu nunggu selama itu cuma untuk jatuh ke jalur UNCERTAIN/grace-check.
# launcher.py juga sekarang deteksi PID mati lebih cepat (tiap 3 detik),
# jadi timeout ini praktis cuma jadi batas atas untuk kasus join yang
# genuinely lambat, bukan lagi wajar kena penuh tiap kali.
JOIN_TIMEOUT_SECONDS = 35

# LIFECYCLE REVISION (Masalah #1): dipakai KHUSUS saat launch_and_wait()
# balik "UNCERTAIN" (proses hidup, tidak ada keyword sukses MAUPUN bukti
# kegagalan). Grace period singkat ini memberi kesempatan terakhir untuk
# menangkap bukti kegagalan asli (disconnect/kick) yang mungkin baru muncul
# sedikit terlambat, SEBELUM status ini diterima sebagai fallback RUNNING.
# SENGAJA jauh lebih pendek dari JOIN_TIMEOUT_SECONDS -- ini bukan menunggu
# join lagi dari awal, cuma observasi tambahan atas proses yang sudah hidup.
# Improve poin 3: dipangkas 10->6 detik, sejalan dengan JOIN_TIMEOUT_SECONDS
# yang juga dipangkas -- RF yang sudah beneran masuk game jadi lebih cepat
# dianggap RUNNING alih-alih nyangkut nunggu grace period penuh.
JOIN_VERIFY_GRACE_SECONDS = 6

# Dipanggil sebelum PREPARING (buka Roblox ke lobby dulu, TANPA target) --
# hanya perlu proses hidup, tidak perlu sinyal join game.
LOBBY_TIMEOUT_SECONDS = 30

# Recovery memakai flow yang sama untuk PID crash dan disconnect logcat.
RECOVERY_LOBBY_WAIT_MIN_SECONDS = 60
RECOVERY_LOBBY_WAIT_MAX_SECONDS = 90
RECOVERY_PRE_KILL_JITTER_MIN_SECONDS = 2
RECOVERY_PRE_KILL_JITTER_MAX_SECONDS = 6
RECOVERY_RETRY_BASE_DELAY_SECONDS = 20
RECOVERY_RETRY_MAX_DELAY_SECONDS = 300
RECOVERY_CIRCUIT_BREAKER_THRESHOLD = 5
RECOVERY_CIRCUIT_BREAKER_COOLDOWN_MIN_SECONDS = 300
RECOVERY_CIRCUIT_BREAKER_COOLDOWN_MAX_SECONDS = 600
RECOVERY_LOGIN_WAIT_TIMEOUT_SECONDS = 120
DISCONNECT_POLL_INTERVAL_SECONDS = 1
FREEFORM_ACTIVATION_DELAY_SECONDS = 2.5

# Jeda masuk map per package. Setelah lobby selesai, setiap package mendapat
# delay acak sendiri sebelum join target agar beberapa package tidak burst
# masuk map pada waktu yang sama. Nilai diacak ulang setiap kali join target.
LOBBY_WAIT_MIN_SECONDS = 60
LOBBY_WAIT_MAX_SECONDS = 90
MAP_ENTRY_DELAY_MIN_SECONDS = 10
MAP_ENTRY_DELAY_MAX_SECONDS = 30

def _recovery_backoff_delay(attempt: int) -> float:
    if attempt >= RECOVERY_CIRCUIT_BREAKER_THRESHOLD:
        return random.uniform(
            RECOVERY_CIRCUIT_BREAKER_COOLDOWN_MIN_SECONDS,
            RECOVERY_CIRCUIT_BREAKER_COOLDOWN_MAX_SECONDS,
        )
    base = min(
        RECOVERY_RETRY_MAX_DELAY_SECONDS,
        RECOVERY_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
    )
    return random.uniform(base * 0.7, base * 1.3)


# async(dict) -> None, diregistrasi agent_client.py SETIAP KALI koneksi WS
# baru tersambung (lihat register_sender()). Dipakai modul ini untuk kirim
# SESSION_STATUS (transisi antara PREPARING/WAITING_LOGIN/ACCOUNT_READY/
# JOINING_GAME/RUNNING) ke bot DI LUAR jalur balasan COMMAND_RESULT biasa --
# perlu karena satu START_SESSION sekarang bisa melewati BEBERAPA status
# sebelum benar-benar selesai (dulu cuma 1 balasan langsung ACTIVE/FAILED).
_send_callback = None


# package_name -> session state yang sedang dikelola oleh agent headless.
SESSIONS: dict = {}

# Task lifecycle per package.
_watchdog_tasks: dict = {}
_username_tasks: dict = {}
_start_tasks: dict = {}

# Recovery lock per package. Satu package hanya boleh punya satu recovery aktif.
_recovery_in_progress: set = set()

# Consumer error_detector global, satu per event loop agent.
_error_watcher_task = None

# package_name -> metadata Freeform yang sudah terverifikasi.
_FREEFORM_REGISTRY: dict = {}

# Urutan restore window survivor.
_FREEFORM_RESTORE_STEP_DELAY_SECONDS = 0.3
_FINISH_RESTORE_DELAY_SECONDS = 2.5
_FINISH_RESTORE_STEP_DELAY_SECONDS = 0.3


def register_sender(callback) -> None:
    """Dipanggil agent_client.py (dalam _run_agent(), setiap kali koneksi WS
    baru terbentuk/reconnect) supaya modul ini bisa push SESSION_STATUS ke
    bot kapan saja, tidak cuma sebagai balasan langsung atas satu pesan
    masuk. Aman dipanggil ulang tiap reconnect -- cukup overwrite referensi
    lama (session_agent.py tidak tahu/peduli detail koneksi WS)."""
    global _send_callback
    _send_callback = callback


async def _emit_status(local_device_id: str, session_id: str, pkg: str, order_id, status: str,
                        **extra) -> None:
    """Push runtime SESSION_STATUS tanpa membocorkan state internal watchdog.

    Event tetap boleh dikirim ketika entry lokal sudah dipop (mis. STOPPED),
    karena status terminal masih berguna untuk sinkronisasi bot.
    """
    info = SESSIONS.get(pkg)
    if info is not None and str(info.get("session_id")) == str(session_id):
        seq = int(info.get("status_seq", 0)) + 1
        info["status_seq"] = seq
        info["runtime_status"] = status
        info["last_status_at"] = time.time()
        payload = {
            "username": username_scanner.get_cached_username(pkg),
            "target": info.get("target"),
            "pid": info.get("pid", "-"),
            "timestamp": info["last_status_at"],
            "status_seq": seq,
        }
    else:
        payload = {
            "username": username_scanner.get_cached_username(pkg),
            "timestamp": time.time(),
        }

    payload.update(extra)
    payload.update({
        "type": "SESSION_STATUS",
        "device_id": local_device_id,
        "session_id": session_id,
        "package_name": pkg,
        "order_id": order_id,
        "status": status,
    })

    if _send_callback is None:
        log.warning(
            f"SESSION_AGENT: tidak ada sender terdaftar, SESSION_STATUS "
            f"'{status}' untuk {pkg} (session {session_id}) tidak terkirim."
        )
        return

    try:
        await _send_callback(payload)
    except Exception:
        log.warning(
            f"SESSION_AGENT: gagal kirim SESSION_STATUS '{status}' untuk {pkg} "
            f"(session {session_id}), koneksi mungkin putus."
        )

def _snapshot_for_heartbeat() -> dict:
    """Heartbeat operasional: state yang dilihat bot berasal dari runtime_status,
    sedangkan watchdog tetap memakai field internal `status`."""
    snapshot = {}
    for pkg, info in SESSIONS.items():
        # SUPERVISOR: watchdog tidak boleh hilang diam-diam. Heartbeat jalan
        # tiap 15 dtk, jadi ini jaring pengaman murah -- session ACTIVE/
        # RECOVERING yang watchdog-nya mati dihidupkan lagi.
        if info.get("status") in ("ACTIVE", "RECOVERING"):
            try:
                _ensure_watchdog(pkg)
            except RuntimeError:
                pass  # tidak ada event loop berjalan (mis. pemanggilan sinkron)
        snapshot[pkg] = {
            "pid": info.get("pid", "-"),
            "state": info.get("runtime_status") or info.get("status", "UNKNOWN"),
            "username": username_scanner.get_cached_username(pkg),
            "session_id": info.get("session_id", ""),
            "last_status_at": info.get("last_status_at"),
            # Tambahan (additive, bot lama mengabaikan): hasil cek PID nyata
            # terakhir -- sumber kebenaran liveness, bukan status internal.
            "pid_alive": info.get("pid_alive"),
            "last_pid_check_at": info.get("last_pid_check_at"),
        }
    return snapshot

def _snapshot_for_sync() -> dict:
    """Snapshot lengkap untuk SYNC_SESSIONS, termasuk runtime identity."""
    return {
        pkg: {
            "session_id": info.get("session_id", ""),
            "status": info.get("runtime_status") or info.get("status", "UNKNOWN"),
            "pid": info.get("pid", "-"),
            "username": username_scanner.get_cached_username(pkg),
            "expected_username": info.get("expected_username", ""),
            "target": info.get("target", ""),
            "last_status_at": info.get("last_status_at"),
            "status_seq": info.get("status_seq", 0),
        }
        for pkg, info in SESSIONS.items()
    }

async def handle_start_session(msg: dict, local_device_id: str) -> dict:
    """Terima START_SESSION secara idempotent dan mulai staged flow di background."""
    session_id = str(msg.get("session_id", "")).strip()
    order_id = msg.get("order_id")
    pkg = str(msg.get("package_name", "")).strip()
    target = str(msg.get("target", "")).strip()
    expected_username = str(msg.get("expected_username", "") or "").strip()

    try:
        timeout_seconds = int(msg.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    if not session_id or not pkg or not target:
        return {
            "type": "COMMAND_RESULT", "command": "START_SESSION",
            "device_id": local_device_id, "session_id": session_id,
            "ok": False, "reason": "MISSING_FIELDS",
        }

    try:
        intent_url = get_intent_url(target)
    except ValueError as e:
        return {
            "type": "COMMAND_RESULT", "command": "START_SESSION",
            "device_id": local_device_id, "session_id": session_id,
            "ok": False, "reason": f"INVALID_TARGET: {e}",
        }

    existing = SESSIONS.get(pkg)
    if existing is not None:
        existing_session = str(existing.get("session_id", ""))
        if existing_session == session_id:
            task = _start_tasks.get(pkg)
            if task is not None and not task.done():
                return {
                    "type": "COMMAND_RESULT", "command": "START_SESSION",
                    "device_id": local_device_id, "session_id": session_id,
                    "ok": True, "reason": "ALREADY_PROCESSING",
                }
            if existing.get("status") == "ACTIVE":
                return {
                    "type": "COMMAND_RESULT", "command": "START_SESSION",
                    "device_id": local_device_id, "session_id": session_id,
                    "ok": True, "reason": "ALREADY_ACTIVE",
                }
            if existing.get("status") in ("RECOVERING", "STARTING"):
                return {
                    "type": "COMMAND_RESULT", "command": "START_SESSION",
                    "device_id": local_device_id, "session_id": session_id,
                    "ok": True, "reason": "ALREADY_PROCESSING",
                }
            if existing.get("status") == "FAILED":
                SESSIONS.pop(pkg, None)
        else:
            log.warning(
                f"SESSION_AGENT: START_SESSION untuk {pkg} ditolak karena "
                f"masih dipegang session '{existing_session}'. "
                f"Request baru '{session_id}' tidak boleh menimpa session lama."
            )
            return {
                "type": "COMMAND_RESULT", "command": "START_SESSION",
                "device_id": local_device_id, "session_id": session_id,
                "ok": False, "reason": "PACKAGE_BUSY",
            }

    SESSIONS[pkg] = {
        "session_id": session_id,
        "order_id": order_id,
        "_device_id": local_device_id,
        "target": target,
        "expected_username": expected_username,
        "session_kind": str(msg.get("session_kind", "") or "").strip().upper(),
        "status": "STARTING",
        "runtime_status": "PREPARING",
        "pid": "-",
        "launch_count": 1,
        "crash_count": 0,
        "status_seq": 0,
        "last_status_at": time.time(),
    }

    # Username scanner dimulai SEBELUM staged flow supaya heartbeat tidak
    # perlu menunggu RUNNING untuk memperoleh username pertama.
    _ensure_username_scanner(pkg)

    task = asyncio.create_task(
        _run_start_flow(
            local_device_id, session_id, pkg, order_id,
            intent_url, expected_username, timeout_seconds,
        )
    )
    _start_tasks[pkg] = task

    def _cleanup_start_task(done_task):
        if _start_tasks.get(pkg) is done_task:
            _start_tasks.pop(pkg, None)

    task.add_done_callback(_cleanup_start_task)

    log.info(
        f"SESSION_AGENT: START_SESSION diterima -> {pkg} "
        f"(session {session_id}), expected_username="
        f"{expected_username or '(tidak divalidasi)'}."
    )
    return {
        "type": "COMMAND_RESULT", "command": "START_SESSION",
        "device_id": local_device_id, "session_id": session_id,
        "ok": True, "reason": "PROCESSING",
    }

async def _wait_in_lobby(pkg: str, session_id: str, *, recovery: bool = False) -> bool:
    """Tunggu 60-90 detik di lobby sebelum package boleh lanjut ke target."""
    current = SESSIONS.get(pkg)
    if not current or str(current.get("session_id")) != str(session_id):
        return False

    delay = random.uniform(LOBBY_WAIT_MIN_SECONDS, LOBBY_WAIT_MAX_SECONDS)
    current["last_lobby_wait_seconds"] = delay
    prefix = "[RECOVERY] " if recovery else ""
    log.info(
        f"SESSION_AGENT: {prefix}{pkg}/{session_id} menunggu di lobby "
        f"{delay:.1f}s sebelum lanjut ke tahap berikutnya."
    )
    await asyncio.sleep(delay)

    current = SESSIONS.get(pkg)
    return bool(current and str(current.get("session_id")) == str(session_id))


async def _wait_before_map_entry(pkg: str, session_id: str) -> bool:
    """Delay 10-30 detik acak per package sebelum join target/map."""
    current = SESSIONS.get(pkg)
    if not current or str(current.get("session_id")) != str(session_id):
        return False

    delay = random.uniform(MAP_ENTRY_DELAY_MIN_SECONDS, MAP_ENTRY_DELAY_MAX_SECONDS)
    current["last_map_entry_delay_seconds"] = delay
    log.info(
        f"SESSION_AGENT: {pkg}/{session_id} selesai lobby; "
        f"menunggu {delay:.1f}s sebelum JOIN MAP."
    )
    await asyncio.sleep(delay)

    current = SESSIONS.get(pkg)
    return bool(current and str(current.get("session_id")) == str(session_id))


async def _run_start_flow(local_device_id: str, session_id: str, pkg: str, order_id,
                           intent_url: str, expected_username: str, timeout_seconds: int) -> None:
    """PREPARING -> WAITING_LOGIN -> ACCOUNT_READY -> JOINING_GAME -> RUNNING."""
    def _still_current() -> bool:
        current = SESSIONS.get(pkg)
        return bool(current and current.get("session_id") == session_id)

    try:
        await _emit_status(local_device_id, session_id, pkg, order_id, "PREPARING")

        try:
            lobby_ok = await _launch_then_freeform_soon(
                pkg, get_lobby_intent(), LOBBY_TIMEOUT_SECONDS,
                require_join_signal=False, session_id=session_id,
            )
        except Exception:
            log.error(f"SESSION_AGENT: exception saat buka lobby {pkg}.", exc_info=True)
            lobby_ok = False

        if not _still_current():
            return
        if not lobby_ok:
            SESSIONS[pkg]["status"] = "FAILED"
            await _emit_status(
                local_device_id, session_id, pkg, order_id,
                "START_FAILED", reason="LOBBY_LAUNCH_FAILED",
            )
            return

        SESSIONS[pkg]["pid"] = get_pid_quick(pkg) or "-"

        if not await _wait_in_lobby(pkg, session_id):
            return

        if expected_username:
            await _emit_status(
                local_device_id, session_id, pkg, order_id,
                "WAITING_LOGIN", expected_username=expected_username,
            )
            deadline = time.monotonic() + LOGIN_WAIT_TIMEOUT_SECONDS
            detected = None
            last_reported_mismatch = None

            while time.monotonic() < deadline:
                if not _still_current():
                    return
                try:
                    detected = await asyncio.to_thread(
                        username_scanner.scan_username_blocking, pkg
                    )
                except Exception:
                    log.error(
                        f"SESSION_AGENT: exception scan username {pkg}.",
                        exc_info=True,
                    )
                    detected = None

                if detected and detected.strip().lower() == expected_username.lower():
                    break

                if detected and detected != last_reported_mismatch:
                    last_reported_mismatch = detected
                    await _emit_status(
                        local_device_id, session_id, pkg, order_id,
                        "WAITING_LOGIN",
                        expected_username=expected_username,
                        detected_username=detected,
                        mismatch=True,
                    )

                await asyncio.sleep(LOGIN_POLL_INTERVAL_SECONDS)
            else:
                if not _still_current():
                    return
                SESSIONS[pkg]["status"] = "FAILED"
                await _emit_status(
                    local_device_id, session_id, pkg, order_id,
                    "START_FAILED",
                    reason="LOGIN_TIMEOUT",
                    expected_username=expected_username,
                )
                return

            if not _still_current():
                return
            SESSIONS[pkg]["runtime_status"] = "ACCOUNT_READY"
            await _emit_status(
                local_device_id, session_id, pkg, order_id,
                "ACCOUNT_READY", detected_username=detected,
            )

        await _emit_status(local_device_id, session_id, pkg, order_id, "JOINING_GAME")

        if not await _wait_before_map_entry(pkg, session_id):
            return

        try:
            join_status, join_reason = await _launch_then_freeform_soon(
                pkg, intent_url, timeout_seconds,
                require_join_signal=True, session_id=session_id,
            )
        except Exception:
            log.error(
                f"SESSION_AGENT: exception saat join target {pkg}.",
                exc_info=True,
            )
            join_status, join_reason = "FAILED", "EXCEPTION"

        if not _still_current():
            return

        if join_status == "FAILED":
            SESSIONS[pkg]["status"] = "FAILED"
            await _emit_status(
                local_device_id, session_id, pkg, order_id,
                "START_FAILED",
                reason=f"JOIN_FAILED:{join_reason}",
            )
            return

        if join_status == "UNCERTAIN":
            await _emit_status(
                local_device_id, session_id, pkg, order_id,
                "VERIFYING_GAME", reason=join_reason,
            )

            grace_start_str = datetime.datetime.now().strftime("%m-%d %H:%M:%S.000")
            await asyncio.sleep(JOIN_VERIFY_GRACE_SECONDS)

            if not _still_current():
                return

            pid_after_grace = get_pid_quick(pkg)
            if not pid_after_grace:
                SESSIONS[pkg]["status"] = "FAILED"
                await _emit_status(
                    local_device_id, session_id, pkg, order_id,
                    "START_FAILED",
                    reason="PROCESS_DIED_DURING_VERIFY",
                )
                return

            try:
                has_failure, failure_code = await asyncio.to_thread(
                    has_recent_disconnect_signal,
                    pkg,
                    grace_start_str,
                    pid=pid_after_grace,
                )
            except Exception:
                log.error(
                    f"SESSION_AGENT: exception grace-check disconnect signal {pkg}.",
                    exc_info=True,
                )
                has_failure, failure_code = False, None

            if has_failure:
                SESSIONS[pkg]["status"] = "FAILED"
                await _emit_status(
                    local_device_id, session_id, pkg, order_id,
                    "START_FAILED",
                    reason=f"JOIN_ERROR_SIGNAL_DURING_VERIFY_{failure_code}",
                )
                return

        if not _still_current():
            return

        await _activate_freeform_and_restore_siblings(pkg, session_id)
        if not _still_current():
            return

        SESSIONS[pkg]["status"] = "ACTIVE"
        SESSIONS[pkg]["runtime_status"] = "RUNNING"
        SESSIONS[pkg]["pid"] = get_pid_quick(pkg) or "-"

        _ensure_watchdog(pkg)
        _ensure_username_scanner(pkg)
        _ensure_error_watcher()

        await _emit_status(local_device_id, session_id, pkg, order_id, "RUNNING")
        log.info(
            f"SESSION_AGENT: {pkg} (session {session_id}) RUNNING."
        )

    except asyncio.CancelledError:
        raise
    except Exception:
        log.error(
            f"SESSION_AGENT: exception tak terduga dalam START_SESSION "
            f"{pkg}/{session_id}.",
            exc_info=True,
        )
        if _still_current():
            SESSIONS[pkg]["status"] = "FAILED"
            await _emit_status(
                local_device_id, session_id, pkg, order_id,
                "START_FAILED", reason="UNHANDLED_EXCEPTION",
            )

async def _activate_freeform_and_restore_siblings(pkg: str, session_id: str) -> None:
    """
    SATU-SATUNYA titik pkg diubah ke Freeform. Dipanggil setelah Roblox
    dipastikan aktif (lobby_ok / join_status sukses -- proses SUDAH
    launch_and_wait(..., defer_freeform=True), yaitu launch NORMAL tanpa
    windowingMode dipaksakan saat masih transisi/splash).

    Urutan sesuai permintaan:
    LAUNCH NORMAL (selesai sebelum dipanggil) -> ROBLOX AKTIF (selesai
    sebelum dipanggil) -> UBAH KE FREEFORM -> VERIFY WINDOW BENAR-BENAR
    MUNCUL (activate_freeform() -- cek dumpsys window windows, bukan cuma
    task/activity metadata) -> kalau berhasil, BANGKITKAN kembali semua
    package LAIN yang sudah lebih dulu Freeform (mereka bisa saja ikut
    ketutup/ke-background akibat proses buka package ini) lewat
    `monkey -p <package> 1` (process_manager.restore_foreground) SATU PER
    SATU -- ini murni bring-to-front, TIDAK pernah kill/restart/rejoin
    package lain.

    Dipanggil untuk package yang sama di lebih dari satu tahap (lobby DAN
    setelah join target) supaya kalau ada transisi task lanjutan saat join,
    window-nya tetap diverifikasi ulang -- idempotent & aman dipanggil
    berkali-kali untuk package yang sama.

    Gagal di sini TIDAK PERNAH menghentikan session (freeform murni
    kosmetik/kenyamanan operator, bukan syarat validitas joki) -- cuma
    di-log.
    """
    try:
        ok, task_id = await asyncio.to_thread(activate_freeform, pkg)
    except Exception:
        log.error(f"SESSION_AGENT: exception saat activate_freeform {pkg}.", exc_info=True)
        ok, task_id = False, None

    if not ok:
        log.warning(f"SESSION_AGENT: {pkg} (session {session_id}) belum kekonfirmasi Freeform+tampil -- "
                    f"lanjut proses seperti biasa (tidak menghentikan session gara-gara ini).")
        return

    log.info(f"SESSION_AGENT: {pkg} (session {session_id}) Freeform terverifikasi tampil (taskId={task_id}).")
    was_registered = pkg in _FREEFORM_REGISTRY
    _FREEFORM_REGISTRY[pkg] = {"session_id": session_id, "task_id": task_id, "state": "VISIBLE"}

    # Sibling: package LAIN yang sudah lebih dulu Freeform DAN masih ACTIVE
    # di SESSIONS (jangan restore package yang sesi-nya sudah berhenti/mati).
    siblings = [p for p in _FREEFORM_REGISTRY
                if p != pkg and p in SESSIONS and SESSIONS[p].get("status") == "ACTIVE"]
    if not siblings:
        return

    log.info(f"SESSION_AGENT: {pkg} {'re-verify' if was_registered else 'baru'} Freeform -- "
             f"bangkitkan {len(siblings)} package lain yang sudah lebih dulu Freeform: {siblings}")
    for sibling_pkg in siblings:
        # Re-cek per iterasi: kalau sibling ini berhenti selagi loop restore
        # masih jalan, jangan sentuh.
        if sibling_pkg not in SESSIONS or SESSIONS[sibling_pkg].get("status") != "ACTIVE":
            continue
        restored = await asyncio.to_thread(process_manager.restore_foreground, sibling_pkg)
        log.info(f"SESSION_AGENT: restore foreground sibling {sibling_pkg} (rotasi freeform): "
                 f"{'OK' if restored else 'GAGAL'}")
        await asyncio.sleep(_FREEFORM_RESTORE_STEP_DELAY_SECONDS)


async def _launch_then_freeform_soon(
    pkg: str,
    intent_url: str,
    timeout_seconds: int,
    require_join_signal: bool,
    session_id: str,
    only_if_previously_freeform: bool = False,
):
    """Launch normal -> timer Freeform -> Smart Wait secara paralel."""
    ok, start_time_str = await asyncio.to_thread(
        launch_normal, pkg, intent_url
    )
    if not ok:
        return (
            ("FAILED", "LAUNCH_COMMAND_FAILED")
            if require_join_signal else False
        )

    async def _freeform_after_delay():
        if only_if_previously_freeform and pkg not in _FREEFORM_REGISTRY:
            return
        await asyncio.sleep(FREEFORM_ACTIVATION_DELAY_SECONDS)
        try:
            await _activate_freeform_and_restore_siblings(pkg, session_id)
        except Exception:
            log.error(
                f"SESSION_AGENT: exception freeform timer {pkg}.",
                exc_info=True,
            )

    freeform_task = asyncio.create_task(_freeform_after_delay())
    try:
        return await asyncio.to_thread(
            wait_for_launch_signal,
            pkg,
            start_time_str,
            timeout_seconds,
            require_join_signal,
        )
    finally:
        try:
            await freeform_task
        except Exception:
            log.error(
                f"SESSION_AGENT: exception freeform timer cleanup {pkg}.",
                exc_info=True,
            )


def _ensure_watchdog(pkg: str) -> None:
    """Pastikan cuma ADA SATU watchdog task per package (kalau operator kirim
    START_SESSION lagi untuk package yang sama sebelum watchdog lama berhenti,
    jangan numpuk task)."""
    existing = _watchdog_tasks.get(pkg)
    if existing and not existing.done():
        return
    _watchdog_tasks[pkg] = asyncio.create_task(_headless_watchdog_loop(pkg))


def _ensure_username_scanner(pkg: str) -> None:
    """PHASE 4.5: sama seperti _ensure_watchdog -- pastikan cuma SATU
    scanner task per package."""
    existing = _username_tasks.get(pkg)
    if existing and not existing.done():
        return
    _username_tasks[pkg] = asyncio.create_task(_username_scanner_loop(pkg))


async def _username_scanner_loop(pkg: str) -> None:
    """Scan username berkala tanpa boleh menyeberang ke session baru."""
    log.info(
        f"SESSION_AGENT: username scanner mulai untuk {pkg} "
        f"(interval {USERNAME_SCAN_INTERVAL_SECONDS}s)."
    )
    session_id = None
    try:
        while True:
            info = SESSIONS.get(pkg)
            if info is None:
                break

            if session_id is None:
                session_id = str(info.get("session_id", ""))
            elif str(info.get("session_id", "")) != session_id:
                # Package dipakai session baru. Task lama wajib berhenti agar
                # tidak mengirim username/status untuk session yang berbeda.
                break

            if info.get("status") in ("FAILED", "STOPPED"):
                break

            try:
                prev = info.get("_last_logged_username")
                new_username = await asyncio.to_thread(
                    username_scanner.scan_username_blocking, pkg
                )

                current = SESSIONS.get(pkg)
                if current is None or str(current.get("session_id", "")) != session_id:
                    break

                if new_username != prev:
                    current["_last_logged_username"] = new_username
                    log.info(
                        f"SESSION_AGENT: username {pkg} -> "
                        f"{new_username or '(tidak diketahui)'}."
                    )
                    runtime = (
                        current.get("runtime_status")
                        or current.get("status", "UNKNOWN")
                    )
                    await _emit_status(
                        current.get("_device_id", ""),
                        session_id,
                        pkg,
                        current.get("order_id"),
                        runtime,
                        detected_username=new_username,
                    )
            except Exception:
                log.error(
                    f"SESSION_AGENT: exception scan username {pkg}.",
                    exc_info=True,
                )

            await asyncio.sleep(USERNAME_SCAN_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        raise
    finally:
        _username_tasks.pop(pkg, None)
        current = SESSIONS.get(pkg)
        if current is None or str(current.get("session_id", "")) == str(session_id or ""):
            username_scanner.forget(pkg)
        log.info(f"SESSION_AGENT: username scanner berhenti untuk {pkg}.")

async def _finish_kill_and_restore_survivors(
    local_device_id: str,
    session_id: str,
    pkg: str,
    recorded_pid: str,
    order_id=None,
) -> None:
    """Kill target secara terisolasi lalu restore package survivor."""
    log.info(
        f"[FINISH] session={session_id} package={pkg} -- mulai stop."
    )

    # PID selalu di-resolve ulang. `recorded_pid` hanya informasi diagnostik,
    # bukan target kill yang dipercaya.
    current_pid = await asyncio.to_thread(process_manager.get_pid, pkg)
    if current_pid:
        killed = await asyncio.to_thread(
            process_manager.kill_pid_direct, current_pid
        )
        if killed:
            log.info(
                f"[FINISH] session={session_id} package={pkg} "
                f"pid={current_pid} -- target dipastikan mati."
            )
        else:
            log.error(
                f"[FINISH] session={session_id} package={pkg} "
                f"pid={current_pid} -- target gagal dipastikan mati."
            )
    else:
        log.info(
            f"[FINISH] session={session_id} package={pkg} "
            f"(recorded_pid={recorded_pid}) -- proses sudah tidak ada."
        )

    await asyncio.sleep(_FINISH_RESTORE_DELAY_SECONDS)

    survivors = [
        p for p, info in SESSIONS.items()
        if p != pkg and info.get("status") == "ACTIVE"
    ]
    for survivor_pkg in survivors:
        info = SESSIONS.get(survivor_pkg)
        if not info or info.get("status") != "ACTIVE":
            continue

        ok = await asyncio.to_thread(
            process_manager.restore_foreground, survivor_pkg
        )
        log.info(
            f"[FINISH] session={session_id} restore survivor "
            f"{survivor_pkg}: {'OK' if ok else 'GAGAL'}"
        )
        await asyncio.sleep(_FINISH_RESTORE_STEP_DELAY_SECONDS)

    await _emit_status(
        local_device_id,
        session_id,
        pkg,
        order_id,
        "STOPPED",
        pid="-",
    )

async def handle_stop_session(msg: dict, local_device_id: str, do_reset: bool = True) -> dict:
    """STOP_SESSION dengan anti-rejoin dan status STOPPING -> STOPPED."""
    session_id = str(msg.get("session_id", "")).strip()
    pkg = str(msg.get("package_name", "")).strip()

    if not session_id or not pkg:
        return {
            "type": "COMMAND_RESULT", "command": "STOP_SESSION",
            "device_id": local_device_id, "session_id": session_id,
            "ok": False, "reason": "MISSING_FIELDS",
        }

    info = SESSIONS.get(pkg)
    if info is None:
        return {
            "type": "COMMAND_RESULT", "command": "STOP_SESSION",
            "device_id": local_device_id, "session_id": session_id,
            "ok": True, "reason": "NO_LOCAL_SESSION",
        }

    if str(info.get("session_id")) != session_id:
        return {
            "type": "COMMAND_RESULT", "command": "STOP_SESSION",
            "device_id": local_device_id, "session_id": session_id,
            "ok": False, "reason": "STALE_SESSION_ID",
        }

    await _emit_status(
        local_device_id, session_id, pkg, info.get("order_id"),
        "STOPPING",
    )

    info["status"] = "STOPPED"
    info["runtime_status"] = "STOPPING"
    target_pid = info.get("pid", "-")

    watchdog = _watchdog_tasks.pop(pkg, None)
    if watchdog is not None and not watchdog.done():
        watchdog.cancel()

    scanner = _username_tasks.pop(pkg, None)
    if scanner is not None and not scanner.done():
        scanner.cancel()

    start_task = _start_tasks.pop(pkg, None)
    if start_task is not None and not start_task.done():
        start_task.cancel()

    SESSIONS.pop(pkg, None)
    _FREEFORM_REGISTRY.pop(pkg, None)

    if do_reset:
        asyncio.create_task(
            _finish_kill_and_restore_survivors(
                local_device_id, session_id, pkg, target_pid,
                order_id=info.get("order_id"),
            )
        )

    return {
        "type": "COMMAND_RESULT", "command": "STOP_SESSION",
        "device_id": local_device_id, "session_id": session_id,
        "ok": True, "reason": "STOPPED",
    }

_restart_tasks: set = set()


async def handle_restart_package(msg: dict, local_device_id: str) -> dict:
    """RESTART_PACKAGE: restart SATU package (bukan seluruh device).

    Memakai _run_package_recovery yang sudah ada (kill PID lama -> pastikan
    mati -> relaunch -> tunggu PID baru -> verifikasi username -> lobby ->
    join target -> RUNNING) -- BUKAN sistem recovery kedua. Integrasi dengan
    watchdog: lock _recovery_in_progress + status RECOVERING di-set SINKRON
    sebelum await apa pun, jadi watchdog (yang mengabaikan package ber-lock)
    tidak pernah memicu recovery otomatis kedua saat PID lama hilang."""
    session_id = str(msg.get("session_id", "")).strip()
    pkg = str(msg.get("package_name", "")).strip()

    def _result(ok: bool, reason: str) -> dict:
        return {
            "type": "COMMAND_RESULT", "command": "RESTART_PACKAGE",
            "device_id": local_device_id, "session_id": session_id,
            "package_name": pkg, "ok": ok, "reason": reason,
        }

    if not session_id or not pkg:
        return _result(False, "MISSING_FIELDS")
    info = SESSIONS.get(pkg)
    if info is None:
        return _result(False, "NO_LOCAL_SESSION")
    if str(info.get("session_id")) != session_id:
        return _result(False, "STALE_SESSION_ID")
    if pkg in _recovery_in_progress:
        return _result(True, "ALREADY_IN_PROGRESS")
    if info.get("status") not in ("ACTIVE", "RECOVERING"):
        # Masih tahap start awal / sudah STOPPING: bukan untuk di-restart manual.
        return _result(False, "NOT_RUNNING")

    # --- SINKRON: tidak ada await antara cek lock dan add lock ---
    _recovery_in_progress.add(pkg)
    info["_manual_restart"] = True
    info["status"] = "RECOVERING"
    info["runtime_status"] = "RESTARTING"
    info["pid_alive"] = False
    log.info(f"[{pkg}] MANUAL_RESTART diminta (PID lama: {info.get('pid') or '-'}). "
             "Hanya package ini yang diproses.")
    task = asyncio.create_task(_run_package_recovery(pkg, session_id, "MANUAL_RESTART"))
    _restart_tasks.add(task)
    task.add_done_callback(_restart_tasks.discard)
    return _result(True, "RESTART_STARTED")


def _pick_pid(pids) -> str:
    """Pilih satu PID deterministik dari himpunan PID hidup."""
    return str(min((int(p) for p in pids), default="")) if pids else ""


async def _start_recovery_inline(pkg: str, info: dict, trigger: str) -> None:
    """Mulai recovery SATU package. WAJIB dipanggil dengan _recovery_in_progress
    sudah di-add secara SINKRON oleh caller (tidak ada await di antara cek
    lock dan add) supaya tidak ada dua recovery untuk package yang sama."""
    session_id = info.get("session_id", "")
    info["crash_count"] = info.get("crash_count", 0) + 1
    info["status"] = "RECOVERING"
    info["runtime_status"] = "RECOVERING"
    info["pid_alive"] = False
    try:
        await _emit_status(
            info.get("_device_id", ""), session_id, pkg,
            info.get("order_id"), "RECOVERING", reason=trigger,
        )
        await _run_package_recovery(pkg, session_id, trigger)
    except asyncio.CancelledError:
        _recovery_in_progress.discard(pkg)
        raise
    except Exception:
        log.error(f"[{pkg}] watchdog: exception saat memulai recovery.", exc_info=True)
        _recovery_in_progress.discard(pkg)


async def _watchdog_tick(pkg: str, info: dict) -> None:
    """Satu siklus pengecekan PID NYATA untuk satu package (expected vs actual)."""
    status = info.get("status")

    # Recovery yang berakhir karena exception melepas lock tapi status tetap
    # RECOVERING -> tanpa ini package nyangkut selamanya. Dua tick berturut-
    # turut tanpa recovery aktif = anggap macet, lanjutkan.
    if status == "RECOVERING":
        if pkg in _recovery_in_progress:
            info["_stuck_ticks"] = 0
            return
        info["_stuck_ticks"] = info.get("_stuck_ticks", 0) + 1
        if info["_stuck_ticks"] >= WATCHDOG_STUCK_RECOVERY_TICKS:
            info["_stuck_ticks"] = 0
            _recovery_in_progress.add(pkg)  # sinkron, sebelum await apa pun
            log.warning(f"[{pkg}] RECOVERING tanpa recovery aktif -- melanjutkan recovery.")
            await _start_recovery_inline(pkg, info, "RECOVERY_RESUME")
        return

    if status != "ACTIVE":
        return
    info["_stuck_ticks"] = 0
    if pkg in _recovery_in_progress:
        return

    pids = await asyncio.to_thread(process_manager.get_pids, pkg)
    info["last_pid_check_at"] = time.time()

    if pids is None:
        # Pengecekan gagal/ambigu: BUKAN bukti proses mati.
        log.warning(f"[{pkg}] PID check gagal/ambigu (tidak dianggap mati); ulangi tick berikutnya.")
        return

    if pids:
        recorded = str(info.get("pid") or "")
        info["pid_alive"] = True
        info["_pid_misses"] = 0
        if recorded not in pids:
            new_pid = _pick_pid(pids)
            log.info(f"[{pkg}] PID berubah {recorded or '-'} -> {new_pid} (proses hidup, monitor diperbarui).")
            info["pid"] = new_pid
        elif time.time() - info.get("_last_alive_log", 0) >= WATCHDOG_ALIVE_LOG_EVERY_SECONDS:
            info["_last_alive_log"] = time.time()
            log.info(f"[{pkg}] PID check: ACTIVE (pid={recorded}).")
        return

    # pids == set(): tidak ada proses. Konfirmasi ulang sebelum recovery.
    log.warning(f"[{pkg}] PID check: NOT FOUND (expected pid={info.get('pid') or '-'}); konfirmasi ulang...")
    await asyncio.sleep(WATCHDOG_CONFIRM_DELAY_SECONDS)
    if SESSIONS.get(pkg) is not info or info.get("status") != "ACTIVE" or pkg in _recovery_in_progress:
        return
    pids = await asyncio.to_thread(process_manager.get_pids, pkg)
    if pids is None:
        log.warning(f"[{pkg}] konfirmasi PID gagal/ambigu; tidak memicu recovery.")
        return
    if pids:
        info["pid_alive"] = True
        info["pid"] = _pick_pid(pids)
        log.info(f"[{pkg}] PID muncul lagi pada konfirmasi (pid={info['pid']}); tidak ada recovery.")
        return
    if SESSIONS.get(pkg) is not info or info.get("status") != "ACTIVE" or pkg in _recovery_in_progress:
        return

    _recovery_in_progress.add(pkg)  # sinkron: tidak ada await sejak cek lock terakhir
    log.warning(f"[{pkg}] Process appears dead (PID NOT FOUND 2x). Triggering recovery.")
    await _start_recovery_inline(pkg, info, "PID_CRASH")


async def _headless_watchdog_loop(pkg: str) -> None:
    """Watchdog headless per package: expected state (ACTIVE) vs PID NYATA.

    Perbaikan: exception di satu siklus TIDAK lagi mematikan task (dulu
    exception apa pun menghentikan watchdog tanpa ada yang tahu, package
    tetap 'ACTIVE' tanpa pengawas). Supervisor tambahan ada di
    _snapshot_for_heartbeat.
    """
    log.info(f"SESSION_AGENT: watchdog headless mulai untuk {pkg}.")
    try:
        while pkg in SESSIONS:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
            info = SESSIONS.get(pkg)
            if info is None:
                break
            try:
                await _watchdog_tick(pkg, info)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.error(f"[{pkg}] watchdog tick exception (watchdog TETAP berjalan).", exc_info=True)
    except asyncio.CancelledError:
        raise
    finally:
        _watchdog_tasks.pop(pkg, None)
        log.info(f"SESSION_AGENT: watchdog headless berhenti untuk {pkg}.")

def _ensure_error_watcher() -> None:
    global _error_watcher_task
    if _error_watcher_task is not None and not _error_watcher_task.done():
        return
    start_error_detector()
    _error_watcher_task = asyncio.create_task(_error_event_consumer_loop())


async def _error_event_consumer_loop() -> None:
    log.info("SESSION_AGENT: error watcher mulai.")
    try:
        while True:
            await asyncio.sleep(DISCONNECT_POLL_INTERVAL_SECONDS)
            while has_event():
                event = get_event()
                if event is None:
                    break
                try:
                    await _handle_disconnect_event(event)
                except Exception:
                    log.error(
                        "SESSION_AGENT: exception memproses disconnect event.",
                        exc_info=True,
                    )
    except asyncio.CancelledError:
        raise


async def _handle_disconnect_event(event: dict) -> None:
    pid = str(event.get("pid") or "").strip()
    reason = event.get("reason")
    if not pid:
        return

    for pkg, info in list(SESSIONS.items()):
        if (
            info.get("status") == "ACTIVE"
            and str(info.get("pid") or "") == pid
            and pkg not in _recovery_in_progress
        ):
            session_id = info.get("session_id", "")
            # Lock + status di-set SINKRON sebelum await apa pun, supaya
            # watchdog tidak melihat 'RECOVERING tanpa lock' lalu memulai
            # recovery kedua untuk package yang sama (race).
            _recovery_in_progress.add(pkg)
            info["status"] = "RECOVERING"
            info["runtime_status"] = "RECOVERING"
            info["pid_alive"] = False
            await _emit_status(
                info.get("_device_id", ""),
                session_id,
                pkg,
                info.get("order_id"),
                "RECOVERING",
                reason=f"LOGCAT_{reason}",
            )
            asyncio.create_task(
                _run_package_recovery(
                    pkg, session_id, f"LOGCAT_{reason}"
                )
            )
            return


async def _run_package_recovery(pkg: str, session_id: str, trigger_reason: str) -> None:
    def _still_current() -> bool:
        current = SESSIONS.get(pkg)
        return bool(current and current.get("session_id") == session_id)

    try:
        if not _still_current():
            return

        info = SESSIONS[pkg]
        target = info.get("target", "")
        expected_username = info.get("expected_username", "")
        attempt = 0

        while _still_current():
            attempt += 1
            info = SESSIONS[pkg]
            manual = bool(info.get("_manual_restart"))
            info["status"] = "RECOVERING"
            # Restart manual punya state sendiri di heartbeat/panel:
            # RESTARTING -> WAITING_FOR_PID -> (JOINING_GAME ...) -> RUNNING.
            info["runtime_status"] = "RESTARTING" if manual else "RECOVERING"

            if attempt > 1:
                await asyncio.sleep(_recovery_backoff_delay(attempt - 1))
            if not _still_current():
                return

            if not manual:  # restart manual dijalankan langsung, tanpa jitter
                await asyncio.sleep(random.uniform(
                    RECOVERY_PRE_KILL_JITTER_MIN_SECONDS,
                    RECOVERY_PRE_KILL_JITTER_MAX_SECONDS,
                ))
            if not _still_current():
                return

            # Kill TIAP PID hidup milik package ini satu per satu (get_pid
            # lama mengembalikan string gabungan "p1 p2" kalau lebih dari
            # satu proses). Tetap hanya PID package ini -- tidak ada global kill.
            old_pids = set()
            for _round in range(KILL_VERIFY_MAX_ROUNDS):
                stale_pids = await asyncio.to_thread(process_manager.get_pids, pkg) or set()
                if not stale_pids:
                    break  # PID lama BENAR-BENAR sudah mati
                old_pids |= set(stale_pids)
                for stale_pid in sorted(stale_pids):
                    log.info(f"[{pkg}] recovery: mematikan PID {stale_pid} sebelum relaunch.")
                    await asyncio.to_thread(
                        process_manager.kill_pid_direct, stale_pid
                    )
                    await asyncio.to_thread(
                        process_manager.wait_until_process_dead,
                        stale_pid, 10,
                    )
            else:
                log.warning(f"[{pkg}] recovery: PID lama masih terdeteksi setelah "
                            f"{KILL_VERIFY_MAX_ROUNDS}x kill; lanjut relaunch.")

            if not _still_current():
                return

            await _emit_status(
                info.get("_device_id", ""), session_id, pkg,
                info.get("order_id"), "RECOVERING",
                reason=trigger_reason,
            )
            if manual:
                # _emit_status menimpa runtime_status; kembalikan ke state restart.
                info["runtime_status"] = "WAITING_FOR_PID"

            lobby_ok = await _launch_then_freeform_soon(
                pkg, get_lobby_intent(), LOBBY_TIMEOUT_SECONDS,
                require_join_signal=False, session_id=session_id,
                only_if_previously_freeform=True,
            )
            if not _still_current():
                return

            if not lobby_ok:
                continue

            info = SESSIONS[pkg]
            old_pid = info.get("pid")
            info["pid"] = get_pid_quick(pkg) or "-"
            if manual:
                # Tunggu PID BARU (bukan PID lama) sebelum lanjut verifikasi.
                info["runtime_status"] = "WAITING_FOR_PID"
                deadline = time.monotonic() + RESTART_WAIT_NEW_PID_SECONDS
                while time.monotonic() < deadline and _still_current():
                    new_pids = await asyncio.to_thread(process_manager.get_pids, pkg)
                    fresh = (new_pids or set()) - old_pids
                    if fresh:
                        info["pid"] = _pick_pid(fresh)
                        break
                    await asyncio.sleep(1)
                else:
                    if _still_current():
                        log.warning(f"[{pkg}] restart: PID baru belum terdeteksi dalam "
                                    f"{RESTART_WAIT_NEW_PID_SECONDS}s; lanjut, watchdog akan memantau.")
                if not _still_current():
                    return
                info["runtime_status"] = "RESTARTING"
            log.info(f"[{pkg}] Roblox launched (lobby). PID {old_pid or '-'} -> {info['pid']}")

            if not await _wait_in_lobby(pkg, session_id, recovery=True):
                return

            # Jangan rejoin menggunakan akun yang salah. Recovery wajib
            # melihat ulang username setelah launch lobby.
            if expected_username:
                deadline = time.monotonic() + RECOVERY_LOGIN_WAIT_TIMEOUT_SECONDS
                detected = None
                while time.monotonic() < deadline:
                    if not _still_current():
                        return
                    detected = await asyncio.to_thread(
                        username_scanner.scan_username_blocking, pkg
                    )
                    if (
                        detected
                        and detected.strip().lower()
                        == expected_username.strip().lower()
                    ):
                        break
                    await asyncio.sleep(LOGIN_POLL_INTERVAL_SECONDS)
                else:
                    await _emit_status(
                        info.get("_device_id", ""), session_id, pkg,
                        info.get("order_id"), "RECOVERING",
                        expected_username=expected_username,
                        reason="WAITING_CORRECT_USERNAME",
                    )
                    continue

            await _emit_status(
                info.get("_device_id", ""), session_id, pkg,
                info.get("order_id"), "JOINING_GAME",
            )

            if not await _wait_before_map_entry(pkg, session_id):
                return

            try:
                intent_url = get_intent_url(target)
            except ValueError as e:
                log.error(
                    f"SESSION_AGENT: recovery target invalid {pkg}: {e}"
                )
                continue

            joined = await _attempt_join_target(
                pkg, session_id, intent_url, DEFAULT_TIMEOUT_SECONDS
            )
            if not _still_current():
                return

            if not joined:
                continue

            info = SESSIONS[pkg]
            info["status"] = "ACTIVE"
            info["runtime_status"] = "RUNNING"
            info["pid"] = get_pid_quick(pkg) or "-"
            info["pid_alive"] = info["pid"] != "-"
            info["_pid_misses"] = 0
            info["launch_count"] = info.get("launch_count", 0) + 1
            if info.pop("_manual_restart", False):
                info["restart_count"] = info.get("restart_count", 0) + 1
                info["last_restart_at"] = time.time()
                log.info(f"[{pkg}] MANUAL_RESTART selesai. New PID: {info['pid']}. State: RUNNING")
            else:
                log.info(f"[{pkg}] New PID detected: {info['pid']}. Recovery successful. State: RUNNING")

            _ensure_watchdog(pkg)
            _ensure_username_scanner(pkg)
            _ensure_error_watcher()

            await _activate_freeform_and_restore_siblings(pkg, session_id)
            await _emit_status(
                info.get("_device_id", ""), session_id, pkg,
                info.get("order_id"), "RUNNING",
                recovered=True,
            )
            log.info(
                f"SESSION_AGENT: [RECOVERY] {pkg} berhasil kembali RUNNING "
                f"(attempt={attempt})."
            )
            return
    except asyncio.CancelledError:
        raise
    except Exception:
        log.error(
            f"SESSION_AGENT: [RECOVERY] exception {pkg}/{session_id}.",
            exc_info=True,
        )
    finally:
        _recovery_in_progress.discard(pkg)
        leftover = SESSIONS.get(pkg)
        if leftover is not None:
            leftover.pop("_manual_restart", None)


async def _attempt_join_target(
    pkg: str, session_id: str, intent_url: str, timeout_seconds: int
) -> bool:
    def _still_current() -> bool:
        current = SESSIONS.get(pkg)
        return bool(current and current.get("session_id") == session_id)

    result = await _launch_then_freeform_soon(
        pkg, intent_url, timeout_seconds,
        require_join_signal=True, session_id=session_id,
    )
    if not _still_current():
        return False

    join_status, join_reason = result
    if join_status == "FAILED":
        log.warning(
            f"SESSION_AGENT: [RECOVERY] {pkg} join gagal: {join_reason}"
        )
        return False

    if join_status == "UNCERTAIN":
        await _emit_status(
            SESSIONS[pkg].get("_device_id", ""),
            session_id, pkg,
            SESSIONS[pkg].get("order_id"),
            "VERIFYING_GAME", reason=join_reason,
        )
        grace_start_str = datetime.datetime.now().strftime("%m-%d %H:%M:%S.000")
        await asyncio.sleep(JOIN_VERIFY_GRACE_SECONDS)
        if not _still_current():
            return False
        pid_after_grace = get_pid_quick(pkg)
        if not pid_after_grace:
            return False
        try:
            has_failure, failure_code = await asyncio.to_thread(
                has_recent_disconnect_signal,
                pkg,
                grace_start_str,
                pid=pid_after_grace,
            )
        except Exception:
            log.error(
                f"SESSION_AGENT: recovery grace-check exception {pkg}.",
                exc_info=True,
            )
            has_failure, failure_code = False, None
        if has_failure:
            log.warning(
                f"SESSION_AGENT: [RECOVERY] {pkg} failure signal "
                f"{failure_code}."
            )
            return False

    return True


# ==========================================================
# PHASE 8: SYNC_SESSIONS
# ==========================================================

async def handle_sync_sessions(msg: dict, local_device_id: str) -> dict:
    """Entry point dipanggil agent_client.py saat terima SYNC_SESSIONS dari
    bot. Bot SELALU mengirim ini sesaat setelah REGISTER_OK -- mencakup TIGA
    skenario reconnect sekaligus lewat jalur yang sama (bot restart, device
    reconnect, WS putus-nyambung), device tidak perlu tahu/bedakan mana yang
    mana. Lihat reconcile_sync() untuk aturan lengkap."""
    expected = msg.get("sessions", [])
    if not isinstance(expected, list):
        expected = []
    snapshot = await reconcile_sync(expected, local_device_id)
    return {"type": "SYNC_RESPONSE", "device_id": local_device_id, "packages": snapshot}


async def reconcile_sync(expected_sessions: list, local_device_id: str) -> dict:
    """Reconcile bot-authoritative session list dengan proses fisik device."""
    expected_by_pkg = {}
    for entry in expected_sessions:
        if not isinstance(entry, dict):
            continue
        pkg = str(entry.get("package_name", "")).strip()
        if pkg:
            expected_by_pkg[pkg] = entry

    # Local-only sessions adalah orphan dari perspektif bot.
    for pkg in list(SESSIONS.keys()):
        if pkg not in expected_by_pkg:
            local_session_id = SESSIONS[pkg].get("session_id", "")
            await handle_stop_session(
                {"session_id": local_session_id, "package_name": pkg},
                local_device_id,
            )

    transitional = {
        "ASSIGNED", "PREPARING", "WAITING_LOGIN",
        "ACCOUNT_READY", "JOINING_GAME", "VERIFYING_GAME",
        "STARTING",
    }
    running = {"RUNNING", "ACTIVE"}

    for pkg, entry in expected_by_pkg.items():
        desired_status = str(entry.get("status", "")).upper()
        expected_session_id = str(entry.get("session_id", "")).strip()
        if not expected_session_id:
            continue

        local = SESSIONS.get(pkg)

        if desired_status in {"EXPIRING", "STOPPING", "STOPPED"}:
            if local and local.get("session_id") == expected_session_id:
                await handle_stop_session(
                    {"session_id": expected_session_id, "package_name": pkg},
                    local_device_id,
                )
            continue

        if local:
            if local.get("session_id") == expected_session_id:
                continue

            if local.get("status") == "FAILED":
                # Session gagal lama tidak boleh memblokir assignment baru.
                old_task = _start_tasks.pop(pkg, None)
                if old_task is not None and not old_task.done():
                    old_task.cancel()
                old_scanner = _username_tasks.pop(pkg, None)
                if old_scanner is not None and not old_scanner.done():
                    old_scanner.cancel()
                SESSIONS.pop(pkg, None)
                local = None
            else:
                # Jangan silently adopt session baru di atas session lama.
                log.warning(
                    f"SESSION_AGENT: SYNC conflict {pkg}: local session "
                    f"{local.get('session_id')} != bot session {expected_session_id}. "
                    f"Local session dipertahankan."
                )
                continue

        pid = process_manager.get_pid(pkg)
        if not pid:
            log.warning(
                f"SESSION_AGENT: SYNC -- bot mengharapkan {pkg} "
                f"(session {expected_session_id}) tetapi proses tidak hidup; "
                f"TIDAK auto-launch."
            )
            continue

        expected_username = str(
            entry.get("expected_username")
            or entry.get("username")
            or ""
        ).strip()
        target = str(entry.get("target", "") or "").strip()

        if desired_status in running:
            internal_status = "ACTIVE"
            runtime_status = "RUNNING"
        elif desired_status in transitional:
            internal_status = "STARTING"
            runtime_status = desired_status
        else:
            internal_status = "STARTING"
            runtime_status = desired_status or "ASSIGNED"

        SESSIONS[pkg] = {
            "session_id": expected_session_id,
            "order_id": entry.get("order_id"),
            "_device_id": local_device_id,
            "target": target,
            "expected_username": expected_username,
            "session_kind": str(entry.get("session_kind", "") or "").strip().upper(),
            "status": internal_status,
            "runtime_status": runtime_status,
            "pid": pid,
            "launch_count": 1,
            "crash_count": 0,
            "status_seq": 0,
            "last_status_at": time.time(),
        }

        _ensure_username_scanner(pkg)
        if internal_status == "ACTIVE":
            _ensure_watchdog(pkg)
            _ensure_error_watcher()

        log.info(
            f"SESSION_AGENT: SYNC -- adopt {pkg} session "
            f"{expected_session_id}, pid={pid}, runtime={runtime_status}."
        )

    return _snapshot_for_sync()

