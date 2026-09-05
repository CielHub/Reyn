"""
Modul: launcher.py
Tanggung Jawab: Membuka package Roblox dan menjalankan fungsi Smart Wait dengan aman.
"""
import re
import math
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


# =============================================================================
# AUTO FREEFORM/FLOATING WINDOW -- KHUSUS ANDROID 12 (SDK 31/32)
# =============================================================================
# Semua state di bawah ini di-cache di LEVEL MODUL (bukan di-reset tiap
# panggilan launch_and_wait) supaya deteksi versi Android, aktivasi setting
# freeform, dan ukuran layar CUMA dibaca SEKALI per proses/device -- bukan
# tiap kali sebuah package di-launch. Karena tiap device menjalankan proses
# CARRERA-HUB-nya sendiri (root-relaunch di main.py), "sekali per device"
# di sini = sekali per lifetime proses ini berjalan di device tsb.
#
# Device selain Android 12 TIDAK PERNAH kena command freeform sama sekali:
# is_android12() jadi satu-satunya syarat gerbang (if) sebelum command
# windowingMode dipanggil di launch_and_wait(). Kalau False, am start jalan
# PERSIS seperti sebelum fitur ini ada (tidak ada argumen tambahan apa pun).
_ANDROID_SDK_CACHE = None          # int SDK level, di-cache sekali
_FREEFORM_SETTINGS_APPLIED = None  # None=belum dicoba, True/False=sudah
_FREEFORM_SCREEN_SIZE = None       # (width, height) hasil 'wm size', di-cache
_FREEFORM_SLOT_MAP = {}            # pkg_name -> slot grid (0..4), konsisten selama proses hidup
_FREEFORM_MAX_SLOTS = 5
_FREEFORM_RETRY_ATTEMPTS = 3       # sesuai keputusan: coba paksa dulu beberapa kali sebelum fallback normal


def _run_shell(args, use_su=False, timeout=10):
    """Jalankan satu command shell, opsional lewat `su -c "..."` (device sudah root)."""
    try:
        if use_su:
            joined = " ".join(args)
            return subprocess.run(['su', '-c', joined], capture_output=True, text=True,
                                   errors='replace', timeout=timeout)
        return subprocess.run(args, capture_output=True, text=True, errors='replace', timeout=timeout)
    except Exception as e:
        class _FailResult:
            returncode = 1
            stdout = ""
            stderr = str(e)
        return _FailResult()


def _shell_with_fallback(args, timeout=10):
    """
    Coba shell plain dulu; kalau gagal/permission denied, retry lewat
    `su -c` (device sudah di-root, jadi semua command boleh lewat sini
    kalau plain command kena permission denied -- sesuai arahan tugas).
    """
    result = _run_shell(args, use_su=False, timeout=timeout)
    combined = ((result.stdout or '') + (result.stderr or '')).lower()
    if result.returncode != 0 or 'permission denial' in combined or 'permission denied' in combined:
        result = _run_shell(args, use_su=True, timeout=timeout)
    return result


def detect_android_sdk():
    """
    Baca ro.build.version.sdk SEKALI per proses (di-cache di _ANDROID_SDK_CACHE).
    Android 12 = SDK 31, Android 12L = SDK 32. Kalau baca gagal/tidak
    dikenali, dianggap 0 (aman -- artinya is_android12() otomatis False,
    device TIDAK kena logic freeform sama sekali, tidak ada risiko crash
    gara-gara flag yang tidak relevan).
    """
    global _ANDROID_SDK_CACHE
    if _ANDROID_SDK_CACHE is not None:
        return _ANDROID_SDK_CACHE

    sdk = 0
    release = "?"
    try:
        result = subprocess.run(['getprop', 'ro.build.version.sdk'],
                                 capture_output=True, text=True, errors='replace', timeout=5)
        raw = (result.stdout or '').strip()
        if raw.isdigit():
            sdk = int(raw)
    except Exception as e:
        log.warning(f"DEVICE DETECT: Gagal baca ro.build.version.sdk ({str(e)}) -- anggap non-Android12.")

    try:
        rel_result = subprocess.run(['getprop', 'ro.build.version.release'],
                                     capture_output=True, text=True, errors='replace', timeout=5)
        release = (rel_result.stdout or '').strip() or "?"
    except Exception:
        pass

    _ANDROID_SDK_CACHE = sdk
    log.info(f"DEVICE DETECT: Android release={release} (SDK={sdk}) -- terdeteksi sekali, di-cache untuk proses ini.")
    return _ANDROID_SDK_CACHE


def is_android12():
    """True HANYA untuk Android 12 / 12L (SDK 31 atau 32). Ini gerbang WAJIB sebelum command freeform apa pun."""
    return detect_android_sdk() in (31, 32)


def _ensure_freeform_settings_once():
    """
    Aktifkan enable_freeform_support & force_resizable_activities SEKALI
    per proses, HANYA kalau device Android 12 (sudah dijamin oleh caller).
    Aman dipanggil berkali-kali -- no-op setelah percobaan pertama.
    """
    global _FREEFORM_SETTINGS_APPLIED
    if _FREEFORM_SETTINGS_APPLIED is not None:
        return _FREEFORM_SETTINGS_APPLIED

    all_ok = True
    for setting_args in (
        ['settings', 'put', 'global', 'enable_freeform_support', '1'],
        ['settings', 'put', 'global', 'force_resizable_activities', '1'],
    ):
        result = _shell_with_fallback(setting_args)
        if result.returncode != 0:
            all_ok = False
            log.warning(f"FREEFORM SETUP: '{' '.join(setting_args)}' gagal (rc={result.returncode}): "
                        f"{(result.stderr or result.stdout or '').strip()}")

    _FREEFORM_SETTINGS_APPLIED = all_ok
    log.info(f"FREEFORM SETUP: enable_freeform_support/force_resizable_activities di-set "
             f"(Android 12 terdeteksi, ok={all_ok}).")
    return all_ok


def _get_screen_size():
    """Baca resolusi layar SEKALI per proses lewat `wm size` (dipakai untuk hitung grid)."""
    global _FREEFORM_SCREEN_SIZE
    if _FREEFORM_SCREEN_SIZE is not None:
        return _FREEFORM_SCREEN_SIZE

    width, height = 1080, 1920  # fallback default kalau parsing gagal
    try:
        result = _shell_with_fallback(['wm', 'size'])
        out = (result.stdout or '') + (result.stderr or '')
        match = re.search(r'(\d+)\s*x\s*(\d+)', out)
        if match:
            width, height = int(match.group(1)), int(match.group(2))
        else:
            log.warning(f"FREEFORM GRID: Tidak bisa parse 'wm size' ({out.strip()!r}), pakai default {width}x{height}.")
    except Exception as e:
        log.warning(f"FREEFORM GRID: Gagal baca 'wm size' ({str(e)}), pakai default {width}x{height}.")

    _FREEFORM_SCREEN_SIZE = (width, height)
    return _FREEFORM_SCREEN_SIZE


def _assign_slot(pkg_name):
    """
    Assign slot grid (0.._FREEFORM_MAX_SLOTS-1) untuk pkg_name. Package yang
    sama selalu dapat slot yang sama selama proses ini hidup (konsisten,
    tidak query ulang), slot baru dikasih urut ke package yang belum pernah
    dilihat, lalu wrap kalau lebih dari _FREEFORM_MAX_SLOTS package aktif.
    """
    if pkg_name in _FREEFORM_SLOT_MAP:
        return _FREEFORM_SLOT_MAP[pkg_name]
    slot = len(_FREEFORM_SLOT_MAP) % _FREEFORM_MAX_SLOTS
    _FREEFORM_SLOT_MAP[pkg_name] = slot
    return slot


def _compute_grid_bounds(slot_index, total_slots=_FREEFORM_MAX_SLOTS):
    """
    Bagi layar jadi grid serata mungkin secara otomatis berdasarkan resolusi
    layar device saat ini (mis. 5 slot -> grid 3 kolom x 2 baris, 1 cell
    sisa dibiarkan kosong). Return (left, top, right, bottom) buat
    `am task resize`.
    """
    width, height = _get_screen_size()
    cols = max(1, math.ceil(math.sqrt(total_slots)))
    rows = max(1, math.ceil(total_slots / cols))
    cell_w = width // cols
    cell_h = height // rows
    row = slot_index // cols
    col = slot_index % cols
    left = col * cell_w
    top = row * cell_h
    right = left + cell_w
    bottom = top + cell_h
    return left, top, right, bottom


def _get_task_id(pkg_name):
    """
    Cari taskId aktif untuk pkg_name lewat `dumpsys activity activities`.

    FIX: regex lama (`taskId=(\\d+)`) SELALU gagal match di device manapun --
    output asli dumpsys tidak pernah literally menulis string "taskId=".
    Format yang benar-benar dipakai (stabil dari Android lama sampai baru)
    ada di baris ActivityRecord: "<pkg>/<Activity> t<id>}" -- ini yang
    dicari duluan. Fallback ke pola Task{...} lama ("#<id>") kalau baris
    ActivityRecord tidak ketemu (mis. beda locale/format vendor).
    """
    result = _shell_with_fallback(['dumpsys', 'activity', 'activities'])
    out = result.stdout or ''
    for line in out.splitlines():
        if pkg_name not in line:
            continue
        # Format ActivityRecord: "...com.roblox.client/.MainActivity t1234}"
        match = re.search(rf'{re.escape(pkg_name)}/\S*\s+t(\d+)\b', line)
        if match:
            return match.group(1)
    for line in out.splitlines():
        if pkg_name not in line:
            continue
        # Fallback format Task{...}/TaskRecord{...}: "...#1234..."
        match = re.search(r'[Tt]ask(?:Record)?\{[^}]*#(\d+)', line)
        if match:
            return match.group(1)
    return None


def _debug_dump_task_state(pkg_name, task_id):
    """
    Diagnostik: setelah launch+resize freeform, log baris mentah dumpsys
    yang menyebut pkg_name/task_id ini -- supaya kalau window masih
    fullscreen padahal am start 'sukses', ada data konkret (bukan tebakan)
    buat cek apakah windowingMode beneran diterapkan OS atau cuma
    di-ignore diam-diam (rc=0 dari am start TIDAK menjamin freeform benar2
    aktif -- banyak ROM/build silently ignore flag ini di layar utama HP
    biasa, beda dari tablet/Chromebook/DeX).
    """
    try:
        result = _shell_with_fallback(['dumpsys', 'activity', 'activities'])
        out = result.stdout or ''
        relevant = [ln.strip() for ln in out.splitlines()
                    if pkg_name in ln or (task_id and f'#{task_id}' in ln) or (task_id and f't{task_id}' in ln)]
        if relevant:
            log.info(f"FREEFORM DEBUG [{pkg_name}]: " + " | ".join(relevant[:6]))
        else:
            log.info(f"FREEFORM DEBUG [{pkg_name}]: tidak ada baris relevan ditemukan di dumpsys.")
    except Exception as e:
        log.warning(f"FREEFORM DEBUG: gagal dump state ({str(e)}).")


def _apply_freeform_grid(pkg_name):
    """
    Resize/reposisi window pkg_name sesuai slot grid otomatisnya lewat
    `am task resize`. Dipanggil HANYA setelah am start dengan windowingMode
    freeform sukses (rc=0). CATATAN: rc=0 dari am start TIDAK MENJAMIN
    windowingMode benar-benar diterapkan OS -- lihat _debug_dump_task_state().
    Kegagalan resize di sini cuma di-log, tidak pernah bikin launch_and_wait
    gagal gara-gara ini.
    """
    try:
        slot = _assign_slot(pkg_name)
        bounds = _compute_grid_bounds(slot)
        time.sleep(0.5)  # beri jeda kecil supaya taskId sempat terdaftar di activity stack
        task_id = _get_task_id(pkg_name)
        if not task_id:
            log.warning(f"FREEFORM RESIZE: taskId untuk {pkg_name} tidak ditemukan, skip resize "
                        f"(window tetap freeform ukuran default).")
            _debug_dump_task_state(pkg_name, None)
            return
        left, top, right, bottom = bounds
        result = _shell_with_fallback(['am', 'task', 'resize', task_id,
                                        str(left), str(top), str(right), str(bottom)])
        if result.returncode != 0:
            log.warning(f"FREEFORM RESIZE: gagal resize taskId={task_id} {pkg_name} -> {bounds}: "
                        f"{(result.stderr or result.stdout or '').strip()}")
        else:
            log.info(f"FREEFORM RESIZE: {pkg_name} (slot {slot}) taskId={task_id} -> {bounds}")
        _debug_dump_task_state(pkg_name, task_id)
    except Exception as e:
        log.warning(f"FREEFORM RESIZE: error tak terduga untuk {pkg_name} ({str(e)}), lanjut tanpa resize.")

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

    # --activity-single-top: WAJIB supaya intent ini TETAP dikirim ke
    # activity yang sudah berjalan lewat onNewIntent() walau activity
    # tsb kebetulan sudah di posisi paling atas/foreground (skenario
    # persis "trigger balik ke Lobby" pada package yang masih hidup di
    # tengah game -- tanpa flag ini, Android hanya membalas "brought to
    # the front" TANPA benar-benar mengirim data intent-nya ke app,
    # sehingga Roblox tidak pernah tahu ada perintah roblox:// baru dan
    # tetap diam di layar game lama walau proses/foreground check kita
    # tetap lolos/false-positive).
    base_am_args = ['am', 'start', '--activity-single-top', '-p', pkg_name,
                     '-a', 'android.intent.action.VIEW', '-d', intent_url]

    # === AUTO FREEFORM/FLOATING WINDOW -- KHUSUS ANDROID 12 (SDK 31/32) ===
    # is_android12() adalah SATU-SATUNYA gerbang: device selain Android 12
    # jatuh langsung ke else di bawah dan menjalankan am start PERSIS seperti
    # sebelum fitur ini ada -- tidak ada argumen tambahan, tidak ada risiko
    # error/crash gara-gara flag windowingMode yang tidak relevan di versi
    # itu. Deteksi SDK & aktivasi setting freeform sendiri sudah di-cache
    # sekali per proses (lihat detect_android_sdk()/_ensure_freeform_settings_once()),
    # jadi baris di bawah ini TIDAK melakukan query ulang tiap launch.
    use_freeform = is_android12()
    freeform_applied = False
    launch_result = None

    if use_freeform:
        _ensure_freeform_settings_once()
        freeform_am_args = ['am', 'start', '--windowingMode', '5', '--activity-single-top',
                             '-p', pkg_name, '-a', 'android.intent.action.VIEW', '-d', intent_url]

        for attempt in range(1, _FREEFORM_RETRY_ATTEMPTS + 1):
            candidate = _shell_with_fallback(freeform_am_args)
            candidate_out = ((candidate.stdout or '') + (candidate.stderr or '')).lower()
            if candidate.returncode == 0 and 'error' not in candidate_out:
                launch_result = candidate
                freeform_applied = True
                break
            log.warning(f"FREEFORM LAUNCH: percobaan {attempt}/{_FREEFORM_RETRY_ATTEMPTS} gagal untuk "
                        f"{pkg_name} (windowingMode 5 mungkin tidak didukung ROM/vendor ini): "
                        f"{(candidate.stderr or candidate.stdout or '').strip()}")
            time.sleep(1)

        if not freeform_applied:
            log.warning(f"FREEFORM LAUNCH: {pkg_name} fallback ke launch NORMAL (tanpa windowingMode) "
                        f"setelah {_FREEFORM_RETRY_ATTEMPTS}x percobaan freeform gagal.")

    if launch_result is None:
        launch_result = subprocess.run(base_am_args, capture_output=True, text=True, errors='replace')

    launch_output = ((launch_result.stdout or '') + '\n' + (launch_result.stderr or '')).strip()
    if launch_output:
        log.info(f"LAUNCH RESULT [{pkg_name}]: {launch_output.replace(chr(10), ' | ')}")
    if launch_result.returncode != 0:
        log.error(f"LAUNCH COMMAND FAILED [{pkg_name}]: rc={launch_result.returncode}")
        return ("FAILED", "LAUNCH_COMMAND_FAILED") if require_join_signal else False

    if freeform_applied:
        _apply_freeform_grid(pkg_name)

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
    # Improve poin 3 (percepat deteksi join): keyword logcat di atas hampir
    # tidak pernah muncul di client modifikasi (mis. Delta Lite), jadi
    # loop ini SELALU habis nunggu sampai timeout_seconds sebelum sadar
    # prosesnya sudah mati. Sekarang dicek tiap PID_CHECK_INTERVAL_SECONDS
    # supaya proses yang KETAHUAN mati bisa langsung dilaporkan gagal tanpa
    # nunggu sisa timeout habis dulu -- tidak mengubah cara SUCCESS/UNCERTAIN
    # diputuskan sama sekali, cuma mempercepat jalur proses mati.
    PID_CHECK_INTERVAL_SECONDS = 3
    last_pid_check = start_time

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
            elif time.time() - last_pid_check >= PID_CHECK_INTERVAL_SECONDS:
                last_pid_check = time.time()
                if not get_pid_quick(pkg_name):
                    log.warning(f"[FAST-FAIL] {pkg_name}: PID hilang sebelum timeout habis "
                                f"(elapsed={elapsed:.1f}s) -- berhenti nunggu lebih awal.")
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

    
