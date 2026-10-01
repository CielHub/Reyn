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
    # timeout ditambahkan: `pidof` yang menggantung tidak boleh membekukan
    # caller (watchdog/recovery/sync). Perilaku lain TIDAK berubah.
    try:
        result = subprocess.run(
            ['pidof', pkg_name], capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _validate_pid(pid, pkg_name):
    """Validasi bahwa PID hasil `pidof` memang proses utama package ini dan
    belum zombie. Return True/False; None kalau /proc tidak bisa dibaca
    (jangan menolak PID hanya karena tidak bisa diverifikasi)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().split(b"\x00", 1)[0].decode("utf-8", "ignore").strip()
    except FileNotFoundError:
        return False  # proses sudah hilang di antara pidof dan cek ini
    except Exception:
        return None
    if cmd and cmd != pkg_name:
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read().decode("utf-8", "ignore")
        state = stat.rsplit(")", 1)[1].split()[0]
        if state in ("Z", "X"):
            return False
    except FileNotFoundError:
        return False
    except Exception:
        pass
    return True


def get_ram_info():
    """RAM KESELURUHAN device dari /proc/meminfo (bukan per package).

    Return dict {total_mb, used_mb, available_mb, percent, ts} atau None kalau
    tidak bisa dibaca. used = MemTotal - MemAvailable (cara yang sama dengan
    `free`/Android, jadi cache yang bisa dilepas tidak dihitung terpakai).
    Baca file kecil di /proc -> murni I/O ringan, tanpa subprocess."""
    try:
        values = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached"):
                    parts = rest.split()
                    if parts and parts[0].isdigit():
                        values[key] = int(parts[0])  # kB
        total = values.get("MemTotal")
        if not total:
            return None
        available = values.get("MemAvailable")
        if available is None:  # kernel lama tanpa MemAvailable
            available = values.get("MemFree", 0) + values.get("Buffers", 0) + values.get("Cached", 0)
        available = max(0, min(available, total))
        used = total - available
        return {
            "total_mb": int(round(total / 1024)),
            "used_mb": int(round(used / 1024)),
            "available_mb": int(round(available / 1024)),
            "percent": int(round(used * 100 / total)),
            "ts": time.time(),
        }
    except Exception:
        return None


def get_pids(pkg_name):
    """Semua PID hidup milik SATU package (set of str), tervalidasi.

    Return:
      set() kosong  -> BENAR-BENAR tidak ada proses (pidof exit 1 / kosong).
      None          -> pengecekan GAGAL/ambigu (timeout, pidof tidak ada,
                       error lain). Caller WAJIB menganggap ini 'tidak tahu',
                       BUKAN 'mati', supaya error sesaat tidak memicu
                       recovery palsu (yang akan kill proses yang hidup).
    """
    try:
        result = subprocess.run(
            ['pidof', pkg_name], capture_output=True, text=True,
            timeout=5, errors='replace',
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    except Exception:
        return None
    raw = (result.stdout or "").strip().split()
    candidates = [p for p in raw if p.isdigit()]
    if not candidates:
        # pidof exit code 1 = tidak ada proses. Kode lain = error pengecekan.
        return set() if result.returncode in (0, 1) else None
    alive = set()
    for pid in candidates:
        if _validate_pid(pid, pkg_name) is not False:
            alive.add(pid)
    return alive

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

