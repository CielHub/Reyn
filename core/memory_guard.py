"""
Modul : memory_guard.py
Tanggung Jawab:
- Menjalankan daemon Memory Guard.
- Membaca ketersediaan RAM (/proc/meminfo).
- Mencegah spam event menggunakan cooldown dan state lock.
- Mengirim event Low Memory ke RecoveryManager.
"""

import threading
import time
import queue

_memory_event_queue = queue.Queue()

class MemoryGuard:

    def __init__(self):
        self._running = False
        self._thread = None
        self.available_memory_mb = 0
        self.event_sent = False
        self.cooldown_until = 0
        self.config_data = {}

    def configure(self, config_data):
        self.config_data = config_data if config_data else {}

    def start(self):
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._worker,
            daemon=True,
            name="MemoryGuard"
        )
        self._thread.start()

    def stop(self):
        self._running = False

    def reset_event(self):
        self.event_sent = False

    def get_available_memory_mb(self):
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        kb = int(line.split()[1])
                        return kb // 1024
        except Exception:
            return None
        return None

    def is_low_memory(self):
        threshold = self.config_data.get("MEMORY_THRESHOLD_MB", 200)
        return self.available_memory_mb <= threshold

    def _worker(self):
        while self._running:
            is_enabled = self.config_data.get("MEMORY_GUARD_ENABLED", True)
            interval = self.config_data.get("MEMORY_CHECK_INTERVAL", 5)
            
            if not is_enabled:
                time.sleep(interval)
                continue

            available_mb = self.get_available_memory_mb()
            
            if available_mb is not None:
                self.available_memory_mb = available_mb
                
                threshold = self.config_data.get("MEMORY_THRESHOLD_MB", 200)
                emergency = self.config_data.get("MEMORY_EMERGENCY_MB", 100)
                cooldown_duration = self.config_data.get("MEMORY_RECOVERY_COOLDOWN", 300)
                
                # Reset lock otomatis jika RAM kembali sehat
                if available_mb > threshold:
                    self.event_sent = False
                
                now = time.time()
                
                if (
                    available_mb <= threshold
                    and not self.event_sent
                    and now >= self.cooldown_until
                ):
                    event = {
                        "type": "MEMORY",
                        "available_mb": available_mb,
                        "is_emergency": available_mb <= emergency,
                        "timestamp": now
                    }
                    _memory_event_queue.put(event)
                    self.event_sent = True
                    self.cooldown_until = now + cooldown_duration
                    
            time.sleep(interval)

_guard = MemoryGuard()

def start_memory_guard(config_data=None):
    _guard.configure(config_data)
    _guard.start()

def stop_memory_guard():
    _guard.stop()

def reset_memory_guard():
    _guard.reset_event()

def has_memory_event():
    return not _memory_event_queue.empty()

def get_memory_event():
    try:
        return _memory_event_queue.get_nowait()
    except queue.Empty:
        return None
      
