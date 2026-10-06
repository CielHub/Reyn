"""
Modul : process_manager.py
Tanggung Jawab:
- Manajemen proses secara low-level.
- Pencarian PID suatu package.
- Menangani terminasi/kill (Graceful Terminate).
"""

import re
import shlex
import subprocess
import time

_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_.]+$")

def is_valid_package_name(pkg_name) -> bool:
    return bool(_PACKAGE_RE.fullmatch(str(pkg_name or "").strip()))


def get_pid(pkg_name):
    pkg_name = str(pkg_name or "").strip()
    if not is_valid_package_name(pkg_name):
        return ""
    try:
        result = subprocess.run(
            ['pidof', pkg_name], capture_output=True, text=True, timeout=5
        )
        for pid in (result.stdout or "").split():
            if _validate_pid(pid, pkg_name):
                return pid
        return ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def get_process_identity(pid):
    """Return process identity for one PID.

    Identity is stronger than PID existence:
      - package is read from /proc/<pid>/cmdline and must exactly match when
        package ownership is required.
      - start_time is field 22 from /proc/<pid>/stat (kernel clock ticks).
        This prevents PID-reuse races.
    """
    pid = str(pid).strip()
    if not pid.isdigit():
        return None

    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            package = f.read().split(b"\x00", 1)[0].decode(
                "utf-8", "ignore"
            ).strip()

        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read().decode("utf-8", "ignore")

        _, rest = stat.rsplit(")", 1)
        fields = rest.split()
        if len(fields) < 20:
            return None

        state = fields[0]
        if state in ("Z", "X"):
            return None

        start_time = int(fields[19])
        return {
            "pid": pid,
            "package": package,
            "start_time": start_time,
        }
    except FileNotFoundError:
        return None
    except (ValueError, IndexError):
        return None
    except Exception:
        return None


def is_pid_owned_by_package(pid, pkg_name, expected_start_time=None):
    """Return True only when PID ownership is proven exactly."""
    pkg_name = str(pkg_name or "").strip()
    if not pkg_name:
        return False

    identity = get_process_identity(pid)
    if identity is None or identity.get("package") != pkg_name:
        return False

    if expected_start_time is not None:
        try:
            return int(identity["start_time"]) == int(expected_start_time)
        except (TypeError, ValueError, KeyError):
            return False

    return True


def _validate_pid(pid, pkg_name):
    """Validate a pidof result against exact package ownership and state."""
    identity = get_process_identity(pid)
    if identity is None:
        return False

    package = identity.get("package") or ""
    return bool(pkg_name and package and package == pkg_name)



def get_oom_adj(pid):
    """oom_score_adj proses (int) atau None kalau tidak terbaca.

    Ini tingkat 'seberapa aktif' proses menurut Android:
      0   = foreground, 100 = visible (jendela terlihat), 200 = perceptible,
      700 = previous app, 900+ = CACHED (tidak punya jendela/aktivitas aktif).
    Roblox yang jendelanya ditutup (X di freeform) / di-close dari layar sering
    TIDAK langsung mati: prosesnya tetap ada (PID masih ketemu `pidof`) tapi
    jatuh ke 900+. PID saja tidak cukup untuk membuktikan Roblox benar-benar
    sedang jalan -- adj inilah yang membedakannya."""
    try:
        with open(f"/proc/{pid}/oom_score_adj", "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def get_min_oom_adj(pids):
    """adj TERENDAH (= paling aktif) dari sekumpulan PID satu package. Package
    dengan beberapa proses dianggap aktif kalau SALAH SATU proses aktif."""
    values = [v for v in (get_oom_adj(p) for p in (pids or ())) if v is not None]
    return min(values) if values else None


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
        pkg_name = str(pkg_name or "").strip()
        if not is_valid_package_name(pkg_name):
            return None
        result = subprocess.run(
            ['pidof', pkg_name], capture_output=True, text=True,
            timeout=5, errors='replace',
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    except Exception:
        return None
    pkg_name = str(pkg_name or "").strip()
    if not is_valid_package_name(pkg_name):
        return None
    raw = (result.stdout or "").strip().split()
    candidates = [p for p in raw if p.isdigit()]
    if not candidates:
        # pidof exit code 1 = tidak ada proses. Kode lain = error pengecekan.
        return set() if result.returncode in (0, 1) else None
    alive = set()
    for pid in candidates:
        if not _validate_pid(pid, pkg_name):
            return None
        alive.add(pid)
    return alive

def pid_exists(pid):
    pid = str(pid or "").strip()
    if not pid.isdigit():
        return False
    result = subprocess.run(
        ["su", "-c", f"kill -0 {shlex.quote(pid)}"],
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
    pid = str(pid or "").strip()
    if not pid.isdigit():
        return False
    result = subprocess.run(
        ["su", "-c", f"test -d /proc/{shlex.quote(pid)}"],
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

def graceful_kill(pid, package=None, expected_start_time=None):
    """Terminate exactly one PID with SIGTERM (`kill -15`) only.

    When ``package`` is supplied, ownership and (optionally) kernel
    ``start_time`` are verified immediately before the signal is sent. This
    prevents a reused PID from receiving SIGTERM after its original process
    has already disappeared.

    There is deliberately NO package-wide fallback, SIGKILL fallback, or
    implicit ``am force-stop`` here. A caller that needs an emergency
    package-scoped stop must opt into ``hard_force_stop()`` explicitly.

    Return: ``(killed: bool, used_force_stop: bool)``. The second value is
    retained for compatibility and is always ``False``.
    """
    pid = str(pid or "").strip()
    if not pid.isdigit():
        return False, False

    if package is not None:
        package = str(package or "").strip()
        if not is_valid_package_name(package):
            return False, False
        identity = get_process_identity(pid)
        if identity is None or identity.get("package") != package:
            return False, False
        if expected_start_time is not None:
            try:
                if int(identity.get("start_time")) != int(expected_start_time):
                    return False, False
            except (TypeError, ValueError, KeyError):
                return False, False

    subprocess.run(
        ["su", "-c", f"kill -15 {pid}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if package is not None:
        killed = _wait_until_pid_not_owned(
            pid,
            package,
            expected_start_time if expected_start_time is not None else identity.get("start_time"),
            timeout=7.0,
        )
    else:
        killed = wait_until_process_dead(pid, timeout=7.0)

    return killed, False

def _wait_until_pid_not_owned(pid, package, expected_start_time, timeout=5.0):
    """Wait until the exact original PID identity is gone or replaced."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        identity = get_process_identity(pid)
        if identity is None:
            if not pid_exists(pid):
                return True
        else:
            if (
                identity.get("package") != package
                or int(identity.get("start_time", -1)) != int(expected_start_time)
            ):
                return True
        time.sleep(0.2)
    return False


def kill_pid_direct(
    pid,
    verify_timeout=7.0,
    package=None,
    expected_start_time=None,
):
    """Kill one PID using SIGTERM (`kill -15`) only.

    Ownership/start-time validation is performed before signaling when a
    package is supplied. There is intentionally NO hard-kill fallback and NO
    `am force-stop` fallback because hard-killing a Roblox clone on the target
    Android 10 environment has been observed to affect sibling clones.

    Returns True only when the exact PID is gone/replaced by a different
    process identity within the verification timeout.
    """
    pid = str(pid or "").strip()
    if not pid or not pid.isdigit():
        return False

    if package:
        package = str(package).strip()
        if not is_valid_package_name(package):
            return False
        identity = get_process_identity(pid)
        if identity is None or identity.get("package") != package:
            return False
        if expected_start_time is None:
            expected_start_time = identity.get("start_time")
        if not is_pid_owned_by_package(
            pid, package, expected_start_time=expected_start_time
        ):
            return False

    subprocess.run(
        ["su", "-c", f"kill -15 {pid}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if package:
        return _wait_until_pid_not_owned(
            pid, package, expected_start_time, timeout=verify_timeout
        )
    return wait_until_process_dead(pid, timeout=verify_timeout)

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
    package = str(package or "").strip()
    if not is_valid_package_name(package):
        return False

    result = subprocess.run(
        ["su", "-c", f"monkey -p {shlex.quote(package)} 1"],
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
    SETELAH proses package tsb dipastikan mati lewat kill_pid_direct() +
    verifikasi tambahan di pemanggil (jangan pernah clear-data proses yang
    masih hidup -- state package saat itu tidak terdefinisi).

    Scoped ke SATU package by design: `pm clear <package>` Android hanya
    menerima satu nama package persis, tidak ada bentuk wildcard/global di
    command ini -- tidak ada jalur di fungsi ini yang bisa menyentuh
    package lain, sama seperti kill_pid_direct() di atas.

    Return True hanya kalau PackageManager benar-benar melaporkan sukses
    (returncode 0 DAN output mengandung 'Success') -- returncode 0 saja
    tidak selalu berarti berhasil untuk command `pm` di sebagian device.
    """
    package = str(package or "").strip()
    if not is_valid_package_name(package):
        return False

    result = subprocess.run(
        ["su", "-c", f"pm clear {shlex.quote(package)}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    output = (result.stdout or "").strip().lower()
    return result.returncode == 0 and "success" in output


def hard_force_stop(package, expected_pid=None, expected_start_time=None):
    """Explicit emergency stop for exactly one package.

    IMPORTANT: normal session/recovery flows MUST NOT use this helper.
    Android's ActivityManager/package-manager stop can have ROM/window-manager
    side effects around floating/clone environments. Keep it available only for
    an explicit operator action after normal ownership-checked SIGTERM
    has failed and the operator accepts those platform-level side effects.
    """
    package = str(package or "").strip()
    if not is_valid_package_name(package):
        return False

    expected_pid = str(expected_pid or "").strip()
    if expected_pid:
        if not is_pid_owned_by_package(
            expected_pid,
            package,
            expected_start_time=expected_start_time,
        ):
            return False

    result = subprocess.run(
        ["su", "-c", f"am force-stop {shlex.quote(package)}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return False

    if not expected_pid:
        return True

    deadline = time.time() + 5.0
    while time.time() < deadline:
        pids = get_pids(package)
        if pids == set():
            return True
        if pids is None:
            time.sleep(0.2)
            continue
        time.sleep(0.2)

    return False
