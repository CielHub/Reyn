"""
Stage 3: lightweight post-launch join verification.
"""
import subprocess
import time

def _foreground_package():
    for cmd in (
        ['dumpsys', 'activity', 'activities'],
        ['dumpsys', 'window', 'windows'],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2, errors='replace')
            for line in (result.stdout or '').splitlines():
                if any(k in line for k in ('mResumedActivity', 'mCurrentFocus', 'mFocusedApp')):
                    return line
        except Exception:
            continue
    return ''

def verify_join(pkg_name, minimum_wait=0.5):
    if minimum_wait > 0:
        time.sleep(minimum_wait)
    try:
        pid = subprocess.run(
            ['pidof', pkg_name], capture_output=True, text=True, timeout=2
        ).stdout.strip()
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
