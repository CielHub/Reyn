"""Stage 4R2: package-isolated recovery verification.

The verifier is deliberately scoped to one package at a time.  A running
Roblox clone is not considered recovered merely because *some* Roblox process
exists or because an Android command returned success.
"""
import subprocess
import time

from core.process_manager import get_pid


def get_single_pid(pkg):
    return get_pid(pkg)


def _dump(cmd):
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3,
            errors="replace",
        )
        return result.stdout or ""
    except Exception:
        return ""


def _package_ui_signals(pkg):
    """Return strong package-specific UI signals from Android dumpsys.

    We intentionally do not treat arbitrary occurrences of the package name
    as proof of a visible window.  A package can remain present in Android's
    process/task bookkeeping after its UI has disappeared.
    """
    signals = []

    activity = _dump(["dumpsys", "activity", "activities"])
    for line in activity.splitlines():
        lower = line.lower()
        if pkg.lower() not in lower:
            continue
        if any(token in lower for token in (
            "mresumedactivity",
            "resumedactivity",
            "state=resumed",
            "topresumedactivity",
        )):
            signals.append("ACTIVITY_RESUMED")
            break

    windows = _dump(["dumpsys", "window", "windows"])
    for line in windows.splitlines():
        lower = line.lower()
        if pkg.lower() not in lower:
            continue
        if any(token in lower for token in (
            "mcurrentfocus",
            "focusedapp",
            "mhasSurface=true",
            "isonScreen=true",
            "visible=true",
        )):
            signals.append("WINDOW_VISIBLE")
            break

    return tuple(dict.fromkeys(signals))


def verify_recovery(pkg, baseline_pid="", stable_seconds=4.0):
    """Verify only the requested package.

    Requirements:
      1. A PID for the requested package exists.
      2. The PID remains stable for the verification window.
      3. Android exposes a strong UI/activity signal for that same package.

    Returns (ok, reason, pid).
    """
    first_pid = get_single_pid(pkg)
    if not first_pid:
        return False, "TARGET_PROCESS_NOT_RUNNING", ""

    # A recovery should normally produce a fresh PID after force-close.  If a
    # baseline PID is supplied and Android still reports the exact same PID,
    # do not treat the old process as a successful relaunch.
    if baseline_pid and first_pid == str(baseline_pid):
        return False, "OLD_PID_STILL_PRESENT", first_pid

    deadline = time.time() + stable_seconds
    while time.time() < deadline:
        current_pid = get_single_pid(pkg)
        if not current_pid:
            return False, "TARGET_PROCESS_DIED", ""
        if current_pid != first_pid:
            return False, "TARGET_PID_CHANGED", current_pid
        time.sleep(0.5)

    signals = _package_ui_signals(pkg)
    if not signals:
        return False, "TARGET_UI_NOT_CONFIRMED", first_pid

    return True, "+".join(signals), first_pid
