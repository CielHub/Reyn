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
import uuid
import threading

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
# --- Package "hidup tapi tidak benar-benar jalan" (proses cached) ---
# PID masih ada TAPI proses sudah CACHED (oom_score_adj >= 900): jendela Roblox
# ditutup / aplikasi di-close dari layar. Dianggap mati hanya setelah N tick
# berturut-turut + konfirmasi ulang (menghindari false positive yang akan
# me-kill proses sehat).
WATCHDOG_CACHED_ADJ_MIN = 900
WATCHDOG_CACHED_TICKS_BEFORE_RECOVERY = 3
WATCHDOG_POST_STATUS_GRACE_SECONDS = 60
# Log diagnostik per package per tick (SEMENTARA, untuk debugging).
# Set False kalau sudah tidak diperlukan.
WATCHDOG_DEBUG_LOG = True

# DIAGNOSTIC MODE: ketika aktif, event error hanya melakukan exact-PID SIGTERM
# dan berhenti di sana. Tidak ada automatic recovery dan watchdog tidak boleh
# memulai recovery. Ini sengaja dibuat sebagai mode uji untuk membuktikan
# apakah masalah terjadi pada jalur kill atau pada jalur recovery/launch.
# Release diagnostik ini default ON; caller utama dapat mengubahnya lewat
# configure_runtime() sebelum agent background dimulai.
KILL_ONLY_TEST_MODE = False
# DIAGNOSTIC PHASE B: kill exact target PID, then launch target package only.
# No join/rejoin, watchdog recovery, retry, sibling restore, or sibling launch.
KILL_THEN_LAUNCH_TEST_MODE = True

# Pastikan PID lama benar-benar mati: berapa kali kill ulang kalau masih ada.
KILL_VERIFY_MAX_ROUNDS = 3
WATCHDOG_ALIVE_LOG_EVERY_SECONDS = 300
MAX_RECOVERY_ATTEMPTS = 10
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

def configure_runtime(*, kill_only_test_mode=None, kill_then_launch_test_mode=None) -> None:
    """Configure runtime diagnostics before the agent thread starts.

    Diagnostic modes keep ErrorDetector active but stop normal recovery.
    KILL_ONLY_TEST_MODE performs exact-PID SIGTERM only.
    KILL_THEN_LAUNCH_TEST_MODE performs exact-PID SIGTERM followed by a
    target-only lobby launch, then stops.
    """
    global KILL_ONLY_TEST_MODE, KILL_THEN_LAUNCH_TEST_MODE
    if kill_only_test_mode is not None:
        KILL_ONLY_TEST_MODE = bool(kill_only_test_mode)
    if kill_then_launch_test_mode is not None:
        KILL_THEN_LAUNCH_TEST_MODE = bool(kill_then_launch_test_mode)
    if KILL_ONLY_TEST_MODE and KILL_THEN_LAUNCH_TEST_MODE:
        log.warning(
            "SESSION_AGENT: both diagnostic modes enabled; "
            "KILL_THEN_LAUNCH_TEST_MODE takes precedence."
        )
    log.warning(
        "SESSION_AGENT: KILL_ONLY_TEST_MODE=%s KILL_THEN_LAUNCH_TEST_MODE=%s",
        KILL_ONLY_TEST_MODE,
        KILL_THEN_LAUNCH_TEST_MODE,
    )


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
_start_cancel_events: dict = {}  # pkg -> threading.Event used by blocking workers
_package_locks: dict = {}         # pkg -> asyncio.Lock for lifecycle mutations
_device_restore_locks: dict = {}  # device -> lock for sibling/window restores
_STOP_TASKS: dict = {}             # pkg -> stop cleanup task

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


def _get_package_lock(pkg: str) -> asyncio.Lock:
    lock = _package_locks.get(pkg)
    if lock is None:
        lock = asyncio.Lock()
        _package_locks[pkg] = lock
    return lock


def _get_cancel_event(pkg: str):
    return _start_cancel_events.get(pkg)


def _get_device_restore_lock(device_id: str) -> asyncio.Lock:
    key = str(device_id or "").strip().upper() or "__UNKNOWN_DEVICE__"
    lock = _device_restore_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _device_restore_locks[key] = lock
    return lock


async def _sleep_or_cancel(seconds: float, cancel_event=None) -> bool:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(1.0, remaining))


async def _kill_package_and_verify(pkg: str, max_rounds: int = KILL_VERIFY_MAX_ROUNDS) -> bool:
    """Compatibility helper for explicit terminal package cleanup only.

    IMPORTANT: automatic error recovery must use `_kill_exact_pid_and_verify()`
    and must never sweep all PIDs belonging to a package. This helper remains
    available for START/STOP terminal cleanup paths where package-wide cleanup
    is explicitly requested.
    """
    pkg = str(pkg or "").strip()
    if not process_manager.is_valid_package_name(pkg):
        return False
    for _ in range(max(1, int(max_rounds))):
        pids = await asyncio.to_thread(process_manager.get_pids, pkg)
        if pids == set():
            return True
        if pids is None:
            await asyncio.sleep(0.5)
            continue
        for pid in sorted(pids):
            identity = await asyncio.to_thread(process_manager.get_process_identity, pid)
            if not identity or identity.get("package") != pkg:
                continue
            await asyncio.to_thread(
                process_manager.kill_pid_direct,
                pid, package=pkg, expected_start_time=identity.get("start_time")
            )
        await asyncio.sleep(0.2)
    final = await asyncio.to_thread(process_manager.get_pids, pkg)
    return final == set()


async def _kill_exact_pid_and_verify(pkg: str, pid: str, expected_start_time=None) -> bool:
    """Terminate EXACTLY one process selected by the triggering recovery event.

    No package-wide PID enumeration, no sibling interaction, no SIGKILL and no
    force-stop. If the original PID is already gone, it is considered safe to
    continue recovery because there is nothing left to terminate. If the PID
    was reused by another process identity, refuse to touch it.
    """
    pkg = str(pkg or "").strip()
    pid = str(pid or "").strip()
    if not process_manager.is_valid_package_name(pkg) or not pid.isdigit():
        return False

    identity = await asyncio.to_thread(process_manager.get_process_identity, pid)
    if identity is None:
        # Target is already gone. Do NOT search for a replacement PID.
        exists = await asyncio.to_thread(process_manager.pid_exists, pid)
        return not exists

    if identity.get("package") != pkg:
        log.error(
            f"[PID_OWNERSHIP] {pkg}: exact recovery pid={pid} belongs to "
            f"{identity.get('package')!r}; refusing to kill."
        )
        return False

    actual_start = identity.get("start_time")
    if expected_start_time is not None:
        try:
            if int(actual_start) != int(expected_start_time):
                log.warning(
                    f"[PID_OWNERSHIP] {pkg}: exact recovery pid={pid} start_time "
                    f"changed {expected_start_time} -> {actual_start}; refusing to kill."
                )
                return False
        except (TypeError, ValueError):
            return False

    return await asyncio.to_thread(
        process_manager.kill_pid_direct,
        pid,
        package=pkg,
        expected_start_time=actual_start,
    )


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
            "username_verified": bool(username_scanner.get_cached_identity(pkg).get("read_ok")),
            "target": info.get("target"),
            "pid": info.get("pid", "-"),
            "timestamp": info["last_status_at"],
            "status_seq": seq,
        }
    else:
        payload = {
            "username": username_scanner.get_cached_username(pkg),
            "username_verified": bool(username_scanner.get_cached_identity(pkg).get("read_ok")),
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
    """Heartbeat operasional dengan metadata freshness/liveness package.

    `snapshot_at` diproduksi SEKALI per heartbeat dari clock device, sehingga
    bot dapat mengukur umur `last_pid_check_at` tanpa bergantung pada clock server.
    """
    snapshot = {}
    snapshot_at = time.time()
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
            "pid_start_time": info.get("pid_start_time"),
            "state": info.get("runtime_status") or info.get("status", "UNKNOWN"),
            "username": username_scanner.get_cached_username(pkg),
            "username_verified": username_scanner.get_cached_identity(pkg).get("read_ok", False),
            "username_scanned_at": username_scanner.get_cached_identity(pkg).get("scanned_at", 0),
            "session_id": info.get("session_id", ""),
            "last_status_at": info.get("last_status_at"),
            # Tambahan (additive, bot lama mengabaikan): hasil cek PID nyata
            # terakhir -- sumber kebenaran liveness, bukan status internal.
            "pid_alive": info.get("pid_alive"),
            "package_alive": info.get("package_alive"),
            "adj": info.get("adj"),
            "liveness_state": info.get("liveness_state", "UNKNOWN"),
            "last_pid_check_at": info.get("last_pid_check_at"),
            "snapshot_at": snapshot_at,
        }
    return snapshot

def _snapshot_for_sync() -> dict:
    """Snapshot lengkap untuk SYNC_SESSIONS, termasuk runtime identity + liveness."""
    snapshot_at = time.time()
    return {
        pkg: {
            "session_id": info.get("session_id", ""),
            "status": info.get("runtime_status") or info.get("status", "UNKNOWN"),
            "pid": info.get("pid", "-"),
            "pid_start_time": info.get("pid_start_time"),
            "username": username_scanner.get_cached_username(pkg),
            "username_verified": username_scanner.get_cached_identity(pkg).get("read_ok", False),
            "username_scanned_at": username_scanner.get_cached_identity(pkg).get("scanned_at", 0),
            "expected_username": info.get("expected_username", ""),
            "target": info.get("target", ""),
            "last_status_at": info.get("last_status_at"),
            "status_seq": info.get("status_seq", 0),
            "pid_alive": info.get("pid_alive"),
            "package_alive": info.get("package_alive"),
            "adj": info.get("adj"),
            "liveness_state": info.get("liveness_state", "UNKNOWN"),
            "last_pid_check_at": info.get("last_pid_check_at"),
            "snapshot_at": snapshot_at,
        }
        for pkg, info in SESSIONS.items()
    }

async def handle_start_session(msg: dict, local_device_id: str) -> dict:
    """Accept START_SESSION idempotently and reserve exactly one package.

    Reservation and lifecycle ownership are decided under the per-package lock
    so two concurrent START requests cannot both observe a free package.
    """
    session_id = str(msg.get("session_id", "")).strip()
    order_id = msg.get("order_id")
    pkg = str(msg.get("package_name", "")).strip()
    target = str(msg.get("target", "")).strip()
    expected_username = str(msg.get("expected_username", "") or "").strip()

    try:
        timeout_seconds = int(msg.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    timeout_seconds = max(15, min(timeout_seconds, 300))

    if not session_id or not pkg or not target:
        return {"type":"COMMAND_RESULT","command":"START_SESSION","device_id":local_device_id,
                "session_id":session_id,"ok":False,"reason":"MISSING_FIELDS"}
    if not process_manager.is_valid_package_name(pkg):
        return {"type":"COMMAND_RESULT","command":"START_SESSION","device_id":local_device_id,
                "session_id":session_id,"ok":False,"reason":"INVALID_PACKAGE"}

    try:
        intent_url = get_intent_url(target)
    except ValueError as e:
        return {"type":"COMMAND_RESULT","command":"START_SESSION","device_id":local_device_id,
                "session_id":session_id,"ok":False,"reason":f"INVALID_TARGET: {e}"}

    async with _get_package_lock(pkg):
        existing = SESSIONS.get(pkg)
        if existing is not None:
            existing_session = str(existing.get("session_id", ""))
            status = str(existing.get("status") or "").upper()
            if existing_session != session_id:
                log.warning(
                    f"SESSION_AGENT: START_SESSION untuk {pkg} ditolak karena masih dipegang "
                    f"session '{existing_session}'. Request baru '{session_id}' tidak boleh menimpa."
                )
                return {"type":"COMMAND_RESULT","command":"START_SESSION","device_id":local_device_id,
                        "session_id":session_id,"ok":False,"reason":"PACKAGE_BUSY"}

            task = _start_tasks.get(pkg)
            if task is not None and not task.done():
                return {"type":"COMMAND_RESULT","command":"START_SESSION","device_id":local_device_id,
                        "session_id":session_id,"ok":True,"reason":"ALREADY_PROCESSING"}
            if status in {"ACTIVE", "RECOVERING", "STARTING", "STOPPING"}:
                return {"type":"COMMAND_RESULT","command":"START_SESSION","device_id":local_device_id,
                        "session_id":session_id,"ok":True,"reason":"ALREADY_PROCESSING"}
            if status not in {"FAILED", "START_FAILED", "STOPPED"}:
                return {"type":"COMMAND_RESULT","command":"START_SESSION","device_id":local_device_id,
                        "session_id":session_id,"ok":False,"reason":"PACKAGE_BUSY"}

            # Terminal local entry is safe to replace, provided it is not still
            # in STOPPING. A STOPPING session remains the owner until STOPPED.
            SESSIONS.pop(pkg, None)
            _FREEFORM_REGISTRY.pop(pkg, None)
            username_scanner.forget(pkg)

        _start_cancel_events[pkg] = threading.Event()
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
            "pid_alive": None,
            "package_alive": None,
            "adj": None,
            "liveness_state": "UNKNOWN",
            "last_pid_check_at": None,
            "pid_start_time": None,
        }

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
        f"SESSION_AGENT: START_SESSION diterima -> {pkg} (session {session_id}), "
        f"expected_username={expected_username or '(tidak divalidasi)'}.",
    )
    return {"type":"COMMAND_RESULT","command":"START_SESSION","device_id":local_device_id,
            "session_id":session_id,"ok":True,"reason":"PROCESSING"}

async def _wait_in_lobby(pkg: str, session_id: str, *, recovery: bool = False, cancel_event=None) -> bool:
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
    if not await _sleep_or_cancel(delay, cancel_event):
        return False

    current = SESSIONS.get(pkg)
    return bool(current and str(current.get("session_id")) == str(session_id))


async def _wait_before_map_entry(pkg: str, session_id: str, *, cancel_event=None) -> bool:
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
    if not await _sleep_or_cancel(delay, cancel_event):
        return False

    current = SESSIONS.get(pkg)
    return bool(current and str(current.get("session_id")) == str(session_id))


async def _fail_start_session(
    local_device_id: str,
    session_id: str,
    pkg: str,
    order_id,
    reason: str,
) -> None:
    """Fail one start attempt and clean only that package before START_FAILED."""
    current = SESSIONS.get(pkg)
    if (not current or str(current.get("session_id")) != str(session_id)
            or str(current.get("status") or "").upper() in {"STOPPING", "STOPPED"}):
        return

    cancel_event = _start_cancel_events.get(pkg)
    if cancel_event is not None:
        cancel_event.set()
    current["status"] = "STOPPING"
    current["runtime_status"] = "STOPPING"
    await _emit_status(
        local_device_id, session_id, pkg, order_id,
        "STOPPING", reason=f"START_FAILURE_CLEANUP:{reason}",
    )

    killed = await _kill_package_and_verify(pkg)
    current = SESSIONS.get(pkg)
    if (not current or str(current.get("session_id")) != str(session_id)
            or str(current.get("status") or "").upper() in {"STOPPING", "STOPPED"}):
        return

    current["status"] = "FAILED"
    current["runtime_status"] = "FAILED"
    current["pid"] = "-"
    current["pid_alive"] = not killed
    current["package_alive"] = not killed
    if not killed:
        log.error(
            f"SESSION_AGENT: start failure cleanup {pkg}/{session_id} gagal memastikan "
            "process benar-benar mati; START_FAILED tetap dilaporkan dengan cleanup_failed=True."
        )
    username_scanner.forget(pkg)
    await _emit_status(
        local_device_id, session_id, pkg, order_id,
        "START_FAILED",
        reason=reason,
        cleanup_failed=not killed,
    )

async def _run_start_flow(local_device_id: str, session_id: str, pkg: str, order_id,
                           intent_url: str, expected_username: str, timeout_seconds: int) -> None:
    """PREPARING -> WAITING_LOGIN -> ACCOUNT_READY -> JOINING_GAME -> RUNNING."""
    cancel_event = _start_cancel_events.get(pkg)

    def _still_current() -> bool:
        current = SESSIONS.get(pkg)
        if not current or current.get("session_id") != session_id:
            return False
        if str(current.get("status") or "").upper() in {"STOPPING", "STOPPED", "FAILED", "START_FAILED"}:
            return False
        return not (cancel_event is not None and cancel_event.is_set())

    try:
        if cancel_event and cancel_event.is_set():
            return
        await _emit_status(local_device_id, session_id, pkg, order_id, "PREPARING")

        try:
            lobby_ok = await _launch_then_freeform_soon(
                pkg, get_lobby_intent(), LOBBY_TIMEOUT_SECONDS,
                require_join_signal=False, session_id=session_id, cancel_event=cancel_event,
            )
        except Exception:
            log.error(f"SESSION_AGENT: exception saat buka lobby {pkg}.", exc_info=True)
            lobby_ok = False

        if not _still_current():
            return
        if not lobby_ok:
            await _fail_start_session(local_device_id, session_id, pkg, order_id, "LOBBY_LAUNCH_FAILED")
            return

        SESSIONS[pkg]["pid"] = get_pid_quick(pkg) or "-"

        if not await _wait_in_lobby(pkg, session_id, cancel_event=cancel_event):
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
                    scan_result = await asyncio.to_thread(
                        username_scanner.scan_username_result_blocking, pkg
                    )
                    detected = scan_result.get("username") if scan_result.get("read_ok") else None
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

                if not await _sleep_or_cancel(LOGIN_POLL_INTERVAL_SECONDS, cancel_event):
                    return
            else:
                if not _still_current():
                    return
                await _fail_start_session(local_device_id, session_id, pkg, order_id, "LOGIN_TIMEOUT")
                return

            if not _still_current():
                return
            SESSIONS[pkg]["runtime_status"] = "ACCOUNT_READY"
            await _emit_status(
                local_device_id, session_id, pkg, order_id,
                "ACCOUNT_READY", detected_username=detected,
            )

        await _emit_status(local_device_id, session_id, pkg, order_id, "JOINING_GAME")

        if not await _wait_before_map_entry(pkg, session_id, cancel_event=cancel_event):
            return

        try:
            join_status, join_reason = await _launch_then_freeform_soon(
                pkg, intent_url, timeout_seconds,
                require_join_signal=True, session_id=session_id, cancel_event=cancel_event,
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
            await _fail_start_session(local_device_id, session_id, pkg, order_id, f"JOIN_FAILED:{join_reason}")
            return

        if join_status == "UNCERTAIN":
            await _emit_status(
                local_device_id, session_id, pkg, order_id,
                "VERIFYING_GAME", reason=join_reason,
            )

            grace_start_str = datetime.datetime.now().strftime("%m-%d %H:%M:%S.000")
            if not await _sleep_or_cancel(JOIN_VERIFY_GRACE_SECONDS, cancel_event):
                return

            if not _still_current():
                return

            pid_after_grace = get_pid_quick(pkg)
            if not pid_after_grace:
                await _fail_start_session(local_device_id, session_id, pkg, order_id, "PROCESS_DIED_DURING_VERIFY")
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
                await _fail_start_session(
                    local_device_id, session_id, pkg, order_id,
                    f"JOIN_ERROR_SIGNAL_DURING_VERIFY_{failure_code}",
                )
                return

        if not _still_current():
            return

        await _activate_freeform_for_target(pkg, session_id)
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
            await _fail_start_session(
                local_device_id, session_id, pkg, order_id, "UNHANDLED_EXCEPTION"
            )

async def _activate_freeform_for_target(pkg: str, session_id: str) -> None:
    """Best-effort Freeform activation for ONE target package.

    Isolation rule: this function ONLY changes the target package's windowing
    state. It never brings sibling packages to foreground. Survivor restoration
    is handled separately after a target kill/recovery operation completes.

    On Android 10 ``activate_freeform()`` is expected to be unavailable; that
    is cosmetic and must never trigger any operation on another package.
    """
    try:
        ok, task_id = await asyncio.to_thread(activate_freeform, pkg)
    except Exception:
        log.error(
            f"SESSION_AGENT: exception saat activate_freeform {pkg}.",
            exc_info=True,
        )
        ok, task_id = False, None

    current = SESSIONS.get(pkg)
    if not current or str(current.get("session_id")) != str(session_id):
        return
    if str(current.get("status") or "").upper() in {
        "STOPPING", "STOPPED", "FAILED", "START_FAILED"
    }:
        return

    if not ok:
        log.warning(
            f"SESSION_AGENT: {pkg} (session {session_id}) Freeform tidak "
            f"terverifikasi (taskId={task_id or '-'}); lanjut tanpa Freeform."
        )
        return

    _FREEFORM_REGISTRY[pkg] = {
        "session_id": session_id,
        "task_id": task_id,
        "state": "VISIBLE",
    }
    log.info(
        f"SESSION_AGENT: {pkg} (session {session_id}) Freeform terverifikasi "
        f"tampil (taskId={task_id})."
    )


async def _launch_then_freeform_soon(
    pkg: str,
    intent_url: str,
    timeout_seconds: int,
    require_join_signal: bool,
    session_id: str,
    only_if_previously_freeform: bool = False,
    cancel_event=None,
):
    """Launch normal -> timer Freeform -> Smart Wait secara paralel."""
    if cancel_event is not None and cancel_event.is_set():
        return (("FAILED", "CANCELLED") if require_join_signal else False)

    ok, start_time_str = await asyncio.to_thread(
        launch_normal, pkg, intent_url
    )
    if cancel_event is not None and cancel_event.is_set():
        return (("FAILED", "CANCELLED") if require_join_signal else False)
    if not ok:
        return (
            ("FAILED", "LAUNCH_COMMAND_FAILED")
            if require_join_signal else False
        )

    async def _freeform_after_delay():
        if only_if_previously_freeform and pkg not in _FREEFORM_REGISTRY:
            return
        if not await _sleep_or_cancel(FREEFORM_ACTIVATION_DELAY_SECONDS, cancel_event):
            return
        try:
            await _activate_freeform_for_target(pkg, session_id)
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
            cancel_event,
        )
    finally:
        if not freeform_task.done():
            freeform_task.cancel()
        try:
            await freeform_task
        except asyncio.CancelledError:
            pass
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

async def _snapshot_survivor_pids(exclude_pkg: str) -> dict:
    """Snapshot PID set package ACTIVE lain sebelum operasi target."""
    survivors = [
        pkg for pkg, info in list(SESSIONS.items())
        if pkg != exclude_pkg and info.get("status") == "ACTIVE"
    ]
    if not survivors:
        return {}
    results = await asyncio.gather(
        *(asyncio.to_thread(process_manager.get_pids, pkg) for pkg in survivors),
        return_exceptions=True,
    )
    snapshot = {}
    for pkg, pids in zip(survivors, results):
        if isinstance(pids, Exception):
            log.warning(f"[ISOLATION_CHECK] snapshot survivor={pkg}: probe exception: {pids!r}")
            snapshot[pkg] = None
        else:
            snapshot[pkg] = None if pids is None else set(pids)
    return snapshot


async def _verify_survivor_pids_unchanged(snapshot: dict, phase: str) -> None:
    """Detect unexpected PID changes in packages that were not targeted."""
    if not snapshot:
        return
    packages = list(snapshot)
    results = await asyncio.gather(
        *(asyncio.to_thread(process_manager.get_pids, pkg) for pkg in packages),
        return_exceptions=True,
    )
    for survivor_pkg, before, current in zip(packages, (snapshot[p] for p in packages), results):
        if isinstance(current, Exception) or before is None or current is None:
            log.warning(
                f"[ISOLATION_CHECK] {phase} survivor={survivor_pkg}: "
                "PID state UNKNOWN, tidak bisa membuktikan unchanged."
            )
            continue
        current = set(current)
        if current != before:
            log.error(
                f"[CROSS_PACKAGE_IMPACT] {phase} survivor={survivor_pkg}: "
                f"PID berubah {sorted(before)} -> {sorted(current)}."
            )
        else:
            log.info(
                f"[ISOLATION_CHECK] {phase} survivor={survivor_pkg}: "
                f"PID unchanged {sorted(current)}."
            )


async def _restore_survivors_after_target_operation(
    local_device_id: str,
    exclude_pkg: str,
    survivor_snapshot: dict,
    phase: str,
) -> None:
    """Restore ONLY non-target active packages after a target lifecycle operation.

    Android/Zetsu may remove floating windows for sibling packages when one
    target task is closed. This helper treats window restoration as a separate
    concern from process killing:
      - never sends a kill/force-stop to a survivor;
      - brings each survivor to foreground one-by-one;
      - verifies the survivor PID set after the restore attempt;
      - logs cross-package process impact explicitly if the PID set changed.

    A survivor process that disappeared is never re-created here. That is
    deliberately a diagnostic boundary: automatic rejoin of a non-target
    package would violate package isolation and must be a separate, explicit
    recovery decision.
    """
    if not survivor_snapshot:
        return

    async with _get_device_restore_lock(local_device_id):
        for survivor_pkg, before in survivor_snapshot.items():
            if survivor_pkg == exclude_pkg:
                continue
            info = SESSIONS.get(survivor_pkg)
            if not info or info.get("status") != "ACTIVE":
                continue

            if before is None:
                log.warning(
                    f"[ISOLATION_CHECK] {phase} survivor={survivor_pkg}: "
                    "snapshot UNKNOWN, skip foreground restore otomatis agar state "
                    "tidak diasumsikan aman."
                )
                continue

            before = set(before)
            current_before_restore = await asyncio.to_thread(
                process_manager.get_pids, survivor_pkg
            )
            if current_before_restore is None:
                log.warning(
                    f"[ISOLATION_CHECK] {phase} survivor={survivor_pkg}: "
                    "PID probe UNKNOWN sebelum restore; skip agar restore_foreground "
                    "tidak berpotensi meluncurkan package yang tidak terverifikasi."
                )
                continue

            current_before_restore = set(current_before_restore)
            if not current_before_restore:
                log.error(
                    f"[CROSS_PACKAGE_IMPACT] {phase} survivor={survivor_pkg}: "
                    f"PID survivor hilang sebelum restore (expected={sorted(before)}). "
                    "TIDAK menjalankan monkey/auto-rejoin pada survivor."
                )
                continue

            log.info(
                f"[SURVIVOR_RESTORE] phase={phase} target={exclude_pkg} "
                f"survivor={survivor_pkg} before_pids={sorted(before)} "
                f"current_pids={sorted(current_before_restore)}."
            )

            restored = await asyncio.to_thread(
                process_manager.restore_foreground, survivor_pkg
            )
            log.info(
                f"[SURVIVOR_RESTORE] phase={phase} survivor={survivor_pkg}: "
                f"foreground={'OK' if restored else 'FAILED'}."
            )
            await asyncio.sleep(_FINISH_RESTORE_STEP_DELAY_SECONDS)

            after = await asyncio.to_thread(process_manager.get_pids, survivor_pkg)
            if after is None:
                log.warning(
                    f"[ISOLATION_CHECK] {phase} survivor={survivor_pkg}: "
                    "post-restore PID probe UNKNOWN."
                )
                continue

            after = set(after)
            if after == before:
                log.info(
                    f"[ISOLATION_CHECK] {phase} survivor={survivor_pkg}: "
                    f"PID unchanged {sorted(after)}."
                )
            else:
                log.error(
                    f"[CROSS_PACKAGE_IMPACT] {phase} survivor={survivor_pkg}: "
                    f"PID berubah {sorted(before)} -> {sorted(after)}. "
                    "Target operation tidak mengirim kill ke survivor; perubahan "
                    "dianggap efek eksternal dan TIDAK dilakukan auto-rejoin."
                )


async def _finish_kill_and_restore_survivors(local_device_id: str, session_id: str, pkg: str, recorded_pid: str, order_id=None) -> None:
    log.info(f"[FINISH] session={session_id} package={pkg} -- mulai stop.")
    try:
        survivor_snapshot = await _snapshot_survivor_pids(pkg)
        killed = await _kill_package_and_verify(pkg)
        if not killed:
            await _emit_status(local_device_id, session_id, pkg, order_id, "STOPPING",
                               pid=recorded_pid, reason="PROCESS_STILL_ALIVE")
            log.error(f"[FINISH] {pkg}/{session_id} gagal diverifikasi mati.")
            return

        await _sleep_or_cancel(_FINISH_RESTORE_DELAY_SECONDS)
        await _restore_survivors_after_target_operation(
            local_device_id,
            pkg,
            survivor_snapshot,
            phase=f"finish/{pkg}",
        )

        async with _get_package_lock(pkg):
            current = SESSIONS.get(pkg)
            if current is None or str(current.get("session_id")) != session_id:
                return
            current["status"] = "STOPPED"
            current["runtime_status"] = "STOPPED"
            current["pid"] = "-"
            current["pid_alive"] = False
            current["package_alive"] = False
            stopped_seq = int(current.get("status_seq", 0)) + 1
            current["status_seq"] = stopped_seq
            # Remove ownership BEFORE emitting the terminal event. This closes
            # the tiny race where a retrying START could replace the old entry
            # between STOPPED emission and cleanup, after which a late pop could
            # accidentally delete the brand-new session.
            SESSIONS.pop(pkg, None)
            _FREEFORM_REGISTRY.pop(pkg, None)
            _start_cancel_events.pop(pkg, None)
            username_scanner.forget(pkg)

        await _emit_status(
            local_device_id, session_id, pkg, order_id, "STOPPED",
            pid="-", status_seq=stopped_seq, username_verified=False,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.error(f"SESSION_AGENT: finish stop exception {pkg}/{session_id}.", exc_info=True)

async def handle_stop_session(msg: dict, local_device_id: str, do_reset: bool = True) -> dict:
    """Transition one package to STOPPING and asynchronously prove STOPPED.

    The immediate command result means "stop accepted", not "process is dead".
    The authoritative terminal STOPPED event is emitted only after all target
    PIDs disappear (or the command is reported failed).
    """
    session_id = str(msg.get("session_id", "")).strip()
    pkg = str(msg.get("package_name", "")).strip()
    if not session_id or not pkg:
        return {"type":"COMMAND_RESULT","command":"STOP_SESSION","device_id":local_device_id,"session_id":session_id,"ok":False,"reason":"MISSING_FIELDS"}
    if not process_manager.is_valid_package_name(pkg):
        return {"type":"COMMAND_RESULT","command":"STOP_SESSION","device_id":local_device_id,"session_id":session_id,"ok":False,"reason":"INVALID_PACKAGE"}

    async with _get_package_lock(pkg):
        info = SESSIONS.get(pkg)
        if info is None:
            return {"type":"COMMAND_RESULT","command":"STOP_SESSION","device_id":local_device_id,"session_id":session_id,"ok":True,"reason":"NO_LOCAL_SESSION"}
        if str(info.get("session_id")) != session_id:
            return {"type":"COMMAND_RESULT","command":"STOP_SESSION","device_id":local_device_id,"session_id":session_id,"ok":False,"reason":"STALE_SESSION_ID"}
        if info.get("status") == "STOPPING":
            return {"type":"COMMAND_RESULT","command":"STOP_SESSION","device_id":local_device_id,"session_id":session_id,"ok":True,"reason":"ALREADY_STOPPING"}

        info["status"] = "STOPPING"
        info["runtime_status"] = "STOPPING"
        target_pid = info.get("pid", "-")
        cancel_event = _start_cancel_events.get(pkg)
        if cancel_event is not None:
            cancel_event.set()
        for registry, pop_only in ((_watchdog_tasks, True), (_username_tasks, True), (_start_tasks, True)):
            task = registry.pop(pkg, None)
            if task is not None and not task.done():
                task.cancel()
        _FREEFORM_REGISTRY.pop(pkg, None)
        username_scanner.forget(pkg)

        await _emit_status(local_device_id, session_id, pkg, info.get("order_id"), "STOPPING", pid=target_pid)
        order_id = info.get("order_id")

        old_stop = _STOP_TASKS.get(pkg)
        if old_stop is None or old_stop.done():
            task = asyncio.create_task(_finish_kill_and_restore_survivors(
                local_device_id, session_id, pkg, target_pid, order_id=order_id
            ))
            _STOP_TASKS[pkg] = task
            def _cleanup_stop(done_task):
                if _STOP_TASKS.get(pkg) is done_task:
                    _STOP_TASKS.pop(pkg, None)
            task.add_done_callback(_cleanup_stop)

    return {
        "type":"COMMAND_RESULT","command":"STOP_SESSION",
        "device_id":local_device_id,"session_id":session_id,
        "ok":True,"reason":"STOPPING"
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
    task = asyncio.create_task(
        _run_package_recovery(
            pkg,
            session_id,
            "MANUAL_RESTART",
            target_pid=str(info.get("pid") or "").strip(),
            target_pid_start_time=info.get("pid_start_time"),
        )
    )
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
    if KILL_ONLY_TEST_MODE or KILL_THEN_LAUNCH_TEST_MODE:
        log.warning(
            f"[KILL_ONLY_TEST] recovery request diblokir untuk {pkg} "
            f"trigger={trigger}."
        )
        return

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


def _debug_reconcile_log(pkg: str, info: dict, pids, adj, package_alive) -> None:
    """Log diagnostik expected-vs-actual, termasuk liveness tri-state."""
    if not WATCHDOG_DEBUG_LOG:
        return
    device = info.get("_device_id") or "?"
    short = pkg.replace("com.roblox.", "")
    pid_alive = bool(pids) if pids is not None else None
    log.info(
        f"[{device}][{short}]\n"
        f"  Expected: RUNNING\n"
        f"  PID: {info.get('pid') or '-'} (pidof: {','.join(sorted(pids)) if pids else ('UNKNOWN' if pids is None else 'tidak ada')})\n"
        f"  PID Alive: {str(pid_alive).upper()}\n"
        f"  Process adj: {adj if adj is not None else 'n/a'}"
        f"{' (CACHED)' if adj is not None and adj >= WATCHDOG_CACHED_ADJ_MIN else ''}\n"
        f"  Package Alive: {str(package_alive).upper()}\n"
        f"  Liveness State: {info.get('liveness_state', 'UNKNOWN')}\n"
        f"  Last PID Check: {info.get('last_pid_check_at') or 'n/a'}\n"
        f"  Session State: {info.get('runtime_status') or info.get('status')}"
    )
    if pid_alive is False or package_alive is False:
        log.warning(f"[{device}][{short}] STATE MISMATCH: sesi masih "
                    f"{info.get('runtime_status') or info.get('status')} tetapi aktual tidak hidup.")


async def _watchdog_tick(pkg: str, info: dict) -> None:
    """Satu siklus probe liveness package.

    Kontrak tri-state:
      TRUE  = PID + process aktif dan probe fresh.
      FALSE = proses sudah dikonfirmasi hilang/cached.
      None  = belum dapat dibuktikan (probe error / suspected cached).

    Hanya kondisi FALSE yang boleh memicu recovery. Kondisi None tidak boleh
    diperlakukan sebagai sehat dan juga tidak boleh memicu kill palsu.
    """
    status = info.get("status")

    if KILL_ONLY_TEST_MODE or KILL_THEN_LAUNCH_TEST_MODE:
        # Keep watchdog task alive for heartbeat supervision, but never allow
        # a missing/cached PID to become an automatic recovery trigger during
        # the diagnostic kill-only experiment.
        if WATCHDOG_DEBUG_LOG and status == "ACTIVE":
            log.info(f"[KILL_ONLY_TEST] watchdog recovery disabled for {pkg}.")
        return

    if status == "RECOVERING":
        if pkg in _recovery_in_progress:
            info["_stuck_ticks"] = 0
            return
        info["_stuck_ticks"] = info.get("_stuck_ticks", 0) + 1
        if info["_stuck_ticks"] >= WATCHDOG_STUCK_RECOVERY_TICKS:
            info["_stuck_ticks"] = 0
            _recovery_in_progress.add(pkg)
            log.warning(f"[{pkg}] RECOVERING tanpa recovery aktif -- melanjutkan recovery.")
            await _start_recovery_inline(pkg, info, "RECOVERY_RESUME")
        return

    if status != "ACTIVE":
        return
    info["_stuck_ticks"] = 0
    if pkg in _recovery_in_progress:
        return

    pids = await asyncio.to_thread(process_manager.get_pids, pkg)
    if pids is None:
        # Check gagal/ambigu: invalidate liveness terbaru. Jangan update
        # last_pid_check_at karena tidak ada probe sukses yang baru.
        info["pid_alive"] = None
        info["package_alive"] = None
        info["adj"] = None
        info["_cached_ticks"] = 0
        info["liveness_state"] = "UNKNOWN"
        _debug_reconcile_log(pkg, info, pids, None, None)
        log.warning(f"[{pkg}] PID check gagal/ambigu (UNKNOWN); last good liveness dipertahankan sebagai timestamp lama.")
        return

    if pids:
        recorded = str(info.get("pid") or "")
        info["pid_alive"] = True
        info["_pid_misses"] = 0
        if recorded not in pids:
            new_pid = _pick_pid(pids)
            log.info(f"[{pkg}] PID berubah {recorded or '-'} -> {new_pid} (proses hidup, monitor diperbarui).")
            info["pid"] = new_pid

        adj = await asyncio.to_thread(process_manager.get_min_oom_adj, pids)
        info["adj"] = adj
        checked_at = time.time()
        if adj is None:
            info["_cached_ticks"] = 0
            info["package_alive"] = None
            info["liveness_state"] = "UNKNOWN"
            info["last_pid_check_at"] = checked_at
            _debug_reconcile_log(pkg, info, pids, adj, None)
            return

        cached = adj >= WATCHDOG_CACHED_ADJ_MIN
        in_grace = (checked_at - (info.get("last_status_at") or 0)) < WATCHDOG_POST_STATUS_GRACE_SECONDS
        if cached and not in_grace:
            info["_cached_ticks"] = info.get("_cached_ticks", 0) + 1
        else:
            info["_cached_ticks"] = 0

        if not cached:
            info["package_alive"] = True
            info["liveness_state"] = "ALIVE"
            info["last_pid_check_at"] = checked_at
            _debug_reconcile_log(pkg, info, pids, adj, True)
            if checked_at - info.get("_last_alive_log", 0) >= WATCHDOG_ALIVE_LOG_EVERY_SECONDS:
                info["_last_alive_log"] = checked_at
                log.info(f"[{pkg}] PID check: ACTIVE (pid={info.get('pid')}, adj={adj}).")
            return

        # Cached process belum langsung dianggap mati. Tick awal = SUSPECTED;
        # baru setelah threshold + recheck menjadi FALSE/CONFIRMED_CACHED.
        info["package_alive"] = None
        info["liveness_state"] = (
            "CONFIRMED_CACHED"
            if info["_cached_ticks"] >= WATCHDOG_CACHED_TICKS_BEFORE_RECOVERY
            else "SUSPECTED_CACHED"
        )
        info["last_pid_check_at"] = checked_at
        _debug_reconcile_log(pkg, info, pids, adj, None)

        if info["_cached_ticks"] < WATCHDOG_CACHED_TICKS_BEFORE_RECOVERY:
            return

        log.warning(
            f"[{pkg}] PID {info['pid']} MASIH ADA tapi proses CACHED (adj={adj}) "
            f"{info['_cached_ticks']}x berturut-turut -> konfirmasi ulang sebelum recovery..."
        )
        await asyncio.sleep(WATCHDOG_CONFIRM_DELAY_SECONDS)
        if SESSIONS.get(pkg) is not info or info.get("status") != "ACTIVE" or pkg in _recovery_in_progress:
            return

        pids2 = await asyncio.to_thread(process_manager.get_pids, pkg)
        if pids2 is None:
            info["pid_alive"] = None
            info["package_alive"] = None
            info["adj"] = None
            info["liveness_state"] = "UNKNOWN"
            _debug_reconcile_log(pkg, info, pids2, None, None)
            log.warning(f"[{pkg}] konfirmasi cached gagal/ambigu; tidak memicu recovery.")
            return

        if pids2:
            adj2 = await asyncio.to_thread(process_manager.get_min_oom_adj, pids2)
            if adj2 is not None and adj2 < WATCHDOG_CACHED_ADJ_MIN:
                info["pid_alive"] = True
                info["pid"] = _pick_pid(pids2)
                info["adj"] = adj2
                info["package_alive"] = True
                info["liveness_state"] = "ALIVE"
                info["_cached_ticks"] = 0
                info["last_pid_check_at"] = time.time()
                _debug_reconcile_log(pkg, info, pids2, adj2, True)
                log.info(f"[{pkg}] konfirmasi: proses aktif lagi (adj={adj2}); tidak ada recovery.")
                return
            if adj2 is None:
                info["pid_alive"] = True
                info["adj"] = None
                info["_cached_ticks"] = 0
                info["package_alive"] = None
                info["liveness_state"] = "UNKNOWN"
                _debug_reconcile_log(pkg, info, pids2, None, None)
                log.warning(f"[{pkg}] konfirmasi cached: PID ada tetapi adj tidak terbaca; recovery ditunda.")
                return

            # PID masih ada dan tetap cached setelah konfirmasi. Jangan menganggap
            # kondisi ini sebagai crash: pada Android/Zetsu, sibling window dapat
            # masuk cached state akibat operasi package lain. Auto-recovery di sini
            # akan menyentuh sibling dan melanggar isolation.
            info["pid_alive"] = True
            info["pid"] = _pick_pid(pids2)
            info["adj"] = adj2
            info["package_alive"] = False
            info["liveness_state"] = "CONFIRMED_CACHED"
            info["last_pid_check_at"] = time.time()
            _debug_reconcile_log(pkg, info, pids2, adj2, False)
            log.warning(
                f"[{pkg}] proses tetap CACHED setelah konfirmasi; "
                "auto-recovery dilewati untuk menjaga package isolation."
            )
            return
        else:
            # PID hilang pada konfirmasi kedua -> confirmed dead.
            info["pid_alive"] = False
            info["package_alive"] = False
            info["adj"] = None
            info["liveness_state"] = "DEAD"
            info["last_pid_check_at"] = time.time()
            _debug_reconcile_log(pkg, info, pids2, None, False)

        if SESSIONS.get(pkg) is not info or info.get("status") != "ACTIVE" or pkg in _recovery_in_progress:
            return
        info["_cached_ticks"] = 0
        _recovery_in_progress.add(pkg)
        log.warning(f"[{pkg}] Process confirmed dead (PID NOT FOUND 2x). Triggering recovery.")
        await _start_recovery_inline(pkg, info, "PID_CRASH")
        return

    # pids == set(): satu kali miss belum cukup untuk recovery. Selama fase
    # konfirmasi package = UNKNOWN, bukan RUNNING dan bukan langsung DEAD.
    info["pid_alive"] = None
    info["package_alive"] = None
    info["adj"] = None
    info["liveness_state"] = "UNKNOWN"
    _debug_reconcile_log(pkg, info, pids, None, None)
    log.warning(f"[{pkg}] PID check: NOT FOUND (expected pid={info.get('pid') or '-'}); konfirmasi ulang...")
    await asyncio.sleep(WATCHDOG_CONFIRM_DELAY_SECONDS)
    if SESSIONS.get(pkg) is not info or info.get("status") != "ACTIVE" or pkg in _recovery_in_progress:
        return

    pids2 = await asyncio.to_thread(process_manager.get_pids, pkg)
    if pids2 is None:
        info["pid_alive"] = None
        info["package_alive"] = None
        info["adj"] = None
        info["_cached_ticks"] = 0
        info["liveness_state"] = "UNKNOWN"
        _debug_reconcile_log(pkg, info, pids2, None, None)
        log.warning(f"[{pkg}] konfirmasi PID gagal/ambigu; tidak memicu recovery.")
        return

    if pids2:
        info["pid_alive"] = True
        info["pid"] = _pick_pid(pids2)
        adj2 = await asyncio.to_thread(process_manager.get_min_oom_adj, pids2)
        info["adj"] = adj2
        if adj2 is None:
            info["package_alive"] = None
            info["liveness_state"] = "UNKNOWN"
        elif adj2 >= WATCHDOG_CACHED_ADJ_MIN:
            info["package_alive"] = None
            info["liveness_state"] = "SUSPECTED_CACHED"
            # PID hilang lalu muncul lagi memutus rangkaian cached sebelumnya:
            # mulai hitungan SUSPECTED baru dari 1.
            info["_cached_ticks"] = 1
        else:
            info["package_alive"] = True
            info["liveness_state"] = "ALIVE"
            info["_cached_ticks"] = 0
        info["last_pid_check_at"] = time.time()
        _debug_reconcile_log(pkg, info, pids2, adj2, info.get("package_alive"))
        log.info(f"[{pkg}] PID muncul lagi pada konfirmasi (pid={info['pid']}); tidak ada recovery.")
        return

    # Dua probe valid sama-sama tidak menemukan proses -> confirmed dead.
    info["pid_alive"] = False
    info["package_alive"] = False
    info["adj"] = None
    info["liveness_state"] = "DEAD"
    info["last_pid_check_at"] = time.time()
    _debug_reconcile_log(pkg, info, pids2, None, False)
    if SESSIONS.get(pkg) is not info or info.get("status") != "ACTIVE" or pkg in _recovery_in_progress:
        return

    _recovery_in_progress.add(pkg)
    log.warning(f"[{pkg}] Process confirmed dead (PID NOT FOUND 2x). Triggering recovery.")
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


async def _handle_kill_then_launch_test_error(
    event: dict,
    info: dict,
    pkg: str,
    pid: str,
    incident_id: str,
    reason,
    event_start_time,
) -> None:
    """Diagnostic Phase B: exact-PID SIGTERM, then launch TARGET ONLY.

    This intentionally stops before full recovery. No join/rejoin, watchdog
    recovery, retry, freeform, sibling restore, or sibling launch is allowed.
    The purpose is to isolate whether the target launch operation itself causes
    cross-package impact after the proven-safe SIGTERM.
    """
    del event
    killed = await _kill_exact_pid_and_verify(pkg, pid, event_start_time)
    info["pid_alive"] = False if killed else None
    info["package_alive"] = False if killed else None
    info["adj"] = None
    info["last_phase_b_incident_id"] = incident_id
    info["last_phase_b_reason"] = reason
    info["last_phase_b_old_pid"] = pid
    info["last_phase_b_at"] = time.time()

    if not killed:
        info["liveness_state"] = "PHASE_B_KILL_FAILED"
        log.error(
            f"[PHASE_B] incident={incident_id} package={pkg} pid={pid} "
            f"reason={reason} signal=SIGTERM result=FAILED; "
            "launch=SKIPPED recovery=DISABLED sibling_touch=NONE."
        )
        return

    info["pid"] = "-"
    info["liveness_state"] = "PHASE_B_TARGET_DEAD"
    log.warning(
        f"[PHASE_B] incident={incident_id} package={pkg} pid={pid} "
        f"reason={reason} signal=SIGTERM result=DEAD; "
        "launching TARGET ONLY; recovery/join/watchdog/sibling actions disabled."
    )

    # Use the same lobby launch primitive the normal recovery would use, but
    # deliberately do NOT call _launch_then_freeform_soon(): that helper also
    # starts Freeform/Smart-Wait tasks. Phase B needs exactly one additional
    # variable: target launch.
    try:
        launch_ok, launch_start_time = await asyncio.to_thread(
            launch_normal, pkg, get_lobby_intent()
        )
    except Exception:
        launch_ok, launch_start_time = False, None
        log.error(
            f"[PHASE_B] incident={incident_id} package={pkg}: exception during target-only launch.",
            exc_info=True,
        )

    if not launch_ok:
        info["liveness_state"] = "PHASE_B_LAUNCH_FAILED"
        log.error(
            f"[PHASE_B] incident={incident_id} package={pkg}: "
            "target-only launch failed; recovery=DISABLED."
        )
        return

    # Lightweight target-only verification: wait for any PID belonging to the
    # same package. We never enumerate or act on sibling packages here.
    deadline = time.monotonic() + LOBBY_TIMEOUT_SECONDS
    new_pid = ""
    while time.monotonic() < deadline:
        current_pids = await asyncio.to_thread(process_manager.get_pids, pkg)
        if current_pids:
            new_pid = _pick_pid(current_pids)
            break
        await asyncio.sleep(1)

    if new_pid:
        info["pid"] = new_pid
        info["pid_start_time"] = None
        info["pid_alive"] = True
        info["package_alive"] = True
        info["liveness_state"] = "PHASE_B_TARGET_LAUNCHED"
        log.warning(
            f"[PHASE_B] incident={incident_id} package={pkg}: "
            f"target-only launch verified pid={new_pid}; "
            "join/recovery/watchdog remain DISABLED."
        )
    else:
        info["liveness_state"] = "PHASE_B_LAUNCH_UNVERIFIED"
        log.error(
            f"[PHASE_B] incident={incident_id} package={pkg}: "
            f"no target PID detected after {LOBBY_TIMEOUT_SECONDS}s; "
            "recovery=DISABLED sibling_touch=NONE."
        )


async def _handle_kill_only_error(event: dict, info: dict, pkg: str, pid: str, incident_id: str, reason, event_start_time) -> None:
    """Diagnostic path: validate target was already done, then SIGTERM EXACT PID only.

    Recovery, relaunch, rejoin, sibling restore, and watchdog-triggered recovery
    are deliberately not entered. This gives a clean experiment boundary:
    ERROR -> VERIFY -> SIGTERM -> STOP.
    """
    del event  # kept in signature for future diagnostic metadata extensions
    killed = await _kill_exact_pid_and_verify(pkg, pid, event_start_time)
    info["pid_alive"] = False if killed else None
    info["package_alive"] = False if killed else None
    info["adj"] = None
    info["liveness_state"] = "KILL_ONLY_DEAD" if killed else "KILL_ONLY_KILL_FAILED"
    info["last_kill_only_incident_id"] = incident_id
    info["last_kill_only_reason"] = reason
    info["last_kill_only_pid"] = pid
    info["last_kill_only_at"] = time.time()
    if killed:
        info["pid"] = "-"
        log.warning(
            f"[KILL_ONLY_TEST] incident={incident_id} package={pkg} "
            f"pid={pid} reason={reason} signal=SIGTERM result=DEAD "
            "recovery=DISABLED sibling_touch=NONE."
        )
    else:
        log.error(
            f"[KILL_ONLY_TEST] incident={incident_id} package={pkg} "
            f"pid={pid} reason={reason} signal=SIGTERM result=FAILED "
            "no-fallback=no-recovery."
        )


async def _handle_disconnect_event(event: dict) -> None:
    pid = str(event.get("pid") or "").strip()
    pkg = str(event.get("package") or "").strip()
    reason = event.get("reason")
    incident_id = str(event.get("incident_id") or uuid.uuid4().hex).strip()
    event_start_time = event.get("pid_start_time")

    if not pid or not pkg or event_start_time is None:
        log.warning(
            f"[ERROR_EVENT] drop unscoped event pid={pid or '-'} "
            f"package={pkg or '-'} reason={reason}."
        )
        return

    info = SESSIONS.get(pkg)
    if info is None or info.get("status") != "ACTIVE":
        return
    if pkg in _recovery_in_progress:
        return

    identity = await asyncio.to_thread(
        process_manager.get_process_identity, pid
    )
    if not identity:
        log.warning(
            f"[PID_OWNERSHIP] incident={incident_id} {pkg}: "
            f"pid={pid} sudah tidak bisa diverifikasi; watchdog/recovery lain akan menangani."
        )
        return
    if identity.get("package") != pkg:
        log.error(
            f"[PID_OWNERSHIP] incident={incident_id}: pid={pid} "
            f"event_package={pkg} actual_package={identity.get('package')}; DROP."
        )
        return
    if int(identity.get("start_time", -1)) != int(event_start_time):
        log.warning(
            f"[PID_OWNERSHIP] incident={incident_id} {pkg}: pid={pid} "
            f"start_time berubah {event_start_time} -> {identity.get('start_time')}; DROP."
        )
        return

    current_tracked_pid = str(info.get("pid") or "").strip()
    if current_tracked_pid and current_tracked_pid != pid:
        log.warning(
            f"[ERROR_EVENT] incident={incident_id} {pkg}: event pid={pid} "
            f"bukan PID session saat ini ({current_tracked_pid}); DROP."
        )
        return

    if KILL_THEN_LAUNCH_TEST_MODE:
        await _handle_kill_then_launch_test_error(
            event, info, pkg, pid, incident_id, reason, event_start_time
        )
        return

    if KILL_ONLY_TEST_MODE:
        await _handle_kill_only_error(
            event, info, pkg, pid, incident_id, reason, event_start_time
        )
        return

    session_id = info.get("session_id", "")
    # Pin the recovery to the exact process that emitted the error. This is
    # stronger than re-enumerating every PID of the package later.
    info["pid"] = pid
    info["pid_start_time"] = event_start_time
    info["last_error_incident_id"] = incident_id
    info["last_error_reason"] = reason
    _recovery_in_progress.add(pkg)
    info["status"] = "RECOVERING"
    info["runtime_status"] = "RECOVERING"
    info["pid_alive"] = False
    log.warning(
        f"[RECOVERY_TARGET] incident={incident_id} package={pkg} "
        f"session={session_id} pid={pid} reason={reason} target=ONLY_THIS_PACKAGE"
    )
    await _emit_status(
        info.get("_device_id", ""),
        session_id,
        pkg,
        info.get("order_id"),
        "RECOVERING",
        reason=f"LOGCAT_{reason}",
        incident_id=incident_id,
        error_pid=pid,
        error_pid_start_time=event_start_time,
    )
    asyncio.create_task(
        _run_package_recovery(
            pkg,
            session_id,
            f"LOGCAT_{reason}",
            incident_id=incident_id,
            target_pid=pid,
            target_pid_start_time=event_start_time,
        )
    )


async def _run_package_recovery(
    pkg: str,
    session_id: str,
    trigger_reason: str,
    incident_id: str | None = None,
    target_pid: str | None = None,
    target_pid_start_time=None,
) -> None:
    cancel_event = _start_cancel_events.get(pkg)

    def _still_current() -> bool:
        current = SESSIONS.get(pkg)
        if not current or current.get("session_id") != session_id:
            return False
        if str(current.get("status") or "").upper() in {"STOPPING", "STOPPED", "FAILED", "START_FAILED"}:
            return False
        return not (cancel_event is not None and cancel_event.is_set())

    try:
        if not _still_current():
            return

        info = SESSIONS[pkg]
        target = info.get("target", "")
        expected_username = info.get("expected_username", "")
        attempt = 0

        while _still_current():
            attempt += 1
            if attempt > MAX_RECOVERY_ATTEMPTS:
                info = SESSIONS.get(pkg)
                if info is not None:
                    info["status"] = "FAILED"
                    info["runtime_status"] = "RECOVERY_EXHAUSTED"
                log.error(
                    f"[{pkg}] recovery exhausted after {MAX_RECOVERY_ATTEMPTS} attempts "
                    f"(session={session_id}, trigger={trigger_reason})."
                )
                if info is not None:
                    await _emit_status(
                        info.get("_device_id", ""), session_id, pkg, info.get("order_id"),
                        "START_FAILED", reason="RECOVERY_EXHAUSTED",
                    )
                return
            info = SESSIONS[pkg]
            manual = bool(info.get("_manual_restart"))
            info["status"] = "RECOVERING"
            # Restart manual punya state sendiri di heartbeat/panel:
            # RESTARTING -> WAITING_FOR_PID -> (JOINING_GAME ...) -> RUNNING.
            info["runtime_status"] = "RESTARTING" if manual else "RECOVERING"

            if attempt > 1:
                if not await _sleep_or_cancel(_recovery_backoff_delay(attempt - 1), cancel_event):
                    return
            if not _still_current():
                return

            if not manual:  # restart manual dijalankan langsung, tanpa jitter
                if not await _sleep_or_cancel(random.uniform(
                    RECOVERY_PRE_KILL_JITTER_MIN_SECONDS,
                    RECOVERY_PRE_KILL_JITTER_MAX_SECONDS,
                ), cancel_event):
                    return
            if not _still_current():
                return

            # ERROR RECOVERY: kill exactly the PID that produced the event (or
            # the session's currently tracked PID for watchdog/manual recovery).
            # NEVER enumerate and kill all PIDs for the package.
            old_pid = str(info.get("pid") or "").strip()
            old_pids = {old_pid} if manual and old_pid.isdigit() else set()
            recovery_pid = str(target_pid or old_pid).strip()
            recovery_start_time = (
                target_pid_start_time
                if target_pid_start_time is not None
                else info.get("pid_start_time")
            )

            if recovery_pid and recovery_pid.isdigit():
                log.info(
                    f"[RECOVERY_TARGET] incident={incident_id or '-'} {pkg}: "
                    f"exact_pid={recovery_pid} start_time={recovery_start_time or '-'}; "
                    "zero-touch sibling."
                )
                killed = await _kill_exact_pid_and_verify(
                    pkg, recovery_pid, recovery_start_time
                )
                if not killed:
                    log.error(
                        f"[KILL] incident={incident_id or '-'} {pkg}: "
                        f"exact pid={recovery_pid} gagal dihentikan dengan SIGTERM. "
                        "Tidak memilih PID lain, tidak SIGKILL, tidak force-stop."
                    )
                    continue
                log.info(
                    f"[KILL] incident={incident_id or '-'} {pkg}: "
                    f"exact pid={recovery_pid} killed=OK."
                )
            else:
                # Process is already gone or watchdog has no trustworthy PID.
                # Recovery may relaunch the target, but it must not guess another
                # process to kill.
                log.info(
                    f"[RECOVERY_TARGET] incident={incident_id or '-'} {pkg}: "
                    "no exact PID available; launch-only recovery, zero-touch sibling."
                )

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

            if not _still_current():
                return
            lobby_ok = await _launch_then_freeform_soon(
                pkg, get_lobby_intent(), LOBBY_TIMEOUT_SECONDS,
                require_join_signal=False, session_id=session_id,
                only_if_previously_freeform=True, cancel_event=cancel_event,
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
                    if not await _sleep_or_cancel(1, cancel_event):
                        return
                else:
                    if _still_current():
                        log.warning(f"[{pkg}] restart: PID baru belum terdeteksi dalam "
                                    f"{RESTART_WAIT_NEW_PID_SECONDS}s; lanjut, watchdog akan memantau.")
                if not _still_current():
                    return
                info["runtime_status"] = "RESTARTING"
            log.info(f"[{pkg}] Roblox launched (lobby). PID {old_pid or '-'} -> {info['pid']}")

            if not await _wait_in_lobby(pkg, session_id, recovery=True, cancel_event=cancel_event):
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
                    if not await _sleep_or_cancel(LOGIN_POLL_INTERVAL_SECONDS, cancel_event):
                        return
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

            if not await _wait_before_map_entry(pkg, session_id, cancel_event=cancel_event):
                return

            try:
                intent_url = get_intent_url(target)
            except ValueError as e:
                log.error(
                    f"SESSION_AGENT: recovery target invalid {pkg}: {e}"
                )
                continue

            if not _still_current():
                return
            joined = await _attempt_join_target(
                pkg, session_id, intent_url, DEFAULT_TIMEOUT_SECONDS,
                cancel_event=cancel_event,
            )
            if not _still_current():
                return

            if not joined:
                continue

            info = SESSIONS[pkg]
            info["status"] = "ACTIVE"
            info["runtime_status"] = "RUNNING"
            info["pid"] = get_pid_quick(pkg) or "-"
            info["pid_start_time"] = None
            info["_pid_misses"] = 0
            info["launch_count"] = info.get("launch_count", 0) + 1
            # PID baru sudah terdeteksi, tetapi probe liveness lengkap belum
            # dijalankan lagi. Jangan wariskan status FALSE lama sebagai state
            # sehat; watchdog akan melakukan probe fresh berikutnya.
            info["pid_alive"] = None
            info["package_alive"] = None
            info["adj"] = None
            info["liveness_state"] = "UNKNOWN"
            info["last_pid_check_at"] = None
            info.pop("_cached_ticks", None)
            if info.pop("_manual_restart", False):
                info["restart_count"] = info.get("restart_count", 0) + 1
                info["last_restart_at"] = time.time()
                log.info(f"[{pkg}] MANUAL_RESTART selesai. New PID: {info['pid']}. State: RUNNING")
            else:
                log.info(f"[{pkg}] New PID detected: {info['pid']}. Recovery successful. State: RUNNING")

            if not _still_current():
                return

            _ensure_watchdog(pkg)
            _ensure_username_scanner(pkg)
            _ensure_error_watcher()

            # Error recovery owns the TARGET package only. It must never issue
            # commands to sibling packages, including foreground/restore actions.
            # Any sibling window/process impact is logged by the watchdog/diagnostic
            # layer and is deliberately not repaired from this recovery task.
            log.info(
                f"[RECOVERY_COMPLETE] incident={incident_id or '-'} "
                f"package={pkg} session={session_id} new_pid={info['pid']} "
                "state=RUNNING."
            )
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
    pkg: str, session_id: str, intent_url: str, timeout_seconds: int, cancel_event=None
) -> bool:
    def _still_current() -> bool:
        current = SESSIONS.get(pkg)
        return bool(current and current.get("session_id") == session_id)

    result = await _launch_then_freeform_soon(
        pkg, intent_url, timeout_seconds,
        require_join_signal=True, session_id=session_id, cancel_event=cancel_event,
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

        # Cek PID tervalidasi + retry singkat: satu `pidof` yang meleset
        # (timeout/transient) dulu membuat package yang MASIH HIDUP dilewati
        # permanen -> tidak pernah dipantau watchdog -> panel "Tidak terpantau".
        alive = None
        for _attempt in range(3):
            alive = await asyncio.to_thread(process_manager.get_pids, pkg)
            if alive:
                break
            if _attempt < 2:
                await asyncio.sleep(1)
        if not alive:
            log.warning(
                f"SESSION_AGENT: SYNC -- bot mengharapkan {pkg} "
                f"(session {expected_session_id}) tetapi proses tidak hidup; "
                f"TIDAK auto-launch."
            )
            continue
        pid = _pick_pid(alive)
        pid_identity = await asyncio.to_thread(
            process_manager.get_process_identity, pid
        )
        pid_start_time = (
            pid_identity.get("start_time")
            if pid_identity and pid_identity.get("package") == pkg
            else None
        )
        adj = await asyncio.to_thread(process_manager.get_min_oom_adj, alive)
        sync_check_at = time.time()
        if adj is None:
            sync_package_alive = None
            sync_liveness_state = "UNKNOWN"
        elif adj >= WATCHDOG_CACHED_ADJ_MIN:
            sync_package_alive = None
            sync_liveness_state = "SUSPECTED_CACHED"
        else:
            sync_package_alive = True
            sync_liveness_state = "ALIVE"

        expected_username = str(
            entry.get("expected_username")
            or entry.get("username")
            or ""
        ).strip()
        target = str(entry.get("target", "") or "").strip()

        if desired_status in running or desired_status == "RECOVERING":
            # RECOVERING di DB bot + proses MASIH HIDUP di device = package
            # efektif berjalan (task recovery lama ikut hilang bersama agent
            # yang restart). Adopt sebagai ACTIVE supaya watchdog memantau lagi.
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
            "pid_start_time": pid_start_time,
            "launch_count": 1,
            "crash_count": 0,
            "status_seq": 0,
            "last_status_at": time.time(),
            "pid_alive": True,
            "package_alive": sync_package_alive,
            "adj": adj,
            "liveness_state": sync_liveness_state,
            "last_pid_check_at": sync_check_at,
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

