"""
Modul: launcher.py
Tanggung Jawab: Membuka package Roblox dan menjalankan fungsi Smart Wait dengan aman.
"""
import re
import subprocess
import time
import datetime
import select
from core.logger import log
from core.join_verifier import verify_join

# Sinkron dengan join_verifier._FLOG_NETWORK_PATTERN / _DISCONNECT_REASON_PATTERN
# (dan core/error_detector.py) -- dipakai di sini HANYA untuk fast-fail dini
# (berhenti nunggu lebih cepat kalau sudah ada bukti kick asli), bukan sebagai
# signal sukses.
_FLOG_NETWORK_PATTERN = re.compile(r"\[flog::network\]")
_DISCONNECT_REASON_PATTERN = re.compile(r"reason\s*:\s*(266|267|277|279|280)")

def get_pid_quick(pkg_name):
    try:
        result = subprocess.run(['pidof', pkg_name], capture_output=True, text=True)
        pids = result.stdout.strip().split()
        return pids[0] if pids else ""
    except Exception:
        return ""

def launch_and_wait(pkg_name, intent_url, timeout_seconds, require_join_signal=False):
    """
    FIX (Lobby-trigger tidak sampai ke app): `am start` di bawah SELALU
    memakai `--activity-single-top`. Tanpa flag ini, kalau activity package
    kebetulan SUDAH di posisi paling atas (mis. package masih di tengah
    game saat di-trigger balik ke Lobby), Android hanya membalas "brought
    to the front" TANPA pernah mengirim intent-nya ke app (onNewIntent()
    tidak terpanggil) -- akibatnya Roblox tidak pernah tahu ada perintah
    `roblox://` baru dan tetap diam di layar lama, padahal verify_join()
    (cuma cek proses hidup + foreground) tetap melaporkan sukses (false
    positive). Dengan flag ini, intent TETAP dikirim lewat onNewIntent()
    walau activity sudah di atas, sekaligus tidak mengubah perilaku kalau
    activity BELUM di atas (start normal seperti biasa).

    require_join_signal (LIFECYCLE REVISION, default False -- PERILAKU LAMA
    TIDAK BERUBAH untuk semua pemanggil existing yang tidak mengisi argumen
    ini: core/menu.py, core/tester.py, core/recovery_manager.py, dan
    relaunch di core/session_agent.py._headless_watchdog_loop):

    Sebelumnya, kalau logcat 'Smart Wait' timeout tanpa menemukan keyword
    join (found_success tetap False), fungsi ini DIAM-DIAM tetap lanjut ke
    verify_join() dan return True selama proses masih hidup (walau Roblox
    baru nyangkut di layar login/menu, BUKAN benar-benar di dalam game).
    Itu bug lama yang membuat variabel found_success tidak pernah dipakai.

    Saat require_join_signal=True (dipakai session_agent.py KHUSUS untuk
    langkah join ke target game, setelah akun terkonfirmasi benar -- lihat
    dokumen revisi bag. 24 'Syarat Valid untuk RUNNING'):

    LIFECYCLE REVISION (Masalah #1, lihat CARRERA_HUB_IMPLEMENTATION_PROMPT_V2):
    fungsi ini TIDAK LAGI mengembalikan bool polos untuk require_join_signal=True
    -- return-nya jadi tuple (status, reason) dengan status salah satu dari:

    - "SUCCESS"   : keyword join ATAU (tidak ada keyword tapi juga tidak ada
                    bukti kick/error asli DAN proses hidup) -- lihat catatan
                    di bawah soal kapan SUCCESS dipakai vs UNCERTAIN.
    - "FAILED"    : ada bukti KUAT kegagalan (proses mati prematur, ATAU
                    logcat menunjukkan disconnect/kick asli lewat
                    [FLog::Network] reason code -- lihat join_verifier.
                    has_recent_disconnect_signal()).
    - "UNCERTAIN" : proses masih hidup, TIDAK ada bukti sukses (keyword)
                    MAUPUN bukti gagal (disconnect/kick) sampai timeout --
                    ini persis kasus nyata yang dilaporkan (Roblox sudah
                    masuk game tapi keyword logcat lama tidak pernah
                    ketemu, kemungkinan client modifikasi/Delta Lite tidak
                    menulis string tersebut). Caller (session_agent.py)
                    WAJIB melakukan verifikasi tambahan (grace-check) untuk
                    status ini -- BUKAN langsung dianggap sukses ataupun
                    gagal di sini, supaya tidak ada "fake success" maupun
                    false START_FAILED untuk kasus yang sebenarnya berhasil.

    require_join_signal=False (dipakai menu.py/tester.py/recovery_manager.py/
    lobby stage session_agent.py): PERILAKU LAMA TIDAK BERUBAH SAMA SEKALI --
    tetap return bool, keyword logcat tetap diabaikan seperti sebelumnya
    (cukup proses hidup + verify_join()).
    """
    if not intent_url:
        log.error(f"LAUNCH FAILED: {pkg_name} tidak memiliki Intent URL.")
        return ("FAILED", "NO_INTENT_URL") if require_join_signal else False

    log.info(f"LAUNCH: Membuka {pkg_name}...")
    
    start_time_str = datetime.datetime.now().strftime('%m-%d %H:%M:%S.000')
    
    launch_result = subprocess.run(
        # --activity-single-top: WAJIB supaya intent ini TETAP dikirim ke
        # activity yang sudah berjalan lewat onNewIntent() walau activity
        # tsb kebetulan sudah di posisi paling atas/foreground (skenario
        # persis "trigger balik ke Lobby" pada package yang masih hidup di
        # tengah game -- tanpa flag ini, Android hanya membalas "brought to
        # the front" TANPA benar-benar mengirim data intent-nya ke app,
        # sehingga Roblox tidak pernah tahu ada perintah roblox:// baru dan
        # tetap diam di layar game lama walau proses/foreground check kita
        # tetap lolos/false-positive).
        ['am', 'start', '--activity-single-top', '-p', pkg_name,
         '-a', 'android.intent.action.VIEW', '-d', intent_url],
        capture_output=True,
        text=True,
        errors='replace',
    )
    launch_output = ((launch_result.stdout or '') + '\n' + (launch_result.stderr or '')).strip()
    if launch_output:
        log.info(f"LAUNCH RESULT [{pkg_name}]: {launch_output.replace(chr(10), ' | ')}")
    if launch_result.returncode != 0:
        log.error(f"LAUNCH COMMAND FAILED [{pkg_name}]: rc={launch_result.returncode}")
        return ("FAILED", "LAUNCH_COMMAND_FAILED") if require_join_signal else False
    
    log.info(f"Smart Wait: Menunggu {pkg_name} terhubung ({timeout_seconds} detik)...")
    
    logcat_cmd = ['logcat', '-T', start_time_str, '-v', 'time']
    
    # --- BUG FIX IMPLEMENTATION ---
    process = subprocess.Popen(
        logcat_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding='utf-8',   # Pastikan decoding standar
        errors='replace',   # FIX: Abaikan/ganti byte cacat agar program tidak crash
        bufsize=1 
    )
    # ------------------------------
    
    keywords = ["gamejoinutil", "datamodel initialized", "successfully connected"]
    found_success = False
    found_failure = False
    failure_code = None
    start_time = time.time()
    
    try:
        while True:
            elapsed = time.time() - start_time
            if elapsed >= timeout_seconds:
                log.warning(f"FALLBACK: Logcat timeout. Menggunakan Dumb Wait untuk {pkg_name}.")
                break
                
            ready, _, _ = select.select([process.stdout], [], [], 1.0)
            
            if ready:
                line = process.stdout.readline()
                if not line:
                    break 
                
                line_lower = line.lower()
                if any(kw in line_lower for kw in keywords):
                    found_success = True
                    break

                # Fast-fail: kalau require_join_signal, hentikan lebih awal
                # begitu ada bukti KUAT disconnect/kick asli, jangan tunggu
                # sampai timeout habis (regex sinkron dengan join_verifier.py
                # / error_detector.py, sudah terbukti reliable di project ini).
                if require_join_signal and _FLOG_NETWORK_PATTERN.search(line_lower):
                    match = _DISCONNECT_REASON_PATTERN.search(line_lower)
                    if match:
                        found_failure = True
                        failure_code = match.group(1)
                        break
    finally:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill() 
        
    final_pid = get_pid_quick(pkg_name)
    if not final_pid:
        log.error(f"LAUNCH FAILED: {pkg_name} gagal diluncurkan (Proses mati secara prematur).")
        return ("FAILED", "PROCESS_NOT_RUNNING") if require_join_signal else False

    if not require_join_signal:
        # PERILAKU LAMA TIDAK BERUBAH: proses hidup + verify_join() saja.
        verified, reason = verify_join(pkg_name)
        if not verified:
            log.error(f"VERIFY FAILED: {pkg_name} -> {reason}")
            return False
        log.info(f"VERIFY: {pkg_name} -> {reason}")
        log.info(f"SUCCESS: {pkg_name} selesai diproses.")
        return True

    # --- Dari sini require_join_signal=True: return tuple (status, reason) ---

    if found_failure:
        log.warning(f"[VERIFY] {pkg_name}: bukti disconnect/kick asli terdeteksi "
                    f"(reason={failure_code}) sebelum/selama menunggu sinyal join -- FAILED.")
        return ("FAILED", f"JOIN_ERROR_SIGNAL_{failure_code}")

    if found_success:
        verified, reason = verify_join(pkg_name)
        if not verified:
            log.error(f"VERIFY FAILED: {pkg_name} -> {reason} (padahal keyword join ditemukan).")
            return ("FAILED", f"VERIFY_FAILED_AFTER_KEYWORD:{reason}")
        log.info(f"[VERIFY] {pkg_name}: keyword join ditemukan + {reason} -> SUCCESS.")
        return ("SUCCESS", "JOIN_KEYWORD_FOUND")

    # LIFECYCLE REVISION (Masalah #1): tidak ada keyword sukses MAUPUN bukti
    # kegagalan sampai timeout habis, tapi proses masih hidup. Ini kasus
    # nyata yang dilaporkan (Roblox sudah masuk game tapi keyword logcat
    # lama tidak pernah ketemu, kemungkinan besar client modifikasi/Delta
    # Lite tidak menulis string tersebut ke logcat). JANGAN langsung FAILED
    # (fake failure) -- serahkan ke caller untuk grace-check tambahan,
    # JUGA jangan langsung SUCCESS di sini (fake success tanpa bukti).
    log.warning(f"[VERIFY] {pkg_name}: proses hidup (pid={final_pid}), tidak ada keyword join "
                f"MAUPUN bukti kegagalan dalam {timeout_seconds}s -- UNCERTAIN, perlu grace-check.")
    return ("UNCERTAIN", "NO_JOIN_SIGNAL_TIMEOUT")

    
