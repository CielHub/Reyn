"""
Modul : error_detector.py
Tanggung Jawab:
- Membaca logcat per PID yang sudah didaftarkan oleh session_agent.
- Mengambil PID dan Reason yang relevan.
- Melakukan Debounce agar tidak spam Queue.
- Mengirim event ke antrean secara murni (tanpa menyentuh stats/recovery).

ISOLATION CONTRACT
------------------
Tidak ada lagi global ``logcat -s Roblox`` stream untuk semua clone.
Setiap PID target mempunyai reader ``logcat --pid=<PID>`` sendiri.
Sebuah baris baru diterima hanya jika:
  1) PID pada baris sama dengan PID reader,
  2) identity / package / kernel start_time masih cocok dengan target.
Dengan demikian log dari clone lain tidak pernah menjadi input recovery
package target, dan tidak ada ``logcat -c`` pada jalur runtime.
"""

import queue
import re
import subprocess
import threading
import time
import uuid

from core import process_manager

# ==========================================================
# EVENT QUEUE
# ==========================================================

_event_queue = queue.Queue(maxsize=1000)

# Debounce per package + PID identity + reason.
_last_event = {}
_last_event_lock = threading.Lock()
_LAST_EVENT_TTL_SECONDS = 600

# Lama debounce (detik)
DEBOUNCE_SECONDS = 5

# ==========================================================
# REGEX
# ==========================================================

PID_PATTERN = re.compile(
    r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+(\d+)\s+\d+\s+[VDIWEFAS]\s+",
    re.IGNORECASE,
)

NETWORK_PATTERN = re.compile(
    r"\[FLog::Network\]",
    re.IGNORECASE,
)

SUPPORTED_REASON_CODES = (266, 267, 277, 279, 280, 285, 287)

REASON_PATTERN = re.compile(
    r"reason\s*[:=]\s*(266|267|277|279|280|285|287)\b",
    re.IGNORECASE,
)

ERROR_CODE_PATTERN = re.compile(
    r"error\s*code\s*[:=]?\s*(266|267|277|279|280|285|287)\b",
    re.IGNORECASE,
)

ANY_REASON_PATTERN = re.compile(r"reason\s*[:=]\s*(\d{3})\b", re.IGNORECASE)
ANY_ERROR_CODE_PATTERN = re.compile(r"error\s*code\s*[:=]?\s*(\d{3})\b", re.IGNORECASE)

_seen_unknown_reasons = set()
_seen_unknown_lock = threading.Lock()


class ErrorDetector:
    """PID-scoped logcat supervisor."""

    def __init__(self):
        self._running = False
        self._thread = None
        self._targets = {}  # pid -> {"package": str, "start_time": int}
        self._targets_lock = threading.RLock()

        self._watchers = {}  # pid -> {"thread": Thread, "process": Popen}
        self._watchers_lock = threading.RLock()

    # ------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------

    def start(self):
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._supervisor,
            daemon=True,
            name="RobloxErrorDetector",
        )
        self._thread.start()

    def stop(self):
        self._running = False
        with self._targets_lock:
            self._targets.clear()
        with self._watchers_lock:
            watchers = list(self._watchers.values())

        for watcher in watchers:
            proc = watcher.get("process")
            if proc is None:
                continue
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

    # ------------------------------------------------------
    # Target registration
    # ------------------------------------------------------

    def set_package_targets(self, package, pid_start_times):
        """Replace the watched PID set for one exact package.

        ``pid_start_times`` is ``{pid: kernel_start_time}``. An empty mapping
        removes all watchers for that package. ``None`` means do not alter the
        current registration.
        """
        package = str(package or "").strip()
        if not process_manager.is_valid_package_name(package):
            return

        clean = {}
        if pid_start_times is not None:
            for raw_pid, start_time in dict(pid_start_times).items():
                pid = str(raw_pid or "").strip()
                if not pid.isdigit() or start_time is None:
                    continue
                try:
                    clean[pid] = int(start_time)
                except (TypeError, ValueError):
                    continue

        with self._targets_lock:
            for pid, target in list(self._targets.items()):
                if target.get("package") == package:
                    self._targets.pop(pid, None)

            for pid, start_time in clean.items():
                self._targets[pid] = {
                    "package": package,
                    "start_time": start_time,
                }

    def clear_package_targets(self, package):
        package = str(package or "").strip()
        if not package:
            return
        with self._targets_lock:
            for pid, target in list(self._targets.items()):
                if target.get("package") == package:
                    self._targets.pop(pid, None)

    def _get_target(self, pid):
        with self._targets_lock:
            target = self._targets.get(str(pid))
            return dict(target) if target else None

    # ------------------------------------------------------
    # Supervisor / reader
    # ------------------------------------------------------

    def _supervisor(self):
        while self._running:
            try:
                with self._targets_lock:
                    targets = {
                        pid: dict(target)
                        for pid, target in self._targets.items()
                    }

                for pid, target in targets.items():
                    if not self._running:
                        break
                    with self._watchers_lock:
                        watcher = self._watchers.get(pid)
                        alive = bool(
                            watcher
                            and watcher.get("thread")
                            and watcher["thread"].is_alive()
                        )
                    if not alive:
                        self._start_watcher(pid, target)

                # Reap finished watcher records.
                with self._watchers_lock:
                    for pid, watcher in list(self._watchers.items()):
                        thread = watcher.get("thread")
                        if thread is not None and not thread.is_alive():
                            self._watchers.pop(pid, None)

            except Exception:
                # Supervisor must survive an individual PID reader failure.
                pass

            time.sleep(0.5)

    def _start_watcher(self, pid, target):
        with self._watchers_lock:
            existing = self._watchers.get(pid)
            if existing and existing.get("thread") and existing["thread"].is_alive():
                return

            thread = threading.Thread(
                target=self._watch_pid,
                args=(pid, target["package"], target["start_time"]),
                daemon=True,
                name=f"RobloxLogcat-{pid}",
            )
            self._watchers[pid] = {
                "thread": thread,
                "process": None,
            }
            thread.start()

    def _set_watcher_process(self, pid, proc):
        with self._watchers_lock:
            watcher = self._watchers.get(pid)
            if watcher is not None:
                watcher["process"] = proc

    def _clear_watcher(self, pid):
        with self._watchers_lock:
            self._watchers.pop(pid, None)

    def _watch_pid(self, pid, package, expected_start_time):
        process = None
        try:
            # PID is the isolation primitive. There is intentionally NO
            # fallback to a global Roblox log stream.
            cmd = [
                "su",
                "-c",
                f"logcat --pid={pid} -v threadtime -s Roblox",
            ]

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="ignore",
                bufsize=1,
            )
            self._set_watcher_process(pid, process)

            while self._running:
                target = self._get_target(pid)
                if (
                    target is None
                    or target.get("package") != package
                    or int(target.get("start_time", -1)) != int(expected_start_time)
                ):
                    break

                line = process.stdout.readline()
                if not line:
                    break

                line_pid_match = PID_PATTERN.search(line)
                if not line_pid_match:
                    # Without an explicit PID we cannot prove isolation.
                    continue

                line_pid = line_pid_match.group(1)
                if line_pid != pid:
                    continue

                reason_match = REASON_PATTERN.search(line)
                error_code_match = ERROR_CODE_PATTERN.search(line)
                if not reason_match and not error_code_match:
                    any_match = (
                        ANY_REASON_PATTERN.search(line)
                        or ANY_ERROR_CODE_PATTERN.search(line)
                    )
                    if any_match:
                        code = any_match.group(1)
                        with _seen_unknown_lock:
                            unseen = code not in _seen_unknown_reasons
                            if unseen:
                                _seen_unknown_reasons.add(code)
                        if unseen:
                            print(
                                f"[ERROR_DETECTOR] Error code/reason {code} terlihat "
                                f"di log PID {pid}, tetapi tidak ditangani: "
                                f"{line.strip()[:200]}"
                            )
                    continue

                if not NETWORK_PATTERN.search(line) and not error_code_match:
                    continue

                # Revalidate package + start_time on every candidate event.
                # This closes the PID-reuse race.
                identity = process_manager.get_process_identity(pid)
                if not identity:
                    continue
                if identity.get("package") != package:
                    continue
                if int(identity.get("start_time", -1)) != int(expected_start_time):
                    continue

                reason = int((reason_match or error_code_match).group(1))
                now = time.time()

                with _last_event_lock:
                    stale_before = now - _LAST_EVENT_TTL_SECONDS
                    for old_key, old_ts in list(_last_event.items()):
                        if old_ts < stale_before:
                            _last_event.pop(old_key, None)

                    event_key = (package, pid, expected_start_time, reason)
                    last = _last_event.get(event_key, 0)
                    if now - last < DEBOUNCE_SECONDS:
                        continue
                    _last_event[event_key] = now

                event = {
                    "pid": pid,
                    "package": package,
                    "pid_start_time": expected_start_time,
                    "reason": reason,
                    "incident_id": uuid.uuid4().hex,
                    "timestamp": now,
                    "raw": line.strip(),
                }

                try:
                    _event_queue.put_nowait(event)
                except queue.Full:
                    try:
                        _event_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        _event_queue.put_nowait(event)
                    except queue.Full:
                        pass

        except Exception:
            # A missing/unsupported --pid implementation must never trigger a
            # global fallback. The watcher simply ends and the supervisor can
            # retry while the target remains registered.
            pass
        finally:
            if process is not None:
                try:
                    if process.poll() is None:
                        process.terminate()
                except Exception:
                    pass
            self._clear_watcher(pid)


_detector = ErrorDetector()


# ==========================================================
# PUBLIC API
# ==========================================================

def start_error_detector():
    _detector.start()


def stop_error_detector():
    _detector.stop()


def set_package_targets(package, pid_start_times):
    _detector.set_package_targets(package, pid_start_times)


def clear_package_targets(package):
    _detector.clear_package_targets(package)


def has_event():
    return not _event_queue.empty()


def get_event():
    try:
        return _event_queue.get_nowait()
    except queue.Empty:
        return None


def drain_events():
    """Clear queued *events* only.

    This function never calls ``logcat -c`` and never mutates the Android
    logcat buffer. It is retained only for callers that explicitly want to
    discard already queued application events.
    """
    while True:
        try:
            _event_queue.get_nowait()
        except queue.Empty:
            break
