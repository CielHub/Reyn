"""
Stage 3A: lightweight post-launch join verification.

IMPORTANT REGRESSION RULE:
This module intentionally contains NO terminal/UI/dashboard code.
It only verifies that the target Roblox package is still running after launch.
"""
import re
import subprocess
import time

# LIFECYCLE REVISION (Masalah #1 -- lihat CARRERA_HUB_IMPLEMENTATION_PROMPT_V2):
# ini SATU-SATUNYA signal negatif (kegagalan) yang dianggap reliable di
# project ini, karena regex yang SAMA PERSIS sudah terbukti bekerja di
# core/error_detector.py untuk mendeteksi disconnect/kick asli dari Roblox
# (reason code 266/267/277/279/280 lewat tag [FLog::Network]). SENGAJA
# disinkronkan manual (bukan import langsung) supaya module ini tetap tidak
# bergantung pada error_detector.py (yang punya thread/queue sendiri untuk
# use-case berbeda) -- kalau salah satu diubah, cek juga yang satunya.
_FLOG_NETWORK_PATTERN = re.compile(r"\[FLog::Network\]", re.IGNORECASE)
_DISCONNECT_REASON_PATTERN = re.compile(r"reason\s*:\s*(266|267|277|279|280)", re.IGNORECASE)


def _get_package_pids(pkg_name):
    """Return all current PIDs for the requested package."""
    try:
        result = subprocess.run(
            ['pidof', pkg_name],
            capture_output=True,
            text=True,
            timeout=2,
            errors='replace',
        )
    except Exception:
        return set()
    return {pid for pid in (result.stdout or '').strip().split() if pid.isdigit()}


def _extract_logcat_pid(line):
    """Extract PID from Android logcat -v threadtime output."""
    match = re.match(r'^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+(\d+)\s+\d+\s+[VDIWEFAS]\s+', line)
    return match.group(1) if match else ""


def has_recent_disconnect_signal(pkg_name, since_time_str, timeout_seconds=3, pid=None):
    """Check disconnect/kick signals for the requested package/PID only.

    The previous implementation searched the whole logcat buffer, which could
    let Error 266/267/277/279/280 from another Roblox clone affect this package.
    When no PID is supplied, current PIDs are resolved from pkg_name.
    """
    tracked_pids = {str(pid)} if pid is not None and str(pid).strip() else _get_package_pids(pkg_name)
    if not tracked_pids:
        return False, None

    try:
        result = subprocess.run(
            ['logcat', '-d', '-T', since_time_str, '-v', 'threadtime'],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            errors='replace',
        )
    except Exception:
        return False, None

    for line in (result.stdout or '').splitlines():
        line_pid = _extract_logcat_pid(line)
        if line_pid not in tracked_pids:
            continue
        if not _FLOG_NETWORK_PATTERN.search(line):
            continue
        match = _DISCONNECT_REASON_PATTERN.search(line)
        if match:
            return True, match.group(1)
    return False, None


def _foreground_package():
    """Return the Android activity/focus line when available."""
    for cmd in (
        ['dumpsys', 'activity', 'activities'],
        ['dumpsys', 'window', 'windows'],
    ):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=2,
                errors='replace',
            )
            for line in (result.stdout or '').splitlines():
                if any(k in line for k in ('mResumedActivity', 'mCurrentFocus', 'mFocusedApp')):
                    return line
        except Exception:
            continue
    return ''


def verify_join(pkg_name, minimum_wait=0.5, expected_pid=None):
    """Verify that the Roblox package survived the launch.

    Foreground activity is informative rather than mandatory because Delta Lite
    can run in a floating window and may not own Android's global foreground
    activity. A live process is therefore sufficient for this stage.
    """
    if minimum_wait > 0:
        time.sleep(minimum_wait)

    try:
        result = subprocess.run(
            ['pidof', pkg_name],
            capture_output=True,
            text=True,
            timeout=2,
            errors='replace',
        )
        pids = [pid for pid in (result.stdout or '').strip().split() if pid.isdigit()]
    except Exception:
        pids = []

    if not pids:
        return False, 'PROCESS_NOT_RUNNING'

    if expected_pid is not None:
        expected_pid = str(expected_pid).strip()
        if not expected_pid.isdigit():
            return False, 'INVALID_EXPECTED_PID'
        if expected_pid not in pids:
            return False, 'EXPECTED_PID_NOT_RUNNING'
        pid = expected_pid
    else:
        pid = pids[0]

    focus = _foreground_package()
    if not focus:
        return True, 'PROCESS_ALIVE'
    if pkg_name in focus:
        return True, 'FOREGROUND_PACKAGE'
    return True, 'PROCESS_ALIVE'
