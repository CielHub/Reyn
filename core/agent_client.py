"""
Modul: agent_client.py
Tanggung Jawab: jadi jembatan CARRERA-HUB <-> Joki Control Bot lewat
WebSocket. REGISTER + HEARTBEAT + command session/test + SYNC_SESSIONS.

Lifecycle agent bersifat restartable, tetapi hanya boleh ada satu koneksi
WebSocket aktif untuk satu agent instance. Command task yang terikat ke
koneksi lama dibatalkan saat koneksi tersebut berakhir, sehingga callback
SESSION_STATUS tidak pernah mengirim lewat websocket stale.
"""
import asyncio
import json
import threading

try:
    import websockets
except ImportError:
    websockets = None

from core.logger import log
from core import session_agent
from core import test_agent
from core import username_scanner
from core import package_inventory
from core import process_manager

HEARTBEAT_INTERVAL_SECONDS = 15
RECONNECT_BACKOFF_SECONDS = [2, 5, 10, 20, 30]

_stats_ref = {"packages": {}}

_lock = threading.Lock()
_agent_loop = None
_agent_thread = None
_agent_main_task = None

_agent_state = {"status": "OFFLINE", "device_id": "", "reason": ""}

# Hanya websocket generation terbaru yang boleh dipakai sender runtime.
_connection_generation = 0
_active_ws = None


def set_stats_reference(stats: dict) -> None:
    _stats_ref["packages"] = stats


def get_agent_status() -> dict:
    return dict(_agent_state)


def _set_status(status: str, device_id=None, reason: str = "") -> None:
    _agent_state["status"] = status
    if device_id is not None:
        _agent_state["device_id"] = device_id
    _agent_state["reason"] = reason


def _snapshot_packages() -> dict:
    snapshot = {
        pkg: {
            "pid": "-",
            "state": "IDLE",
            "runtime_status": "IDLE",
            "username": username_scanner.get_cached_username(pkg),
        }
        for pkg in package_inventory.get_cached_packages()
    }

    for pkg, s in _stats_ref["packages"].items():
        snapshot[pkg] = {
            "pid": s.get("pid", "-"),
            "state": s.get("status", "UNKNOWN"),
            "runtime_status": s.get("runtime_status", s.get("status", "UNKNOWN")),
            "username": s.get("username") or username_scanner.get_cached_username(pkg),
        }

    snapshot.update(session_agent._snapshot_for_heartbeat())

    # LIVE RAM: RAM KESELURUHAN device (1x baca /proc/meminfo per heartbeat),
    # ditempel di tiap entry package sebagai field "ram". Dengan cara ini
    # tidak ada perubahan skema/ws_server: packages_json yang sudah
    # disimpan bot otomatis membawanya, dan bot membaca dari entry mana pun.
    try:
        ram = process_manager.get_ram_info()
    except Exception:
        ram = None
    if ram:
        for entry in snapshot.values():
            if isinstance(entry, dict):
                entry["ram"] = ram
    return snapshot


_COMMAND_HANDLERS = {
    "START_SESSION": session_agent.handle_start_session,
    "STOP_SESSION": session_agent.handle_stop_session,
    "SYNC_SESSIONS": session_agent.handle_sync_sessions,
    "RESTART_PACKAGE": session_agent.handle_restart_package,
    "TEST_AFK_DEVICE": test_agent.handle_test_afk,
    "STOP_TEST_AFK": test_agent.handle_stop_test_afk,
}


async def _handle_incoming_command(ws, msg: dict, device_id: str, task_registry: set) -> None:
    msg_type = msg.get("type")
    handler = _COMMAND_HANDLERS.get(msg_type)
    if not handler:
        log.info(f"AGENT: command '{msg_type}' belum didukung, diabaikan.")
        return

    try:
        result = await handler(msg, device_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.error(f"AGENT: exception saat proses command '{msg_type}'.", exc_info=True)
        return

    if result:
        try:
            await ws.send(json.dumps(result))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning(f"AGENT: gagal kirim COMMAND_RESULT untuk '{msg_type}' (koneksi mungkin putus).")


def _register_task(task: asyncio.Task, task_registry: set) -> None:
    task_registry.add(task)
    task.add_done_callback(task_registry.discard)


async def _run_agent(device_id: str, token: str, ws_url: str) -> None:
    global _connection_generation, _active_ws

    attempt = 0
    while True:
        try:
            _set_status("CONNECTING", device_id, "")

            async with websockets.connect(
                ws_url,
                open_timeout=10,
            ) as ws:
                _connection_generation += 1
                generation = _connection_generation
                _active_ws = ws

                await ws.send(json.dumps({
                    "type": "REGISTER",
                    "device_id": device_id,
                    "device_name": device_id,
                    "token": token,
                }))

                reply = json.loads(await ws.recv())
                if reply.get("type") != "REGISTER_OK":
                    reason = reply.get("reason", "")
                    log.error(f"AGENT: REGISTER ditolak bot ({reason}). Cek DEVICE_ID/DEVICE_TOKEN di config.conf.")
                    _set_status("AUTH_FAILED", device_id, reason)
                    await asyncio.sleep(30)
                    continue

                log.info(f"AGENT: Terhubung ke Joki Control Bot sebagai '{device_id}'.")
                _set_status("ONLINE", device_id, "")
                attempt = 0

                async def _session_status_sender(payload: dict) -> None:
                    if generation != _connection_generation or _active_ws is not ws:
                        raise ConnectionError("STALE_WEBSOCKET")
                    await ws.send(json.dumps(payload))

                session_agent.register_sender(_session_status_sender)
                test_agent.register_sender(_session_status_sender)

                command_tasks = set()
                connection_tasks = [
                    asyncio.create_task(_heartbeat_loop(ws, device_id)),
                    asyncio.create_task(_receive_loop(ws, device_id, command_tasks)),
                    asyncio.create_task(_package_inventory_loop()),
                ]

                try:
                    await asyncio.gather(*connection_tasks)
                finally:
                    for task in list(command_tasks):
                        if not task.done():
                            task.cancel()
                    for task in connection_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*connection_tasks, return_exceptions=True)
                    if command_tasks:
                        await asyncio.gather(*command_tasks, return_exceptions=True)
                    if _active_ws is ws:
                        _active_ws = None

        except asyncio.CancelledError:
            _set_status("OFFLINE", device_id, "dihentikan")
            raise
        except Exception as e:
            delay = RECONNECT_BACKOFF_SECONDS[min(attempt, len(RECONNECT_BACKOFF_SECONDS) - 1)]
            log.warning(
                f"AGENT: Koneksi ke bot terputus/gagal ({e}). Retry dalam {delay}s. "
                "Monitoring/recovery lokal TETAP JALAN NORMAL."
            )
            _set_status("RECONNECTING", device_id, str(e))
            attempt += 1
            await asyncio.sleep(delay)


async def _package_inventory_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(package_inventory.scan_installed_packages_blocking)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("AGENT: exception tak terduga saat package inventory scan.", exc_info=True)
        await asyncio.sleep(package_inventory.INVENTORY_SCAN_INTERVAL_SECONDS)


def _safe_snapshot_packages() -> dict:
    """Exception saat menyusun snapshot TIDAK boleh mematikan heartbeat (dulu
    exception di sini membunuh task heartbeat -> koneksi putus -> REGISTER
    ulang -> packages_json direset -> panel 'Tidak terpantau'). Kalau snapshot
    penuh gagal, kirim versi minimal: inventory + RAM."""
    try:
        return _snapshot_packages()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.error("AGENT: gagal menyusun snapshot heartbeat penuh; kirim versi minimal.", exc_info=True)
    try:
        minimal = {
            pkg: {"pid": "-", "state": "IDLE", "runtime_status": "IDLE", "username": None}
            for pkg in package_inventory.get_cached_packages()
        }
        ram = process_manager.get_ram_info()
        if ram:
            for entry in minimal.values():
                entry["ram"] = ram
        return minimal
    except Exception:
        log.error("AGENT: snapshot minimal juga gagal; heartbeat kosong.", exc_info=True)
        return {}


async def _heartbeat_loop(ws, device_id: str) -> None:
    while True:
        await ws.send(json.dumps({
            "type": "HEARTBEAT",
            "device_id": device_id,
            "packages": _safe_snapshot_packages(),
        }))
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def _receive_loop(ws, device_id: str, command_tasks: set) -> None:
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("AGENT: pesan dari bot bukan JSON valid, diabaikan.")
            continue

        task = asyncio.create_task(_handle_incoming_command(ws, msg, device_id, command_tasks))
        _register_task(task, command_tasks)

    # Jika async-for berakhir tanpa exception, peer sudah menutup websocket.
    raise ConnectionError("WEBSOCKET_CLOSED")


def stop_agent_background(timeout: float = 5.0) -> None:
    global _agent_loop, _agent_thread, _agent_main_task, _active_ws

    with _lock:
        loop, thread, task = _agent_loop, _agent_thread, _agent_main_task

    if not thread or not thread.is_alive():
        return

    if loop is not None and task is not None:
        loop.call_soon_threadsafe(task.cancel)

    thread.join(timeout=timeout)
    if thread.is_alive():
        log.error(
            "AGENT: thread lama tidak berhenti dalam batas waktu. "
            "Agent baru TIDAK dimulai untuk mencegah dua koneksi WS bersamaan."
        )
        return

    with _lock:
        _agent_loop = None
        _agent_thread = None
        _agent_main_task = None
    _active_ws = None
    _set_status("OFFLINE", reason="dihentikan")


def start_agent_background(device_id: str, token: str, ws_url: str) -> None:
    global _agent_loop, _agent_thread, _agent_main_task

    if websockets is None:
        log.warning(
            "AGENT: library 'websockets' belum terpasang (pip install websockets). "
            "Fitur integrasi Joki Bot dilewati."
        )
        _set_status("OFFLINE", device_id or "", "websockets belum terpasang")
        return

    stop_agent_background()

    with _lock:
        if _agent_thread is not None and _agent_thread.is_alive():
            _set_status("OFFLINE", device_id or "", "thread agent lama belum berhenti")
            return

    if not device_id or not token or not ws_url:
        log.info("AGENT: DEVICE_ID/DEVICE_TOKEN/BOT_WS_URL belum lengkap. Fitur integrasi dilewati.")
        _set_status("OFFLINE", device_id or "", "DEVICE_ID/TOKEN belum diisi")
        return

    def _thread_target():
        global _agent_loop, _agent_main_task
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with _lock:
            _agent_loop = loop
        try:
            task = loop.create_task(_run_agent(device_id, token, ws_url))
            with _lock:
                _agent_main_task = task
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.error("AGENT: thread agent berhenti karena error tak terduga.", exc_info=True)
        finally:
            with _lock:
                if _agent_loop is loop:
                    _agent_loop = None
                if _agent_main_task is not None and _agent_main_task.done():
                    _agent_main_task = None
            loop.close()

    t = threading.Thread(target=_thread_target, name="JokiAgentClient", daemon=True)
    with _lock:
        _agent_thread = t
    _set_status("CONNECTING", device_id, "")
    t.start()
    log.info(f"AGENT: thread agent dimulai, mencoba konek ke {ws_url} ...")
