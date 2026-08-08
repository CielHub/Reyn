"""
Modul : process_manager.py
Tanggung Jawab:
- Manajemen proses secara low-level.
- Pencarian PID suatu package.
- Menangani terminasi/kill (Graceful Terminate).
"""

import subprocess
import time

def get_package_pids(pkg_name):
    """Return a deterministic set-like list of numeric PIDs for one package.

    Android may report more than one PID for a package and may change their
    output order between calls.  Callers that care about process identity must
    therefore use membership checks, not positional equality.
    """
    try:
        result = subprocess.run(
            ['pidof', pkg_name], capture_output=True, text=True, timeout=2
        )
        pids = {pid for pid in result.stdout.strip().split() if pid.isdigit()}
        return sorted(pids, key=int)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def package_has_pid(pkg_name, pid):
    """Return True when *pid* is still owned/reported by *pkg_name*."""
    if not pid:
        return False
    return str(pid) in get_package_pids(pkg_name)


def choose_package_pid(pkg_name, preferred_pid='', exclude_pid=''):
    """Choose a deterministic PID without depending on `pidof` output order.

    If a previously verified PID is still present, it wins. Otherwise the
    first deterministic non-excluded PID is returned.
    """
    pids = get_package_pids(pkg_name)
    preferred_pid = str(preferred_pid or '')
    exclude_pid = str(exclude_pid or '')

    if preferred_pid and preferred_pid in pids:
        return preferred_pid

    for pid in pids:
        if pid != exclude_pid:
            return pid
    return ''


def get_pid(pkg_name):
    """Compatibility helper returning one deterministic package PID."""
    return choose_package_pid(pkg_name)

def pid_exists(pid):
    result = subprocess.run(
        ["su", "-c", f"kill -0 {pid}"],
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
    if not pid:
        return False

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
            return True
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
            return True
        time.sleep(0.2)

    # ====================================================
    # STEP 3: Fallback (am force-stop)
    # ====================================================
    if package:
        subprocess.run(
            ["su", "-c", f"am force-stop {package}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)

    return not pid_exists(pid)

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

