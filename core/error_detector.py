"""
Modul : error_detector.py
Tanggung Jawab:
- Menjalankan daemon pembacaan logcat (hanya membaca Roblox FLog::Network).
- Mengambil PID dan Reason yang relevan.
- Melakukan Debounce agar tidak spam Queue.
- Mengirim event ke antrean secara murni (tanpa menyentuh stats/recovery).
"""

import subprocess
import threading
import queue
import re
import time
import uuid

from core import process_manager

# ==========================================================
# EVENT QUEUE
# ==========================================================

_event_queue = queue.Queue(maxsize=1000)

# Debounce per PID identity + reason.
_last_event = {}
_LAST_EVENT_TTL_SECONDS = 600

# Lama debounce (detik)
DEBOUNCE_SECONDS = 5

# ==========================================================
# REGEX
# ==========================================================

PID_PATTERN = re.compile(
    r"Roblox\s+\(\s*(\d+)\)"
)

NETWORK_PATTERN = re.compile(
    r"\[FLog::Network\]"
)

# Known Roblox disconnect/error codes observed by this project.
# Keep the list explicit to avoid turning arbitrary numbers into recovery triggers.
SUPPORTED_REASON_CODES = (266, 267, 277, 279, 280, 285, 287)

# Typical FLog::Network format: "Sending disconnect with reason: 267".
REASON_PATTERN = re.compile(
    r"reason\s*[:=]\s*(266|267|277|279|280|285|287)\b",
    re.IGNORECASE,
)

# Delta/modified clients can also expose the same condition as a UI/log line
# such as "Error code: 267" instead of FLog::Network's "reason: 267".
ERROR_CODE_PATTERN = re.compile(
    r"error\s*code\s*[:=]?\s*(266|267|277|279|280|285|287)\b",
    re.IGNORECASE,
)

ANY_REASON_PATTERN = re.compile(r"reason\s*[:=]\s*(\d{3})\b", re.IGNORECASE)
ANY_ERROR_CODE_PATTERN = re.compile(r"error\s*code\s*[:=]?\s*(\d{3})\b", re.IGNORECASE)
_seen_unknown_reasons = set()

# ==========================================================
# DETECTOR
# ==========================================================

class ErrorDetector:

    def __init__(self):
        self._running = False
        self._thread = None
        self._process = None
        self._process_lock = threading.Lock()

    def start(self):
        if self._running:
            return

        self._running = True

        self._thread = threading.Thread(
            target=self._worker,
            daemon=True,
            name="RobloxErrorDetector"
        )
        self._thread.start()

    def stop(self):
        self._running = False
        with self._process_lock:
            proc = self._process
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

    def _worker(self):
        cmd = [
            "su",
            "-c",
            "logcat -v brief -s Roblox"
        ]

        while self._running:
            process = None
            try:
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
                with self._process_lock:
                    self._process = process

                while self._running:
                    line = process.stdout.readline()
                    if not line:
                        break
                    pid_match = PID_PATTERN.search(line)
                    if not pid_match:
                        continue

                    # Accept either the canonical FLog::Network reason or the
                    # explicit "Error code: N" format emitted by some modified
                    # Roblox clients. A bare unrelated number is never enough.
                    reason_match = REASON_PATTERN.search(line)
                    error_code_match = ERROR_CODE_PATTERN.search(line)
                    if not reason_match and not error_code_match:
                        any_match = ANY_REASON_PATTERN.search(line) or ANY_ERROR_CODE_PATTERN.search(line)
                        if any_match and any_match.group(1) not in _seen_unknown_reasons:
                            _seen_unknown_reasons.add(any_match.group(1))
                            print(
                                f"[ERROR_DETECTOR] Error code/reason {any_match.group(1)} terlihat di log "
                                f"tapi TIDAK ditangani: {line.strip()[:200]}"
                            )
                        continue

                    # Do not accept an arbitrary "error code" from a random
                    # Roblox log line unless it carries either network context
                    # or the explicit error-code label.
                    if not NETWORK_PATTERN.search(line) and not error_code_match:
                        continue

                    pid = pid_match.group(1)
                    identity = process_manager.get_process_identity(pid)
                    if not identity or not identity.get("package"):
                        continue

                    reason = int((reason_match or error_code_match).group(1))
                    start_time = identity.get("start_time")
                    if start_time is None:
                        continue
                    event_key = (pid, start_time, reason)
                    now = time.time()

                    stale_before = now - _LAST_EVENT_TTL_SECONDS
                    if _last_event:
                        for old_key, old_ts in list(_last_event.items()):
                            if old_ts < stale_before:
                                _last_event.pop(old_key, None)

                    last = _last_event.get(event_key, 0)
                    if now - last < DEBOUNCE_SECONDS:
                        continue
                    _last_event[event_key] = now

                    event = {
                        "pid": pid,
                        "package": identity["package"],
                        "pid_start_time": start_time,
                        "reason": reason,
                        "incident_id": uuid.uuid4().hex,
                        "timestamp": now,
                        "raw": line.strip(),
                    }

                    try:
                        _event_queue.put_nowait(event)
                    except queue.Full:
                        # Drop the oldest item, preserving the newest incident.
                        try:
                            _event_queue.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            _event_queue.put_nowait(event)
                        except queue.Full:
                            pass
            except Exception:
                if self._running:
                    time.sleep(2)
            finally:
                if process is not None:
                    with self._process_lock:
                        if self._process is process:
                            self._process = None
                    try:
                        if process.poll() is None:
                            process.terminate()
                    except Exception:
                        pass

# ==========================================================
# SINGLETON
# ==========================================================

_detector = ErrorDetector()

# ==========================================================
# PUBLIC API
# ==========================================================

def start_error_detector():
    _detector.start()

def stop_error_detector():
    _detector.stop()

def has_event():
    return not _event_queue.empty()

def get_event():
    try:
        return _event_queue.get_nowait()
    except queue.Empty:
        return None

def drain_events():
    """Buang semua event yang masih menumpuk di queue.

    Dipakai sesaat setelah GLOBAL recovery mulai: event lain yang sudah
    ada di queue pada saat itu berasal dari insiden yang sama (mis. WiFi
    putus kena ke banyak package sekaligus) dan sudah tercakup oleh
    siklus recovery yang baru saja dimulai. Tanpa ini, event-event lama
    tsb diproses satu per satu setelahnya dan masing-masing memicu
    siklus GLOBAL recovery baru secara beruntun.
    """
    while True:
        try:
            _event_queue.get_nowait()
        except queue.Empty:
            break
