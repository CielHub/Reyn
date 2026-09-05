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
import time

import datetime

from core.logger import log
from core.deeplink import get_intent_url, get_lobby_intent
from core.launcher import launch_and_wait, get_pid_quick
from core.join_verifier import has_recent_disconnect_signal
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
        lobby_ok = await asyncio.to_thread(
            launch_and_wait, pkg, get_lobby_intent(), LOBBY_TIMEOUT_SECONDS,
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
        join_status, join_reason = await asyncio.to_thread(
            launch_and_wait, pkg, intent_url, timeout_seconds, True,  # require_join_signal
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

    # --- Tahap 4: RUNNING -- SATU-SATUNYA titik joki benar-benar dimulai. ---
    SESSIONS[pkg]["status"] = "ACTIVE"  # nilai internal SESSIONS tetap "ACTIVE" (dipakai watchdog)
    SESSIONS[pkg]["pid"] = get_pid_quick(pkg) or "-"

    _ensure_watchdog(pkg)
    _ensure_username_scanner(pkg)  # PHASE 4.5 (lihat D2) -- tetap jalan untuk tampilan heartbeat

    await _emit_status(local_device_id, session_id, pkg, order_id, "RUNNING")
    log.info(f"SESSION_AGENT: {pkg} (session {session_id}) RUNNING -- timer customer dimulai sekarang.")


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
    tetap hidup (relaunch pakai target yang sama kalau crash) -- BUKAN
    pengganti tiering recovery_manager.py yang dipakai mode manual/menu.

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
                continue

            current_pid = process_manager.get_pid(pkg)
            if current_pid and current_pid == info.get("pid"):
                continue  # masih hidup, normal

            info["crash_count"] += 1
            log.warning(f"SESSION_AGENT: {pkg} terdeteksi mati (session {info['session_id']}), "
                        f"relaunch ke target yang sama...")
            try:
                intent_url = get_intent_url(info["target"])
                success = await asyncio.to_thread(launch_and_wait, pkg, intent_url, DEFAULT_TIMEOUT_SECONDS)
            except Exception:
                log.error(f"SESSION_AGENT: exception saat relaunch {pkg}.", exc_info=True)
                success = False

            if pkg not in SESSIONS:
                break  # sesi sudah dihapus selagi kita relaunch

            if success:
                SESSIONS[pkg]["pid"] = get_pid_quick(pkg) or "-"
                SESSIONS[pkg]["launch_count"] += 1
            else:
                SESSIONS[pkg]["pid"] = "-"
                log.error(f"SESSION_AGENT: relaunch {pkg} gagal, akan dicoba lagi siklus berikutnya.")
    except asyncio.CancelledError:
        raise
    finally:
        log.info(f"SESSION_AGENT: watchdog headless berhenti untuk {pkg}.")
        _watchdog_tasks.pop(pkg, None)


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

  
