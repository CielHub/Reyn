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


def has_recent_disconnect_signal(pkg_name, since_time_str, timeout_seconds=3):
    """One-shot check (buffer dump, BUKAN live tail) apakah ada bukti
    disconnect/kick asli dari Roblox di logcat sejak `since_time_str`
    (format sama seperti dipakai launcher.py: '%m-%d %H:%M:%S.000').

    Dipakai sebagai signal NEGATIF tambahan saat join tidak bisa dipastikan
    lewat keyword sukses (lihat launcher.py) -- kalau ada bukti kick/error
    asli, JANGAN anggap sukses walau proses masih hidup. Kalau tidak ada
    bukti kick sama sekali, fungsi ini TIDAK membuktikan sukses -- itu bukan
    tanggung jawabnya (lihat verify_join() untuk itu).

    Return (found: bool, code: str|None).
    """
    try:
        result = subprocess.run(
            ['logcat', '-d', '-T', since_time_str, '-v', 'time'],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            errors='replace',
        )
    except Exception:
        return False, None

    for line in (result.stdout or '').splitlines():
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
