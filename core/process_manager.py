"""
Modul : process_manager.py
Tanggung Jawab:
- Manajemen proses secara low-level.
- Pencarian PID suatu package.
- Menangani terminasi/kill (Graceful Terminate).
"""

import subprocess
import time

def get_pid(pkg_name):
    try:
        result = subprocess.run(['pidof', pkg_name], capture_output=True, text=True)
        return result.stdout.strip()
    except FileNotFoundError:
        return ""

def pid_exists(pid):
    result = subprocess.run(
        ["su", "-c", f"kill -0 {pid}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0

def _proc_dir_exists(pid):
    """Cek independen KEDUA apakah proses masih hidup, lewat keberadaan
    /proc/<pid> (bukan lewat signal kill -0 seperti pid_exists()) -- dipakai
    graceful_kill() sebagai pengaman ganda SEBELUM eskalasi ke `am
    force-stop`, supaya tidak salah eskalasi hanya karena satu metode cek
    saja kebetulan lambat/gagal baca."""
    result = subprocess.run(
        ["su", "-c", f"test -d /proc/{pid}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0

def wait_until_process_dead(pid, timeout=5.0):
    deadline = time.time() + timeout

    while time.time() < deadline:
        if not pid_exists(pid):
            return True
        time.sleep(0.2)

    return False

def graceful_kill(pid, package=None):
    """Kill SATU proses (pid) secara bertahap: SIGTERM -> SIGKILL -> (last
    resort) `am force-stop` kalau proses masih bertahan.

    LIFECYCLE REVISION (Masalah #2, lihat CARRERA_HUB_IMPLEMENTATION_PROMPT_V2):
    audit isolasi package/session (session_agent.handle_stop_session ->
    sini) sudah dikonfirmasi BENAR -- fungsi ini SELALU dipanggil dengan PID
    spesifik hasil resolve TERBARU untuk SATU package, tidak pernah pid/nama
    package gabungan. `kill -15`/`kill -9` di atas PID tunggal TIDAK bisa
    menyentuh proses package lain.

    Satu-satunya langkah di sini yang punya jangkauan LEBIH LUAS dari sekadar
    "matikan proses ini" adalah `am force-stop {package}` (STEP 3) -- ini
    operasi level Activity Manager Android (bukan sekadar kill), yang juga
    membersihkan task stack/service/alarm milik package tsb dan memicu
    broadcast sistem. Di lingkungan floating-window/cloud-phone, broadcast
    inilah yang paling mungkin membuat window package LAIN (yang sama sekali
    tidak disentuh proses/PID-nya) ikut ter-refresh/minimize oleh host
    window manager -- ini limitasi HOST, bukan cross-package kill di kode
    ini (lihat catatan [WINDOW] di session_agent.py).

    Karena itu, force-stop SENGAJA dibuat seketat mungkin sebagai upaya
    terakhir: re-verifikasi proses masih hidup lewat DUA cara independen
    (kill -0 DAN pidof) plus jeda tambahan, supaya tidak eskalasi ke
    force-stop kalau SIGKILL sebenarnya sudah berhasil tapi belum
    kebaca cepat oleh satu metode cek saja.

    Return: (killed: bool, used_force_stop: bool) -- used_force_stop dipakai
    caller untuk log [WINDOW] kalau limitasi host di atas kemungkinan
    kena trigger.
    """
    if not pid:
        return False, False

    # ====================================================
    # STEP 1: SIGTERM
    # ====================================================
    subprocess.run(
        ["su", "-c", f"kill -15 {pid}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for _ in range(35):      # Tunggu sampai maksimal 7 detik
        if not pid_exists(pid):
            return True, False
        time.sleep(0.2)

    # ====================================================
    # STEP 2: SIGKILL
    # ====================================================
    subprocess.run(
        ["su", "-c", f"kill -9 {pid}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for _ in range(10):
        if not pid_exists(pid):
            return True, False
        time.sleep(0.2)

    # Cek independen kedua (/proc/<pid>, bukan cuma kill -0) + jeda kecil
    # sebelum eskalasi -- mengurangi false-trigger ke force-stop kalau
    # SIGKILL sebenarnya sudah berhasil tapi belum kebaca cepat oleh satu
    # metode saja.
    time.sleep(0.5)
    if not pid_exists(pid) and not _proc_dir_exists(pid):
        return True, False

    # ====================================================
    # STEP 3: Fallback (am force-stop) -- LAST RESORT, lihat catatan di
    # docstring soal potensi efek samping window package lain di host.
    # ====================================================
    if package:
        subprocess.run(
            ["su", "-c", f"am force-stop {package}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
        return (not pid_exists(pid)), True

    return not pid_exists(pid), False

def kill_pid_direct(pid, verify_timeout=3.0):
    """[FINISH LOGIC BARU] Kill SATU PID target pakai `kill` biasa (BUKAN
    `am force-stop`) -- dipakai session_agent.handle_stop_session sebagai
    pengganti penuh alur lama yang tidak pernah kill sama sekali.

    Kenapa bukan `am force-stop`: command itu operasi level Activity
    Manager yang broadcast lebih luas dan (di host floating-window/
    cloud-phone) bisa memicu efek visual ke package LAIN yang sama sekali
    tidak disentuh proses/PID-nya. `kill <pid>` di sini HANYA menyentuh
    proses tunggal pemilik PID tsb -- efek "package lain jadi
    bubble/minimized" yang tetap mungkin terjadi adalah efek window
    manager host terhadap floating window yang kosong, bukan hasil dari
    proses/PID package lain ikut mati (lihat _finish_kill_and_restore_survivors
    di session_agent.py untuk langkah pemulihannya).

    Default TIDAK langsung "kill -9": kirim SIGTERM (`kill` polos) dulu,
    baru eskalasi ke SIGKILL kalau target ternyata masih hidup setelah
    verify_timeout -- sesuai instruksi supaya "kill -9" hanya jadi
    fallback, bukan default.

    Return True kalau PID target sudah dipastikan mati (lewat pid_exists()),
    False kalau masih hidup setelah kedua percobaan.
    """
    if not pid:
        return False

    subprocess.run(
        ["su", "-c", f"kill {pid}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if wait_until_process_dead(pid, timeout=verify_timeout):
        return True

    # Fallback: target masih hidup setelah `kill` polos -- eskalasi ke SIGKILL.
    subprocess.run(
        ["su", "-c", f"kill -9 {pid}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return wait_until_process_dead(pid, timeout=2.0)


def restore_foreground(package):
    """[FINISH LOGIC BARU] Bawa SATU package floating-window yang masih
    hidup (survivor) kembali ke foreground pakai `monkey -p <package> 1`.

    PENTING (hasil test manual, lihat instruksi FINISH LOGIC BARU): satu
    command ini HANYA membawa `package` tsb ke foreground -- TIDAK ikut
    membawa package lain, dan TIDAK membuat instance/session baru (proses
    survivor yang sama tetap dipakai, bukan rejoin/restart). Caller WAJIB
    memanggil fungsi ini SATU PER SATU untuk tiap surviving package kalau
    ada lebih dari satu -- jangan berasumsi satu panggilan cukup untuk
    semua survivor.

    Return True kalau command monkey sukses dijalankan (returncode 0).
    Ini bukan jaminan window benar-benar sudah tidak bubble secara visual,
    hanya konfirmasi command-nya berhasil dieksekusi.
    """
    if not package:
        return False

    result = subprocess.run(
        ["su", "-c", f"monkey -p {package} 1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def clear_package_data(package):
    """[RESET] `pm clear` SATU package spesifik lewat PackageManager.

    CATATAN (LOGIC BARU): alur reset pasca-Finish (lihat
    session_agent._restart_finished_package) SUDAH TIDAK memanggil fungsi
    ini lagi -- package pasca-Finish sekarang sengaja di-restart TANPA
    clear-data (data/login/progress package tetap utuh). Fungsi ini
    dibiarkan ada (tidak dipakai caller manapun saat ini) untuk kebutuhan
    lain di masa depan yang memang butuh clear-data eksplisit -- HANYA
    SETELAH proses package tsb dipastikan mati lewat graceful_kill() +
    verifikasi tambahan di pemanggil (jangan pernah clear-data proses yang
    masih hidup -- state package saat itu tidak terdefinisi).

    Scoped ke SATU package by design: `pm clear <package>` Android hanya
    menerima satu nama package persis, tidak ada bentuk wildcard/global di
    command ini -- tidak ada jalur di fungsi ini yang bisa menyentuh
    package lain, sama seperti graceful_kill() di atas.

    Return True hanya kalau PackageManager benar-benar melaporkan sukses
    (returncode 0 DAN output mengandung 'Success') -- returncode 0 saja
    tidak selalu berarti berhasil untuk command `pm` di sebagian device.
    """
    if not package:
        return False

    result = subprocess.run(
        ["su", "-c", f"pm clear {package}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    output = (result.stdout or "").strip().lower()
    return result.returncode == 0 and "success" in output


def hard_force_stop(package):
    """Hard-stop a package through Android's package manager.

    Used by the highest recovery tier only, after lighter relaunch/cache
    attempts have failed. Returns True when the command succeeds.
    """
    if not package:
        return False

    result = subprocess.run(
        ["su", "-c", f"am force-stop {package}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0

