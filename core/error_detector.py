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

_event_queue = queue.Queue()

# Debounce per PID identity + reason.
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

# 287 = "Koneksi Terputus ... Server telah dimatikan" (server shutdown). Pada
# dialog ini proses Roblox TETAP HIDUP (PID ada), jadi watchdog PID tidak bisa
# menangkapnya -- satu-satunya sinyal adalah baris log ini.
REASON_PATTERN = re.compile(
    r"reason\s*:\s*(266|267|277|279|280|287)",
    re.IGNORECASE
)

# Diagnostik: kode reason 3 digit APA PUN pada baris FLog::Network. Dipakai
# hanya untuk mencatat kode yang BELUM ada di REASON_PATTERN (sekali per kode),
# supaya kode error baru bisa ketahuan dari log agent tanpa menebak.
ANY_REASON_PATTERN = re.compile(r"reason\s*:\s*(\d{3})", re.IGNORECASE)
_seen_unknown_reasons = set()

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
                        any_match = ANY_REASON_PATTERN.search(line)
                        if any_match and any_match.group(1) not in _seen_unknown_reasons:
                            _seen_unknown_reasons.add(any_match.group(1))
                            print(f"[ERROR_DETECTOR] Reason {any_match.group(1)} terlihat di log "
                                  f"tapi TIDAK ditangani: {line.strip()[:200]}")
                        continue

                    pid = pid_match.group(1)
                    identity = process_manager.get_process_identity(pid)
                    # Do not enqueue an event when PID ownership cannot be proven.
                    if not identity or not identity.get("package"):
                        continue

                    reason = int(reason_match.group(1))
                    event_key = (pid, identity["start_time"], reason)
                    now = time.time()
                    last = _last_event.get(event_key, 0)
                    if now - last < DEBOUNCE_SECONDS:
                        continue

                    _last_event[event_key] = now
                    event = {
                        "pid": pid,
                        "package": identity["package"],
                        "pid_start_time": identity["start_time"],
                        "reason": reason,
                        "incident_id": uuid.uuid4().hex,
                        "timestamp": now,
                        "raw": line.strip(),
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
