"""
Modul: events.py
Tanggung Jawab: Mendefinisikan Event Enum dan menyediakan EventBus sederhana untuk komunikasi antar modul yang ter-decouple.
"""
from enum import Enum
from typing import Callable, Dict, List

class UpdaterEvent(Enum):
    UPDATE_CHECK_STARTED = "UPDATE_CHECK_STARTED"
    UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
    DOWNLOAD_STARTED = "DOWNLOAD_STARTED"
    DOWNLOAD_FINISHED = "DOWNLOAD_FINISHED"
    UPDATE_FAILED = "UPDATE_FAILED"
    UPDATE_COMPLETED = "UPDATE_COMPLETED"

class EventBus:
    """Implementasi Observer Pattern minimalis."""
    def __init__(self):
        self._listeners: Dict[str, List[Callable]] = {}

    def subscribe(self, event_type: Enum, listener: Callable):
        event_name = event_type.name
        if event_name not in self._listeners:
            self._listeners[event_name] = []
        self._listeners[event_name].append(listener)

    def emit(self, event_type: Enum, **kwargs):
        event_name = event_type.name
        if event_name in self._listeners:
            for listener in self._listeners[event_name]:
                try:
                    listener(**kwargs)
                except Exception:
                    # Kita diamkan error di listener agar tidak men-crash proses utama (misal: logger gagal nulis)
                    pass

# Singleton instance yang akan di-import oleh seluruh aplikasi
event_bus = EventBus()

