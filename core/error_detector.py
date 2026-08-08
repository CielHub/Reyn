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

# ==========================================================
# EVENT QUEUE
# ==========================================================

_event_queue = queue.Queue()

# Debounce per PID
_last_event = {}

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

REASON_PATTERN = re.compile(
    r"reason\s*:\s*(266|267|277|279|280)",
    re.IGNORECASE
)

# ==========================================================
# DETECTOR
# ==========================================================

class ErrorDetector:

    def __init__(self):
        self._running = False
        self._thread = None

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

    def _worker(self):
        cmd = [
            "su",
            "-c",
            "logcat -v brief -s Roblox"
        ]

        while self._running:
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    bufsize=1
                )

                while self._running:
                    line = process.stdout.readline()

                    if not line:
                        break

                    if not NETWORK_PATTERN.search(line):
                        continue

                    pid_match = PID_PATTERN.search(line)
                    if not pid_match:
                        continue

                    reason_match = REASON_PATTERN.search(line)
                    if not reason_match:
                        continue

                    pid = pid_match.group(1)
                    now = time.time()
                    last = _last_event.get(pid, 0)

                    # Abaikan event PID yang sama selama debounce time
                    if now - last < DEBOUNCE_SECONDS:
                        continue

                    _last_event[pid] = now

                    event = {
                        "pid": pid,
                        "reason": int(reason_match.group(1)),
                        "timestamp": now,
                        "raw": line.strip()
                    }

                    _event_queue.put(event)

            except Exception:
                time.sleep(2)

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
        
