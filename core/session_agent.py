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

# ==========================================================
# SISTEM DETEKSI & AUTO-RECOVERY (disconnect logcat + crash PID)
# ==========================================================
# Rentang jeda di LOBBY setelah recovery relaunch, SEBELUM join ulang ke
# target -- supaya Roblox benar-benar siap (bukan cuma proses hidup) sebelum
# dipaksa masuk game lagi. Diacak tiap kali (bukan angka tetap) di rentang
# 60-90 detik.
RECOVERY_LOBBY_WAIT_MIN_SECONDS = 60
RECOVERY_LOBBY_WAIT_MAX_SECONDS = 90

# Jeda kecil SEBELUM kill (dijalankan tiap mulai 1 percobaan recovery) --
# supaya transisi "disconnect -> kill" gak instan kayak mesin (manusia yang
# beneran ke-disconnect biasanya ada jeda "ngecek dulu" sebelum benerin).
RECOVERY_PRE_KILL_JITTER_MIN_SECONDS = 2
RECOVERY_PRE_KILL_JITTER_MAX_SECONDS = 6

# Backoff antar percobaan kalau satu tahap (kill/launch lobby/join ulang)
# gagal -- TETAP retry TANPA BATAS (tidak pernah menyerah, sesuai
# permintaan), tapi delay-nya membesar eksponensial + diacak (jitter)
# supaya tidak jadi pola "coba lagi tiap 20 detik persis" yang keliatan
# seperti brute-force dari sisi anti-fraud Roblox. attempt ke-1 gagal ->
# sekitar RECOVERY_RETRY_BASE_DELAY_SECONDS, attempt berikutnya makin
# lama, dibatasi RECOVERY_RETRY_MAX_DELAY_SECONDS supaya tidak tak
# terhingga.
RECOVERY_RETRY_BASE_DELAY_SECONDS = 20
RECOVERY_RETRY_MAX_DELAY_SECONDS = 300

# "Circuit breaker": begitu SATU siklus recovery (untuk 1 kejadian
# disconnect/crash yang sama) sudah gagal berturut-turut sebanyak ini,
# berhenti mempercepat -- masuk mode cooldown panjang (acak 5-10 menit)
# tiap putaran, BUKAN berhenti retry sama sekali. Ini yang mencegah
# pola "brute-force login" kalau penyebabnya ternyata bukan sesuatu yang
# sementara (mis. device lagi di-rate-limit atau akunnya sendiri sudah
# kena flag).
RECOVERY_CIRCUIT_BREAKER_THRESHOLD = 5
RECOVERY_CIRCUIT_BREAKER_COOLDOWN_MIN_SECONDS = 300
RECOVERY_CIRCUIT_BREAKER_COOLDOWN_MAX_SECONDS = 600


def _recovery_backoff_delay(attempt: int) -> float:
    """Hitung jeda sebelum percobaan recovery berikutnya. attempt = nomor
    percobaan yang BARU SAJA gagal (1-based). Selama masih di bawah
    RECOVERY_CIRCUIT_BREAKER_THRESHOLD, delay naik eksponensial + jitter
    acak (bukan angka tetap). Setelah lewat threshold, masuk mode cooldown
    panjang acak (circuit breaker) -- tetap retry, cuma jauh lebih jarang."""
    if attempt >= RECOVERY_CIRCUIT_BREAKER_THRESHOLD:
        return random.uniform(
            RECOVERY_CIRCUIT_BREAKER_COOLDOWN_MIN_SECONDS, RECOVERY_CIRCUIT_BREAKER_COOLDOWN_MAX_SECONDS,
        )
    base = min(RECOVERY_RETRY_MAX_DELAY_SECONDS, RECOVERY_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
    return random.uniform(base * 0.7, base * 1.3)

# Seberapa sering consumer men-drain queue event dari error_detector.py.
DISCONNECT_POLL_INTERVAL_SECONDS = 1

# IMPROVE ANDROID 12 (Timer utk Proses Freeform): sebelumnya Freeform baru
# diaktifkan SETELAH launch_and_wait() selesai menunggu Roblox "beneran
# aktif" (Smart Wait logcat, bisa sampai puluhan detik / LOBBY_TIMEOUT_SECONDS
# & JOIN_TIMEOUT_SECONDS di atas) -- makin lama itu berjalan, makin lama
# package LAIN yang SUDAH Freeform ikut tertahan di background tanpa
# dibangkitkan (_activate_freeform_and_restore_siblings), makin besar risiko
# package itu di-kill sistem karena kelamaan di background.
#
# Sekarang: activate_freeform() dipicu lewat TIMER TETAP setelah `am start`
# terkirim (launch_normal()), BUKAN menunggu hasil Smart Wait
# (wait_for_launch_signal()) -- keduanya jalan BERBARENGAN (lihat
# _launch_then_freeform_soon()). 2-3 detik dipilih supaya Roblox sempat
# lewat fase transisi awal (ActivityProtocolLaunch -> ActivityNativeMain /
# splash) -- window belum ada di fase itu, dan activate_freeform() sendiri
# masih retry beberapa kali (lihat _FREEFORM_VERIFY_ATTEMPTS di launcher.py)
# kalau taskId/window belum kebentuk pas timer ini habis.
FREEFORM_ACTIVATION_DELAY_SECONDS = 2.5

# async(dict) -> None, diregistrasi agent_client.py SETIAP KALI koneksi WS
# baru tersambung (lihat register_sender()). Dipakai modul ini untuk kirim
# SESSION_STATUS (transisi antara PREPARING/WAITING_LOGIN/ACCOUNT_READY/
# JOINING_GAME/RUNNING) ke bot DI LUAR jalur balasan COMMAND_RESULT biasa --
# perlu karena satu START_SESSION sekarang bisa melewati BEBERAPA status
# sebelum benar-benar selesai (dulu cuma 1 balasan langsung ACTIVE/FAILED).
_send_callback = None


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
    """Kirim SESSION_STATUS ke bot (best-effort -- kalau koneksi sedang
    putus, cukup dilog, TIDAK raise, supaya alur START_SESSION lokal tetap
    lanjut apa adanya; penyelarasan penuh menyusul lewat SYNC_SESSIONS
    begitu device reconnect).

    SENGAJA TIDAK menyentuh SESSIONS[pkg]['status'] -- itu field INTERNAL
    yang dipakai _headless_watchdog_loop (cuma kenal 'ACTIVE'/'STOPPED'/
    'FAILED', lihat modul itu) dan HARUS dikontrol eksplisit oleh
    caller (_run_start_flow/handle_stop_session), supaya nilai protokol
    bertingkat (PREPARING/WAITING_LOGIN/.../RUNNING) yang dikirim ke bot
    tidak bentrok/menimpa status internal watchdog secara tidak sengaja."""
    if _send_callback is None:
        log.warning(f"SESSION_AGENT: tidak ada sender terdaftar, SESSION_STATUS '{status}' "
                    f"untuk {pkg} (session {session_id}) tidak terkirim.")
        return
    payload = {
        "type": "SESSION_STATUS", "device_id": local_device_id, "session_id": session_id,
        "package_name": pkg, "order_id": order_id, "status": status,
    }
    payload.update(extra)
    try:
        await _send_callback(payload)
    except Exception:
        log.warning(f"SESSION_AGENT: gagal kirim SESSION_STATUS '{status}' untuk {pkg} "
                    f"(session {session_id}), koneksi mungkin putus.")

# PHASE 4.5 (lihat D2 di PROJECT_CONTEXT.txt): interval scan username,
# SENGAJA jauh lebih longgar dari WATCHDOG_INTERVAL_SECONDS -- "jangan
# agresif, jangan bebani Cloud Phone" (D2). Baca file kecil tiap 60 detik
# per package jauh lebih ringan daripada cek PID tiap 15 detik.
USERNAME_SCAN_INTERVAL_SECONDS = 60

# package_name -> info sesi yang sedang dikelola agent (bukan menu manual).
# status: STARTING | ACTIVE | FAILED | STOPPED
SESSIONS: dict = {}

# [FINISH] LOGIC BARU: STOP_SESSION kill target package lewat PID
# (`kill <pid>`, BUKAN `am force-stop` -- broadcast Activity Manager dari
# force-stop itulah yang dulu memicu efek visual ke package/session LAIN).
# Survivor (package lain berstatus ACTIVE) yang mungkin ikut jadi
# bubble/minimized secara visual TIDAK di-kill/restart -- cuma dikembalikan
# ke foreground satu per satu lewat `monkey -p <package> 1`. Lihat
# _finish_kill_and_restore_survivors() untuk detail lengkap alurnya.

# package_name -> asyncio.Task watchdog yang sedang berjalan untuk package itu.
_watchdog_tasks: dict = {}

# Set package_name yang SEDANG dalam proses recovery (kill->lobby->delay->
# join ulang) -- dipakai sebagai lock ringan supaya PID-crash watchdog
# (_headless_watchdog_loop) dan consumer disconnect logcat
# (_error_event_consumer_loop) tidak sama-sama memicu recovery dobel untuk
# package yang sama pada saat bersamaan.
_recovery_in_progress: set = set()

# Task tunggal (BUKAN per-package -- logcat "Roblox" mencakup semua PID
# sekaligus) yang membaca queue error_detector.py dan mencocokkan PID ke
# package/akun yang sedang ACTIVE. Lihat _ensure_error_watcher().
_error_watcher_task = None

# === MULTI-PACKAGE FREEFORM (Android 12) ===
# package_name -> {"session_id", "task_id", "state"} untuk package yang
# SUDAH dipastikan Freeform+tampil (lihat _activate_freeform_and_restore_siblings).
# Dipakai SEMATA-MATA untuk tahu package MANA SAJA yang perlu di-"bangkitkan"
# balik ke foreground (monkey) setiap kali ada package BARU yang baru selesai
# di-freeform-kan -- supaya window freeform yang sudah lebih dulu ada tidak
# ketutup/ke-background akibat proses buka package baru. Entry dihapus lagi
# di handle_stop_session begitu package itu berhenti (jangan restore package
# yang sudah tidak aktif).
_FREEFORM_REGISTRY: dict = {}

# Jeda antar panggilan `monkey` per sibling package saat rotasi freeform --
# sama seperti _FINISH_RESTORE_STEP_DELAY_SECONDS (satu "monkey" cuma
# membawa satu package, jeda kecil ini cuma supaya panggilan beruntun tidak
# asal numpuk).
_FREEFORM_RESTORE_STEP_DELAY_SECONDS = 0.3


# package_name -> asyncio.Task username scanner (PHASE 4.5) yang sedang
# berjalan untuk package itu -- lifecycle SAMA seperti _watchdog_tasks
# (mulai bareng START_SESSION, berhenti begitu package dihapus dari
# SESSIONS oleh STOP_SESSION).
_username_tasks: dict = {}


def _snapshot_for_heartbeat() -> dict:
    """Dipakai agent_client.py supaya heartbeat ikut melaporkan package yang
    dikelola agent (selain package yang dipantau lewat menu manual, kalau
    ada). PHASE 4.5: ikut sertakan 'username' (dari cache username_scanner,
    NON-BLOCKING -- tidak pernah subprocess call langsung di sini) supaya
    bot/Discord bisa tampilkan akun Roblox yang sedang jalan (lihat D2)."""
    return {
        pkg: {
            "pid": info.get("pid", "-"),
            "state": info.get("status", "UNKNOWN"),
            "username": username_scanner.get_cached_username(pkg),
        }
        for pkg, info in SESSIONS.items()
    }


def _snapshot_for_sync() -> dict:
    """PHASE 8: mirip _snapshot_for_heartbeat(), TAPI ikut sertakan
    session_id -- HEARTBEAT biasa tidak menyertakan session_id (cukup
    pid/state/username per package), sedangkan SYNC_RESPONSE butuh
    session_id juga supaya bot bisa cocokkan per SESSION, bukan cuma per
    package (2 session berbeda bisa pernah pakai package yang sama)."""
    return {
        pkg: {
            "session_id": info.get("session_id", ""),
            "status": info.get("status", "UNKNOWN"),
            "pid": info.get("pid", "-"),
        }
        for pkg, info in SESSIONS.items()
    }


async def handle_start_session(msg: dict, local_device_id: str) -> dict:
    """LIFECYCLE REVISION: terima START_SESSION dan LANGSUNG return
    COMMAND_RESULT (ok/reason) HANYA untuk memberi tahu bot bahwa command ini
    valid dan sudah MULAI diproses -- bukan lagi berarti joki sudah aktif
    (dokumen revisi bag. 10: 'COMMAND_RESULT OK untuk START_SESSION tidak
    boleh langsung mengubah session menjadi RUNNING').

    Proses staged yang sebenarnya (buka lobby -> tunggu login -> validasi
    akun -> join target -> verifikasi masuk -> RUNNING) dijalankan sebagai
    task terpisah (_run_start_flow) yang melapor progresnya lewat
    SESSION_STATUS (_emit_status) satu per satu -- TIDAK memblokir balasan
    COMMAND_RESULT ini, dan TIDAK memblokir _receive_loop agent_client.py
    (yang sudah membungkus setiap command masuk dengan asyncio.create_task,
    lihat agent_client._handle_incoming_command).
    """
    session_id = str(msg.get("session_id", "")).strip()
    order_id = msg.get("order_id")
    pkg = str(msg.get("package_name", "")).strip()
    target = str(msg.get("target", "")).strip()
    # LIFECYCLE REVISION: username akun customer yang SEHARUSNYA login,
    # dikirim bot dari orders.customer_username (lihat _dispatch_start_session
    # di bot.py). Kalau kosong/tidak dikirim (mis. bot versi lama sebelum
    # revisi ini), validasi akun DILEWATI (fallback ke perilaku lama) --
    # supaya tidak ada kombinasi versi yang saling mematikan sesi.
    expected_username = str(msg.get("expected_username", "") or "").strip()
    timeout_seconds = int(msg.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)

    if not session_id or not pkg or not target:
        log.error(f"SESSION_AGENT: START_SESSION tidak lengkap (session={session_id}, pkg={pkg}).")
        return {"type": "COMMAND_RESULT", "command": "START_SESSION", "device_id": local_device_id,
                "session_id": session_id, "ok": False, "reason": "MISSING_FIELDS"}

    try:
        intent_url = get_intent_url(target)
    except ValueError as e:
        log.error(f"SESSION_AGENT: target tidak valid untuk {pkg}: {e}")
        return {"type": "COMMAND_RESULT", "command": "START_SESSION", "device_id": local_device_id,
                "session_id": session_id, "ok": False, "reason": f"INVALID_TARGET: {e}"}

    SESSIONS[pkg] = {
        "session_id": session_id, "order_id": order_id, "target": target,
        "expected_username": expected_username,
        "status": "PREPARING", "pid": "-", "launch_count": 1, "crash_count": 0,
    }
    log.info(f"SESSION_AGENT: START_SESSION diterima -> {pkg} (session {session_id}), "
             f"expected_username={expected_username or '(tidak divalidasi)'}, memproses...")

    asyncio.create_task(_run_start_flow(
        local_device_id, session_id, pkg, order_id, intent_url, expected_username, timeout_seconds,
    ))

    return {"type": "COMMAND_RESULT", "command": "START_SESSION", "device_id": local_device_id,
            "session_id": session_id, "ok": True, "reason": "PROCESSING"}


async def _run_start_flow(local_device_id: str, session_id: str, pkg: str, order_id,
                           intent_url: str, expected_username: str, timeout_seconds: int) -> None:
    """LIFECYCLE REVISION: jalur staged PREPARING -> (WAITING_LOGIN ->
    ACCOUNT_READY, kalau expected_username diisi) -> JOINING_GAME -> RUNNING,
    persis alur di dokumen revisi bag. 5 & 10. Tidak pernah raise -- semua
    kegagalan dilaporkan lewat SESSION_STATUS status='START_FAILED' supaya
    bot tidak nyangkut menunggu."""

    def _still_current() -> bool:
        """Guard di setiap tahap: kalau session ini sudah digantikan/dibatalkan
        (mis. STOP_SESSION masuk duluan sebelum login selesai, atau
        SESSION_STATUS RUNNING dari attempt lama), hentikan flow ini diam-diam
        -- jangan menimpa state package yang sudah dipegang session lain."""
        current = SESSIONS.get(pkg)
        return bool(current and current.get("session_id") == session_id)

    await _emit_status(local_device_id, session_id, pkg, order_id, "PREPARING")

    # --- Tahap 1: buka Roblox ke LOBBY dulu (bukan langsung ke target) ---
    # supaya kita bisa mengecek akun yang login TANPA sudah keburu masuk ke
    # game dengan akun yang salah (dokumen bag. 3: 'Roblox terbuka bukan
    # berarti joki dimulai'). Kalau expected_username kosong (mode lama),
    # tetap lewat lobby dulu supaya perilaku konsisten -- cuma pengecekan
    # username-nya yang dilewati di tahap 2.
    try:
        # IMPROVE ANDROID 12 (Timer utk Proses Freeform): freeform TIDAK LAGI
        # menunggu lobby_ok -- dipicu lewat timer tetap di dalam
        # _launch_then_freeform_soon() begitu `am start` terkirim, BERBARENGAN
        # dengan Smart Wait (bukan setelahnya lagi). Lihat docstring fungsi
        # tsb / FREEFORM_ACTIVATION_DELAY_SECONDS.
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
        await _emit_status(local_device_id, session_id, pkg, order_id, "START_FAILED",
                            reason="LOBBY_LAUNCH_FAILED")
        return
    SESSIONS[pkg]["pid"] = get_pid_quick(pkg) or "-"

    if not _still_current():
        return

    # --- Tahap 2: pastikan akun customer yang benar sudah login ---
    if expected_username:
        await _emit_status(local_device_id, session_id, pkg, order_id, "WAITING_LOGIN",
                            expected_username=expected_username)
        deadline = time.monotonic() + LOGIN_WAIT_TIMEOUT_SECONDS
        detected = None
        last_reported_mismatch = None
        while time.monotonic() < deadline:
            if not _still_current():
                return
            try:
                detected = await asyncio.to_thread(username_scanner.scan_username_blocking, pkg)
            except Exception:
                log.error(f"SESSION_AGENT: exception scan username {pkg} (start flow).", exc_info=True)
                detected = None

            if detected and detected.strip().lower() == expected_username.strip().lower():
                break  # MATCH -- lanjut ke tahap 3

            if detected and detected != last_reported_mismatch:
                # Akun SUDAH login tapi BUKAN akun customer -- lapor sekali per
                # perubahan (bukan tiap poll) supaya tidak flood Discord, staff
                # perlu ganti akun secara manual (dokumen bag. 14).
                last_reported_mismatch = detected
                log.warning(f"SESSION_AGENT: {pkg} (session {session_id}) login sebagai "
                            f"'{detected}', BUKAN '{expected_username}' -- menunggu staff perbaiki.")
                await _emit_status(local_device_id, session_id, pkg, order_id, "WAITING_LOGIN",
                                    expected_username=expected_username, detected_username=detected,
                                    mismatch=True)

            await asyncio.sleep(LOGIN_POLL_INTERVAL_SECONDS)
        else:
            # Timeout -- staff tidak sempat login akun yang benar.
            if not _still_current():
                return
            SESSIONS[pkg]["status"] = "FAILED"
            await _emit_status(local_device_id, session_id, pkg, order_id, "START_FAILED",
                                reason="LOGIN_TIMEOUT", expected_username=expected_username)
            return

        if not _still_current():
            return
        await _emit_status(local_device_id, session_id, pkg, order_id, "ACCOUNT_READY",
                            detected_username=detected)

    # --- Tahap 3: join ke target game (Place ID / Private Server) ---
    await _emit_status(local_device_id, session_id, pkg, order_id, "JOINING_GAME")
    try:
        # IMPROVE ANDROID 12 (Timer utk Proses Freeform): sama seperti tahap
        # lobby -- freeform dipicu lewat timer tetap begitu `am start`
        # (ke target) terkirim, BERBARENGAN dengan Smart Wait join, bukan
        # menunggu join_status selesai (yang bisa sampai JOIN_TIMEOUT_SECONDS
        # + JOIN_VERIFY_GRACE_SECONDS kalau UNCERTAIN).
        join_status, join_reason = await _launch_then_freeform_soon(
            pkg, intent_url, timeout_seconds,
            require_join_signal=True, session_id=session_id,
        )
    except Exception:
        log.error(f"SESSION_AGENT: exception saat join target {pkg}.", exc_info=True)
        join_status, join_reason = "FAILED", "EXCEPTION"

    if not _still_current():
        return

    if join_status == "FAILED":
        SESSIONS[pkg]["status"] = "FAILED"
        await _emit_status(local_device_id, session_id, pkg, order_id, "START_FAILED",
                            reason=f"JOIN_FAILED:{join_reason}")
        return

    if join_status == "UNCERTAIN":
        # LIFECYCLE REVISION (Masalah #1): proses hidup, tidak ada keyword
        # sukses MAUPUN bukti kegagalan sampai JOIN_TIMEOUT_SECONDS habis.
        # JANGAN langsung START_FAILED (kasus nyata: Roblox sudah masuk
        # game, cuma keyword logcat lama tidak reliable di client ini).
        # Beri satu grace period singkat untuk observasi tambahan sebelum
        # diterima sebagai fallback -- bukan fake RUNNING tanpa pengecekan
        # sama sekali.
        log.info(f"[VERIFY] session={session_id} package={pkg} state=VERIFYING_GAME "
                  f"reason={join_reason}")
        await _emit_status(local_device_id, session_id, pkg, order_id, "VERIFYING_GAME",
                            reason=join_reason)

        grace_start_str = datetime.datetime.now().strftime('%m-%d %H:%M:%S.000')
        await asyncio.sleep(JOIN_VERIFY_GRACE_SECONDS)

        if not _still_current():
            return

        pid_after_grace = get_pid_quick(pkg)
        if not pid_after_grace:
            log.warning(f"[VERIFY] session={session_id} package={pkg} mati selama grace-check "
                        f"-> START_FAILED.")
            SESSIONS[pkg]["status"] = "FAILED"
            await _emit_status(local_device_id, session_id, pkg, order_id, "START_FAILED",
                                reason="PROCESS_DIED_DURING_VERIFY")
            return

        try:
            has_failure, failure_code = await asyncio.to_thread(
                has_recent_disconnect_signal, pkg, grace_start_str,
            )
        except Exception:
            log.error(f"SESSION_AGENT: exception grace-check disconnect signal {pkg}.",
                      exc_info=True)
            has_failure, failure_code = False, None

        if has_failure:
            log.warning(f"[VERIFY] session={session_id} package={pkg} bukti disconnect asli "
                        f"(reason={failure_code}) selama grace-check -> START_FAILED.")
            SESSIONS[pkg]["status"] = "FAILED"
            await _emit_status(local_device_id, session_id, pkg, order_id, "START_FAILED",
                                reason=f"JOIN_ERROR_SIGNAL_DURING_VERIFY_{failure_code}")
            return

        log.info(f"[VERIFY] session={session_id} package={pkg} target verified (fallback: proses "
                 f"hidup, tidak ada bukti kegagalan setelah grace-check) -> RUNNING.")
        # lanjut ke Tahap 4 seperti SUCCESS biasa.

    # Freeform utamanya SUDAH dipicu lewat timer di dalam
    # _launch_then_freeform_soon() di atas (jauh sebelum baris ini
    # tercapai). Panggilan ini murni RE-VERIFY tambahan -- package baru saja
    # pindah/join ke target (intent baru, task bisa saja berubah lagi
    # setelah timer freeform lewat, terutama kalau UNCERTAIN sempat kena
    # grace period) -- idempotent & aman dipanggil ulang, sekaligus
    # bangkitkan lagi sibling package lain kalau sempat ketutup.
    await _activate_freeform_and_restore_siblings(pkg, session_id)
    if not _still_current():
        return

    # --- Tahap 4: RUNNING -- SATU-SATUNYA titik joki benar-benar dimulai. ---
    SESSIONS[pkg]["status"] = "ACTIVE"  # nilai internal SESSIONS tetap "ACTIVE" (dipakai watchdog)
    SESSIONS[pkg]["pid"] = get_pid_quick(pkg) or "-"

    _ensure_watchdog(pkg)
    _ensure_username_scanner(pkg)  # PHASE 4.5 (lihat D2) -- tetap jalan untuk tampilan heartbeat
    _ensure_error_watcher()  # deteksi disconnect logcat (Error 266/267/277/279/280), lihat bawah

    await _emit_status(local_device_id, session_id, pkg, order_id, "RUNNING")
    log.info(f"SESSION_AGENT: {pkg} (session {session_id}) RUNNING -- timer customer dimulai sekarang.")


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


async def _launch_then_freeform_soon(pkg: str, intent_url: str, timeout_seconds: int,
                                      require_join_signal: bool, session_id: str,
                                      only_if_previously_freeform: bool = False):
    """IMPROVE ANDROID 12 (Timer utk Proses Freeform) -- pengganti pola lama
    `launch_and_wait(..., defer_freeform=True)` lalu (SETELAH selesai)
    `await _activate_freeform_and_restore_siblings(...)`.

    Sekarang: `am start` dulu (launch_normal(), cepat) -> jadwalkan
    activate_freeform()+restore siblings lewat TIMER TETAP
    (FREEFORM_ACTIVATION_DELAY_SECONDS), BERBARENGAN (bukan SETELAH) dengan
    Smart Wait (wait_for_launch_signal()) yang tetap jalan seperti biasa
    untuk menentukan status lobby/join sebenarnya. Freeform TIDAK LAGI
    menunggu hasil Smart Wait -- package lain yang sudah Freeform jadi jauh
    lebih cepat dibangkitkan lagi, tidak lama nunggu di background gara-gara
    package ini masih diverifikasi.

    only_if_previously_freeform (dipakai _headless_watchdog_loop untuk
    relaunch setelah crash): kalau True, timer TETAP jalan tapi
    activate_freeform() hanya benar-benar dipanggil kalau pkg SUDAH pernah
    tercatat Freeform sebelumnya di _FREEFORM_REGISTRY (perilaku lama --
    package yang belum pernah Freeform tidak dipaksa Freeform cuma karena
    relaunch crash).

    Return-nya PERSIS sama seperti launch_and_wait(..., defer_freeform=True)
    dulu: bool kalau require_join_signal=False, tuple (status, reason) kalau
    True -- caller (_run_start_flow/_headless_watchdog_loop) tidak perlu
    berubah cara membaca hasilnya.
    """
    ok, start_time_str = await asyncio.to_thread(launch_normal, pkg, intent_url)
    if not ok:
        return ("FAILED", "LAUNCH_COMMAND_FAILED") if require_join_signal else False

    async def _freeform_after_delay():
        if only_if_previously_freeform and pkg not in _FREEFORM_REGISTRY:
            return
        await asyncio.sleep(FREEFORM_ACTIVATION_DELAY_SECONDS)
        await _activate_freeform_and_restore_siblings(pkg, session_id)

    freeform_task = asyncio.create_task(_freeform_after_delay())
    try:
        result = await asyncio.to_thread(
            wait_for_launch_signal, pkg, start_time_str, timeout_seconds, require_join_signal,
        )
    finally:
        # Smart Wait butuh minimal beberapa detik (fast-fail PID check tiap
        # 3 detik), jadi normalnya freeform_task (2-3 detik) SUDAH selesai
        # duluan pas titik ini tercapai -- await di sini murni jaga-jaga
        # (supaya exception di dalamnya ke-log, bukan silently swallowed)
        # dan TIDAK menambah waktu tunggu berarti untuk caller.
        try:
            await freeform_task
        except Exception:
            log.error(f"SESSION_AGENT: exception freeform-timer task {pkg}.", exc_info=True)

    return result


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
    """PHASE 4.5 (lihat D2 di PROJECT_CONTEXT.txt): scan berkala username
    akun Roblox yang sedang login di package ini, SELAMA package tsb masih
    dikelola SESSIONS -- lifecycle SAMA seperti _headless_watchdog_loop
    (mulai bareng START_SESSION lewat _ensure_username_scanner, berhenti
    sendiri begitu entry package-nya dihapus dari SESSIONS oleh
    handle_stop_session).

    Perubahan akun (device ganti login) TIDAK PERNAH mengubah/merusak
    session yang sedang jalan -- session tetap diidentifikasi lewat
    session_id/order_id/device_id/package_name seperti biasa (lihat D2).
    Field username di sini CUMA informasi tambahan yang ikut nebeng di
    HEARTBEAT (packages[pkg]['username']) untuk visibilitas staff -- tidak
    memengaruhi logic START_SESSION/STOP_SESSION/watchdog sama sekali.
    """
    log.info(f"SESSION_AGENT: username scanner mulai untuk {pkg} "
             f"(interval {USERNAME_SCAN_INTERVAL_SECONDS}s).")
    try:
        while pkg in SESSIONS:
            try:
                prev = SESSIONS.get(pkg, {}).get("_last_logged_username", "<belum discan>")
                new_username = await asyncio.to_thread(username_scanner.scan_username_blocking, pkg)
                if pkg in SESSIONS and new_username != prev:
                    # Log CUMA kalau berubah -- jangan spam log tiap tick 60 detik.
                    log.info(f"SESSION_AGENT: username {pkg} -> "
                             f"{new_username or '(tidak diketahui)'} (sebelumnya: {prev}).")
                    SESSIONS[pkg]["_last_logged_username"] = new_username
            except Exception:
                log.error(f"SESSION_AGENT: exception scan username {pkg}.", exc_info=True)
            await asyncio.sleep(USERNAME_SCAN_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        raise
    finally:
        log.info(f"SESSION_AGENT: username scanner berhenti untuk {pkg}.")
        _username_tasks.pop(pkg, None)
        username_scanner.forget(pkg)


# [FINISH] LOGIC BARU: jeda setelah target PID dipastikan mati, SEBELUM
# mulai foreground-restore survivor -- berdasarkan hasil test manual
# (~2-3 detik, lihat instruksi FINISH LOGIC BARU), supaya Android sempat
# memproses perubahan window akibat target mati dulu sebelum di-monkey.
_FINISH_RESTORE_DELAY_SECONDS = 2.5

# Jeda antar panggilan `monkey` per surviving package -- SETIAP package
# WAJIB dipanggil individual (satu "monkey" cuma membawa satu package),
# jeda kecil ini cuma supaya panggilan beruntun tidak asal numpuk.
_FINISH_RESTORE_STEP_DELAY_SECONDS = 0.3


async def _finish_kill_and_restore_survivors(
    local_device_id: str, session_id: str, pkg: str, pid: str,
) -> None:
    """[FINISH] LOGIC BARU (GANTI PENUH _send_package_to_lobby lama --
    lama SENGAJA TIDAK ADA LAGI, tidak dipertahankan sebagai fallback):
    dipanggil (fire-and-forget lewat asyncio.create_task) HANYA dari
    handle_stop_session, SETELAH SESSIONS[pkg] di-pop untuk session ini.

    Alur (lihat hasil test manual di instruksi FINISH LOGIC BARU):
    STEP 1-2: kill PID target (`kill`, BUKAN `am force-stop`) lewat
    process_manager.kill_pid_direct() -- HANYA menyentuh PID tunggal ini,
    `kill -9` cuma fallback kalau `kill` polos belum berhasil.
    STEP 3-4: package survivor lain (yang statusnya masih ACTIVE di
    SESSIONS) kemungkinan berubah jadi bubble/minimized secara visual
    akibat window manager host -- BUKAN berarti mati. Package-package ini
    TIDAK PERNAH di-kill/force-stop/restart/rejoin di sini, cuma
    dikembalikan ke foreground SATU PER SATU lewat
    process_manager.restore_foreground() (`monkey -p <package> 1`).

    ISOLASI (WAJIB): `kill <pid>` HANYA menyentuh proses tunggal target,
    dan `monkey -p <package> 1` per survivor HANYA membawa package itu ke
    foreground (bukan restart/rejoin) -- tidak ada baris di fungsi ini
    yang mengubah state monitor/session package lain.
    """
    log.info(f"[FINISH] session={session_id} package={pkg} pid={pid} -- mulai kill PID target "
             f"(kill biasa, bukan am force-stop)...")

    if pid and pid != "-":
        killed = await asyncio.to_thread(process_manager.kill_pid_direct, pid)
        if killed:
            log.info(f"[FINISH] session={session_id} package={pkg} pid={pid} -- target dipastikan mati.")
        else:
            log.error(f"[FINISH] session={session_id} package={pkg} pid={pid} -- target GAGAL "
                      f"dipastikan mati setelah kill+fallback, staff perlu cek manual.")
    else:
        log.warning(f"[FINISH] session={session_id} package={pkg} -- tidak ada PID tercatat untuk "
                    f"di-kill (mungkin sudah mati duluan), lanjut ke foreground-restore survivor saja.")

    # STEP 3-4: tunggu singkat (hasil test manual: ~2-3 detik) sebelum
    # restore, supaya Android sempat memproses perubahan window dulu.
    await asyncio.sleep(_FINISH_RESTORE_DELAY_SECONDS)

    # Surviving package: ambil dari data session yang memang sudah
    # dimonitor sistem (SESSIONS), bukan menebak dari daftar proses
    # Android secara acak -- prioritas status ACTIVE/RUNNING saja.
    survivors = [p for p, info in SESSIONS.items() if info.get("status") == "ACTIVE"]
    if not survivors:
        log.info(f"[FINISH] session={session_id} package={pkg} -- tidak ada surviving package lain, selesai.")
        return

    log.info(f"[FINISH] session={session_id} package={pkg} -- restore foreground {len(survivors)} "
             f"surviving package satu per satu: {survivors}")
    for survivor_pkg in survivors:
        # Re-cek per iterasi (race condition): kalau survivor ini di-Finish
        # atau diklaim session baru selagi loop restore ini masih jalan,
        # jangan sentuh -- biarkan alurnya sendiri yang menangani.
        if survivor_pkg not in SESSIONS or SESSIONS[survivor_pkg].get("status") != "ACTIVE":
            continue
        ok = await asyncio.to_thread(process_manager.restore_foreground, survivor_pkg)
        log.info(f"[FINISH] session={session_id} restore foreground survivor {survivor_pkg}: "
                 f"{'OK' if ok else 'GAGAL'}")
        await asyncio.sleep(_FINISH_RESTORE_STEP_DELAY_SECONDS)

    log.info(f"[FINISH] session={session_id} package={pkg} -- proses restore foreground survivor selesai.")


async def handle_stop_session(msg: dict, local_device_id: str, do_reset: bool = True) -> dict:
    """PHASE 6/7 (FINISH LOGIC BARU): eksekusi STOP_SESSION. Return dict
    payload COMMAND_RESULT, tidak pernah raise ke pemanggil (sama seperti
    handle_start_session).

    PERUBAHAN PENTING (GANTI PENUH pendekatan "tanpa kill" sebelumnya --
    TIDAK dipertahankan sebagai fallback): target package di-kill lewat
    PID (`kill <pid>`, BUKAN `am force-stop`), lalu surviving package lain
    (status ACTIVE di SESSIONS) yang mungkin ikut berubah jadi
    bubble/minimized secara visual (efek window manager host, BUKAN ikut
    mati) dikembalikan ke foreground SATU PER SATU lewat `monkey -p
    <package> 1` -- lihat _finish_kill_and_restore_survivors() untuk detail.

    Urutan:
    1. Set status STOPPED & hapus entry SESSIONS -- ini yang bikin
       _headless_watchdog_loop skip relaunch-ke-game-lama (anti-rejoin),
       supaya tidak ada jendela waktu watchdog masih sempat mengarahkan
       proses balik ke target lama. PID target diambil SEBELUM entry
       di-pop, supaya kill di background tetap punya PID yang benar.
    2. Balas COMMAND_RESULT STOPPED SEGERA -- bot/staff tidak perlu
       menunggu kill+restore selesai.
    3. Fire-and-forget: _finish_kill_and_restore_survivors() mengeksekusi
       kill target + restore foreground survivor di background --
       package/session lain di SESSIONS (atau yang dipantau menu manual)
       TIDAK di-kill/force-stop/restart, cuma yang statusnya ACTIVE yang
       dibawa balik ke foreground.
    """
    session_id = str(msg.get("session_id", "")).strip()
    pkg = str(msg.get("package_name", "")).strip()

    if not session_id or not pkg:
        log.error(f"SESSION_AGENT: STOP_SESSION tidak lengkap (session={session_id}, pkg={pkg}).")
        return {"type": "COMMAND_RESULT", "command": "STOP_SESSION", "device_id": local_device_id,
                "session_id": session_id, "ok": False, "reason": "MISSING_FIELDS"}

    info = SESSIONS.get(pkg)
    if info is None:
        # Tidak ada entry di memori (mis. agent baru restart, atau package ini
        # memang tidak pernah dikelola headless dari sini). Tidak ada apa pun
        # buat ditandai selesai dari sisi kita -- balas ok supaya bot tidak
        # nyangkut nunggu; penyelarasan state penuh menyusul SYNC_SESSIONS
        # (Phase 8).
        log.warning(f"SESSION_AGENT: STOP_SESSION untuk {pkg} (session {session_id}) tapi tidak "
                    f"ada entry SESSIONS di memori -- mungkin agent baru restart.")
        return {"type": "COMMAND_RESULT", "command": "STOP_SESSION", "device_id": local_device_id,
                "session_id": session_id, "ok": True, "reason": "NO_LOCAL_SESSION"}

    if info.get("session_id") != session_id:
        # session_id tidak cocok session yang SEDANG dipegang SESSIONS[pkg] --
        # command basi (untuk session lama yang sudah tergantikan session baru
        # di package yang sama). JANGAN sentuh apa pun -- itu bisa mengganggu
        # session BARU yang sedang jalan, bukan yang diminta stop.
        log.warning(f"SESSION_AGENT: STOP_SESSION session_id '{session_id}' tidak cocok dengan "
                    f"session aktif '{info.get('session_id')}' di {pkg}, diabaikan (command basi).")
        return {"type": "COMMAND_RESULT", "command": "STOP_SESSION", "device_id": local_device_id,
                "session_id": session_id, "ok": False, "reason": "STALE_SESSION_ID"}

    # (1) Anti-rejoin: set SEBELUM apa pun lainnya. _headless_watchdog_loop
    # cuma bertindak kalau status == "ACTIVE", jadi begitu ini berubah,
    # watchdog otomatis skip package ini di siklus berikutnya (lihat loop di
    # bawah).
    # PID target diambil SEBELUM di-pop -- ini yang dipakai kill di
    # background (STEP 1 FINISH LOGIC BARU: pakai PID yang sudah dimiliki
    # session ini, jangan langsung `am force-stop`).
    target_pid = info.get("pid", "-")
    info["status"] = "STOPPED"
    info["pid"] = "-"
    # (2) Hapus entry SETELAH status STOPPED ter-set -- supaya kalaupun
    # watchdog sempat bangun tepat di titik ini, ia lihat status STOPPED
    # dulu (bukan entry hilang tiba-tiba), lalu keluar dari loop-nya dengan
    # bersih. Pop JUGA dilakukan SEBELUM kill di background dijalankan,
    # supaya loop restore-survivor (_finish_kill_and_restore_survivors)
    # tidak pernah menganggap package ini sendiri sebagai survivor.
    SESSIONS.pop(pkg, None)
    _FREEFORM_REGISTRY.pop(pkg, None)  # jangan ikut di-restore lagi oleh rotasi freeform package lain

    log.info(f"[FINISH] session={session_id} package={pkg} pid={target_pid} -- STOP_SESSION "
             f"diterima, kill target + restore-foreground survivor berjalan di background.")

    # (3) Fire-and-forget: TIDAK menunda COMMAND_RESULT di bawah ini --
    # bot/staff tetap dapat balasan STOPPED secepat mungkin, kill target +
    # restore-foreground survivor (lihat docstring
    # _finish_kill_and_restore_survivors) berjalan di background.
    # do_reset=False tersedia untuk pemanggil lain di masa depan yang butuh
    # stop TANPA kill+restore (tidak dipakai saat ini -- semua caller
    # existing/reconcile_sync tetap dapat perilaku ini).
    if do_reset:
        asyncio.create_task(
            _finish_kill_and_restore_survivors(local_device_id, session_id, pkg, target_pid)
        )

    return {"type": "COMMAND_RESULT", "command": "STOP_SESSION", "device_id": local_device_id,
            "session_id": session_id, "ok": True, "reason": "STOPPED"}


async def _headless_watchdog_loop(pkg: str) -> None:
    """Watchdog ringan khusus package yang dikontrol agent. Cuma menjaga PID
    tetap hidup -- begitu terdeteksi mati, delegasikan ke
    _run_package_recovery() (kill jaga-jaga -> lobby -> delay 60-90s ->
    join ulang) SUPAYA PERSIS SAMA alurnya dengan recovery yang dipicu
    deteksi disconnect logcat (_handle_disconnect_event), bukan relaunch
    langsung ke target seperti sebelumnya. BUKAN pengganti tiering
    recovery_manager.py yang dipakai mode manual/menu.

    Anti-rejoin (PHASE 6/7, SUDAH AKTIF): loop ini HANYA bertindak kalau
    status == "ACTIVE" (lihat cek di bawah) -- begitu handle_stop_session()
    mengubah status jadi STOPPED, watchdog otomatis skip package itu di
    siklus berikutnya, tidak perlu logic tambahan. Loop berhenti sendiri
    kalau entry package-nya dihapus dari SESSIONS (dilakukan handle_stop_session
    segera, SEBELUM kill target PID di background dijalankan lewat
    _finish_kill_and_restore_survivors -- watchdog package ini sendiri
    tidak perlu ikut menunggu kill selesai, karena entry-nya sudah tidak
    ada di SESSIONS begitu Finish diterima).
    """
    log.info(f"SESSION_AGENT: watchdog headless mulai untuk {pkg}.")
    try:
        while pkg in SESSIONS:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
            info = SESSIONS.get(pkg)
            if info is None:
                break
            if info["status"] not in ("ACTIVE",):
                # LIFECYCLE REVISION: watchdog cuma dibuat (_ensure_watchdog)
                # SETELAH RUNNING tercapai (lihat _run_start_flow), jadi
                # cabang ini praktis tidak pernah kena PREPARING/WAITING_LOGIN/
                # ACCOUNT_READY/JOINING_GAME -- tetap dijaga untuk keamanan.
                # Termasuk skip kalau statusnya "RECOVERING" (recovery lain
                # -- mis. dari disconnect logcat -- sedang berjalan).
                continue

            current_pid = process_manager.get_pid(pkg)
            if current_pid and current_pid == info.get("pid"):
                continue  # masih hidup, normal

            if pkg in _recovery_in_progress:
                continue  # sudah/baru saja ditangani jalur recovery lain

            info["crash_count"] += 1
            session_id = info["session_id"]
            log.warning(f"SESSION_AGENT: {pkg} terdeteksi mati (session {session_id}), "
                        f"memulai recovery (kill->lobby->delay->join ulang)...")
            info["status"] = "RECOVERING"
            _recovery_in_progress.add(pkg)
            # Di-await LANGSUNG (bukan create_task) -- loop ini SATU per
            # package, jadi menunggu recovery package ini selesai di sini
            # tidak memblokir watchdog package LAIN (masing-masing task
            # terpisah, lihat _ensure_watchdog).
            await _run_package_recovery(pkg, session_id, "PID_CRASH")
    except asyncio.CancelledError:
        raise
    finally:
        log.info(f"SESSION_AGENT: watchdog headless berhenti untuk {pkg}.")
        _watchdog_tasks.pop(pkg, None)


def _ensure_error_watcher() -> None:
    """Pastikan cuma ADA SATU consumer task global untuk event disconnect
    logcat (error_detector.py sendiri sudah singleton internal, ini cuma
    memastikan task Python-nya juga tidak numpuk kalau dipanggil berkali-kali
    dari beberapa START_SESSION)."""
    global _error_watcher_task
    if _error_watcher_task is not None and not _error_watcher_task.done():
        return
    start_error_detector()
    _error_watcher_task = asyncio.create_task(_error_event_consumer_loop())


async def _error_event_consumer_loop() -> None:
    """Drain queue error_detector.py (deteksi disconnect Roblox in-game via
    logcat reason 266/267/277/279/280) lalu cocokkan PID di event ke
    package/akun yang sedang ACTIVE di SESSIONS -- SATU package saja yang
    kena recovery (identifikasi via kombinasi package+PID, lihat
    _handle_disconnect_event), package lain tetap farming seperti biasa."""
    log.info("SESSION_AGENT: error watcher (deteksi disconnect logcat) mulai.")
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
                    log.error("SESSION_AGENT: exception memproses event disconnect logcat.",
                              exc_info=True)
    except asyncio.CancelledError:
        raise


async def _handle_disconnect_event(event: dict) -> None:
    """Cocokkan PID dari event error_detector.py ke package yang SEDANG
    ACTIVE di SESSIONS (identifikasi package+PID sekaligus, bukan cuma PID
    mentah, supaya tidak salah sasaran ke package yang bukan joki/sudah
    berhenti). Kalau tidak ada yang cocok (mis. PID dari package non-joki
    di device yang sama, atau package itu sudah dalam recovery/berhenti),
    event ini dilewati saja."""
    pid = str(event.get("pid") or "").strip()
    reason = event.get("reason")
    if not pid:
        return

    target_pkg = None
    for pkg, info in list(SESSIONS.items()):
        if info.get("status") == "ACTIVE" and str(info.get("pid") or "") == pid:
            target_pkg = pkg
            break

    if not target_pkg or target_pkg in _recovery_in_progress:
        return

    info = SESSIONS.get(target_pkg)
    if not info:
        return

    session_id = info["session_id"]
    username = info.get("expected_username") or "?"
    info["status"] = "RECOVERING"
    _recovery_in_progress.add(target_pkg)
    log.warning(f"SESSION_AGENT: [DISCONNECT] {target_pkg} (akun {username}, session {session_id}) "
                f"terdeteksi Error {reason} (PID {pid}) -- memulai recovery.")
    # create_task (BUKAN await langsung) -- loop consumer ini SATU untuk
    # SEMUA package, jangan sampai tertahan nunggu recovery 60-90+ detik
    # package ini sementara package LAIN yang juga disconnect tidak terproses.
    asyncio.create_task(_run_package_recovery(target_pkg, session_id, f"LOGCAT_{reason}"))


async def _run_package_recovery(pkg: str, session_id: str, trigger_reason: str) -> None:
    """Recovery INDEPENDEN per-package: kill (jaga-jaga kalau proses masih
    hidup -- disconnect logcat BUKAN berarti proses mati) -> launch ke
    LOBBY -> tunggu acak 60-90 detik (RECOVERY_LOBBY_WAIT_*_SECONDS) supaya
    Roblox benar-benar siap -> join ulang ke target yang SAMA -> kembali
    farming. Dipanggil baik dari watchdog PID-crash maupun consumer
    disconnect logcat -- keduanya berakhir di sini supaya perilakunya PERSIS
    SAMA, tidak ada dua alur recovery yang bisa divergen.

    Retry TANPA BATAS (permintaan eksplisit, TIDAK dihapus) -- tapi supaya
    polanya tidak "terburu-buru"/mekanis (yang bisa keliatan seperti
    brute-force dari sisi anti-fraud Roblox kalau errornya keseringan),
    dua lapis pengaman DITAMBAHKAN, fungsinya tetap sama:
      1. Jeda kecil acak sebelum kill (RECOVERY_PRE_KILL_JITTER_*).
      2. Delay antar percobaan BUKAN angka tetap lagi, tapi backoff
         eksponensial + jitter (_recovery_backoff_delay) yang membesar
         tiap gagal, dan begitu lewat RECOVERY_CIRCUIT_BREAKER_THRESHOLD
         kali gagal berturut-turut, masuk mode cooldown acak 5-10 menit
         per putaran -- TETAP mengulang selamanya, cuma jauh lebih jarang.
    Berhenti hanya kalau berhasil ATAU session ini sudah diganti/distop
    (_still_current() jadi False, mis. staff pencet Finish/Assign ulang
    selagi recovery masih berjalan).

    Package LAIN yang sedang farming TIDAK PERNAH disentuh oleh fungsi ini
    (cuma memanggil launcher/process_manager untuk `pkg` yang diberikan).
    """
    def _still_current() -> bool:
        current = SESSIONS.get(pkg)
        return bool(current and current.get("session_id") == session_id)

    try:
        if not _still_current():
            return

        info = SESSIONS[pkg]
        target = info.get("target")
        expected_username = info.get("expected_username") or "?"

        attempt = 0
        while _still_current():
            attempt += 1
            info = SESSIONS[pkg]
            info["status"] = "RECOVERING"
            log.warning(f"SESSION_AGENT: [RECOVERY] {pkg} (akun {expected_username}, "
                        f"session {session_id}) percobaan #{attempt} -- alasan: {trigger_reason}.")

            # --- Jeda kecil acak sebelum kill (biar transisinya gak instan) ---
            await asyncio.sleep(random.uniform(
                RECOVERY_PRE_KILL_JITTER_MIN_SECONDS, RECOVERY_PRE_KILL_JITTER_MAX_SECONDS,
            ))

            if not _still_current():
                return

            # --- Kill dulu KALAU proses masih hidup. Disconnect logcat bisa
            # terjadi selagi proses Roblox-nya SENDIRI masih berjalan (cuma
            # koneksi game-nya yang putus), beda dari crash PID yang memang
            # sudah mati duluan -- jadi step ini dibuat aman untuk kedua
            # kasus (tidak ada-apa kalau memang sudah mati).
            current_pid = await asyncio.to_thread(process_manager.get_pid, pkg)
            if current_pid:
                await asyncio.to_thread(process_manager.graceful_kill, current_pid, pkg)
                await asyncio.to_thread(process_manager.wait_until_process_dead, current_pid, 10)

            if not _still_current():
                return

            # --- Launch ke LOBBY dulu (BUKAN langsung ke target) ---
            try:
                lobby_ok = await _launch_then_freeform_soon(
                    pkg, get_lobby_intent(), LOBBY_TIMEOUT_SECONDS,
                    require_join_signal=False, session_id=session_id,
                    only_if_previously_freeform=True,
                )
            except Exception:
                log.error(f"SESSION_AGENT: [RECOVERY] exception buka lobby {pkg}.", exc_info=True)
                lobby_ok = False

            if not _still_current():
                return

            if not lobby_ok:
                delay = _recovery_backoff_delay(attempt)
                log.warning(f"SESSION_AGENT: [RECOVERY] {pkg} gagal buka lobby, retry dalam "
                            f"{delay:.0f}s (percobaan #{attempt})...")
                await asyncio.sleep(delay)
                continue

            info["pid"] = get_pid_quick(pkg) or "-"

            # --- Tunggu acak 60-90 detik di lobby sebelum join ulang ---
            wait_seconds = random.uniform(
                RECOVERY_LOBBY_WAIT_MIN_SECONDS, RECOVERY_LOBBY_WAIT_MAX_SECONDS,
            )
            log.info(f"SESSION_AGENT: [RECOVERY] {pkg} di lobby, menunggu {wait_seconds:.0f}s "
                     f"sebelum join ulang ke target...")
            await asyncio.sleep(wait_seconds)

            if not _still_current():
                return

            # --- Join ulang ke target yang SAMA ---
            try:
                intent_url = get_intent_url(target)
            except ValueError as e:
                delay = _recovery_backoff_delay(attempt)
                log.error(f"SESSION_AGENT: [RECOVERY] {pkg} target tidak valid lagi ({e}), retry "
                          f"dalam {delay:.0f}s (percobaan #{attempt})...")
                await asyncio.sleep(delay)
                continue

            joined = await _attempt_join_target(pkg, session_id, intent_url, DEFAULT_TIMEOUT_SECONDS)

            if not _still_current():
                return

            if joined:
                info = SESSIONS.get(pkg)
                if info is not None:
                    info["status"] = "ACTIVE"
                    info["pid"] = get_pid_quick(pkg) or "-"
                    info["launch_count"] = info.get("launch_count", 0) + 1
                await _activate_freeform_and_restore_siblings(pkg, session_id)
                log.info(f"SESSION_AGENT: [RECOVERY] {pkg} (akun {expected_username}) berhasil "
                         f"kembali farming (percobaan #{attempt}).")
                return

            delay = _recovery_backoff_delay(attempt)
            log.warning(f"SESSION_AGENT: [RECOVERY] {pkg} gagal join ulang, retry dalam "
                        f"{delay:.0f}s (percobaan #{attempt})...")
            await asyncio.sleep(delay)
    finally:
        _recovery_in_progress.discard(pkg)


async def _attempt_join_target(pkg: str, session_id: str, intent_url: str, timeout_seconds: int) -> bool:
    """Coba join ke target game -- dipakai KHUSUS oleh _run_package_recovery
    (bukan Tahap 3 di _run_start_flow). Logic join_status/UNCERTAIN/
    grace-check di sini SENGAJA disalin (bukan direfactor jadi satu fungsi
    bersama dengan _run_start_flow) supaya alur start awal yang sudah teruji
    tidak ikut berubah risikonya. Tidak pernah mengirim SESSION_STATUS --
    post-RUNNING pesan itu selalu diabaikan bot.py (lihat
    handle_session_status: session yang sudah RUNNING mengabaikan status
    apa pun berikutnya untuk mencegah timer customer ke-reset), jadi cukup
    logging lokal di device."""
    def _still_current() -> bool:
        current = SESSIONS.get(pkg)
        return bool(current and current.get("session_id") == session_id)

    try:
        join_status, join_reason = await _launch_then_freeform_soon(
            pkg, intent_url, timeout_seconds,
            require_join_signal=True, session_id=session_id,
        )
    except Exception:
        log.error(f"SESSION_AGENT: [RECOVERY] exception saat join target {pkg}.", exc_info=True)
        return False

    if not _still_current():
        return False

    if join_status == "FAILED":
        log.warning(f"SESSION_AGENT: [RECOVERY] {pkg} join gagal: {join_reason}")
        return False

    if join_status == "UNCERTAIN":
        log.info(f"SESSION_AGENT: [RECOVERY] {pkg} join UNCERTAIN ({join_reason}), grace-check...")
        grace_start_str = datetime.datetime.now().strftime('%m-%d %H:%M:%S.000')
        await asyncio.sleep(JOIN_VERIFY_GRACE_SECONDS)

        if not _still_current():
            return False

        pid_after_grace = get_pid_quick(pkg)
        if not pid_after_grace:
            log.warning(f"SESSION_AGENT: [RECOVERY] {pkg} mati selama grace-check.")
            return False

        try:
            has_failure, failure_code = await asyncio.to_thread(
                has_recent_disconnect_signal, pkg, grace_start_str,
            )
        except Exception:
            log.error(f"SESSION_AGENT: [RECOVERY] exception grace-check disconnect {pkg}.",
                      exc_info=True)
            has_failure, failure_code = False, None

        if has_failure:
            log.warning(f"SESSION_AGENT: [RECOVERY] {pkg} bukti disconnect asli "
                        f"(reason={failure_code}) selama grace-check.")
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
    """PHASE 8: cocokkan SESSIONS (state fisik device) terhadap daftar
    session non-terminal versi bot (expected_sessions -- list of dict,
    masing2 {session_id, package_name, status, target, order_id}).

    ATURAN (strict -- lihat requirement 'jangan hidupkan kembali session
    yang expired/stopped, jangan ganggu session/package lain'):

    1. Package yang device SEDANG track (SESSIONS) tapi TIDAK disebut sama
       sekali di expected_sessions -> bot sudah anggap ini selesai/tidak
       tahu apa-apa lagi soal ini -> ORPHAN, WAJIB dihentikan. Reuse
       handle_stop_session() apa adanya supaya perilakunya PERSIS sama
       seperti STOP_SESSION normal (tanpa kill, trigger balik ke Lobby,
       anti-rejoin tetap terjaga).

    2. Package yang bot minta status EXPIRING/STOPPING -> device WAJIB
       pastikan itu berhenti, SAMA seperti (1) -- ini menutup celah kalau
       STOP_SESSION asli sempat tidak nyampe pas disconnect (device masih
       mengira ACTIVE).

    3. Package yang bot minta status non-terminal apa pun (ASSIGNED/
       PREPARING/WAITING_LOGIN/ACCOUNT_READY/JOINING_GAME/RUNNING, atau
       legacy STARTING/ACTIVE), DAN device SUDAH punya entry SESSIONS
       dengan session_id yang SAMA -> cocok, TIDAK ADA aksi (watchdog+
       scanner yang sudah jalan dibiarkan lanjut apa adanya -- tidak
       di-restart/diganggu; kalau masih di tengah _run_start_flow, flow itu
       jalan terus tanpa terganggu SYNC).

    4. Package yang bot minta status non-terminal tapi device TIDAK punya
       entry (mis. proses CARRERA-HUB sendiri sempat restart, memori
       SESSIONS hilang) -> cek PID FISIK via process_manager.get_pid().
       - Proses TERNYATA masih hidup -> ADOPSI: lanjutkan monitoring
         (watchdog + username scanner baru) TANPA relaunch (bukan proses
         baru, cuma resume tracking).
         CATATAN/KETERBATASAN (LIFECYCLE REVISION): adopsi ini TIDAK
         mengulang validasi username/join -- kalau device restart PERSIS
         di tengah WAITING_LOGIN/JOINING_GAME (bukan RUNNING), proses yang
         diadopsi bisa saja masih di akun/layar yang salah. Ini AMAN untuk
         timer customer (bot TIDAK menganggap RUNNING dari sini -- RUNNING
         cuma lewat SESSION_STATUS eksplisit dari _run_start_flow, lihat
         modul ini bag. atas), tapi staff sebaiknya cek manual kalau kasus
         ini terjadi. Perbaikan penuh (re-run staged flow saat adopsi)
         belum diimplementasikan -- di luar prioritas tahap ini.
       - Proses TIDAK hidup -> JANGAN auto-launch (bukan wewenang sync,
         cuma START_SESSION eksplisit yang boleh launch) -- cukup laporkan
         apa adanya (tidak ada entry) lewat SYNC_RESPONSE, bot yang
         putuskan langkah lanjutan (lihat handle_sync_response di bot.py,
         akan tandai SYNC_LOST).

    5. Package/session lain yang tidak disebut di KEDUA sisi (mis. dipantau
       lewat menu manual, di luar SESSIONS/expected_sessions) TIDAK PERNAH
       disentuh -- package isolation tetap berlaku penuh saat sync.

    Return: snapshot SESSIONS TERKINI (setelah reconcile) buat dikirim
    balik sebagai payload SYNC_RESPONSE.
    """
    expected_by_pkg = {}
    for entry in expected_sessions:
        pkg = str(entry.get("package_name", "")).strip()
        if pkg:
            expected_by_pkg[pkg] = entry

    # (1) Orphan cleanup -- package yang device pegang tapi TIDAK disebut bot sama sekali.
    for pkg in list(SESSIONS.keys()):
        if pkg in expected_by_pkg:
            continue
        local_session_id = SESSIONS[pkg].get("session_id", "")
        log.warning(f"SESSION_AGENT: SYNC -- {pkg} (session {local_session_id}) tidak ada di "
                    f"daftar bot, dianggap orphan, dihentikan.")
        await handle_stop_session({"session_id": local_session_id, "package_name": pkg}, local_device_id)

    # (2)+(3)+(4) -- proses tiap entry yang bot harapkan.
    for pkg, entry in expected_by_pkg.items():
        desired_status = str(entry.get("status", "")).upper()
        expected_session_id = str(entry.get("session_id", "")).strip()
        local = SESSIONS.get(pkg)

        if desired_status in ("EXPIRING", "STOPPING"):
            if local and local.get("session_id") == expected_session_id:
                log.info(f"SESSION_AGENT: SYNC -- {pkg} (session {expected_session_id}) status bot "
                         f"'{desired_status}', pastikan berhenti.")
                await handle_stop_session(
                    {"session_id": expected_session_id, "package_name": pkg}, local_device_id
                )
            # local kosong / session_id beda -> device sudah berhenti duluan, tidak ada aksi.
            continue

        # desired_status non-terminal apa pun -> seharusnya hidup.
        if local and local.get("session_id") == expected_session_id:
            continue  # cocok, watchdog+scanner yang sudah jalan tetap dibiarkan lanjut.

        # Tidak ada record lokal untuk session ini -- cek proses fisik.
        pid = process_manager.get_pid(pkg)
        if pid:
            SESSIONS[pkg] = {
                "session_id": expected_session_id, "order_id": entry.get("order_id"),
                "target": str(entry.get("target", "")), "status": "ACTIVE",
                "pid": pid, "launch_count": 1, "crash_count": 0,
            }
            _ensure_watchdog(pkg)
            _ensure_username_scanner(pkg)
            log.info(f"SESSION_AGENT: SYNC -- adopsi {pkg} (session {expected_session_id}), "
                     f"proses masih hidup pid={pid}.")
        else:
            log.warning(f"SESSION_AGENT: SYNC -- bot harap {pkg} (session {expected_session_id}) "
                        f"'{desired_status}' tapi tidak ada proses berjalan di device, TIDAK "
                        f"di-launch otomatis (butuh START_SESSION eksplisit dari bot).")

    return _snapshot_for_sync()

  
