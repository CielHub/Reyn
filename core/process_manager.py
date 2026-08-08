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
    """Return normalized numeric PIDs reported for one Android package."""
    try:
        result = subprocess.run(
            ['pidof', pkg_name], capture_output=True, text=True, timeout=2
        )
        return [pid for pid in result.stdout.strip().split() if pid.isdigit()]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def get_pid(pkg_name):
    """Return one canonical PID for watchdog/recovery comparisons."""
    pids = get_package_pids(pkg_name)
    return pids[0] if pids else ""

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

