"""
Stage 3A: lightweight post-launch join verification.

IMPORTANT REGRESSION RULE:
This module intentionally contains NO terminal/UI/dashboard code.
It only verifies that the target Roblox package is still running after launch.
"""
import subprocess
import time


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


def verify_join(pkg_name, minimum_wait=0.5):
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
        )
        pid = result.stdout.strip()
    except Exception:
        pid = ''

    if not pid:
        return False, 'PROCESS_NOT_RUNNING'

    focus = _foreground_package()
    if not focus:
        return True, 'PROCESS_ALIVE'
    if pkg_name in focus:
        return True, 'FOREGROUND_PACKAGE'
    return True, 'PROCESS_ALIVE'
