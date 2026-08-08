"""
Modul: states.py
Tanggung Jawab: Menyimpan semua definisi State aplikasi (Enum) agar tidak terjadi circular import.
"""
from enum import Enum

class UpdaterState(Enum):
    IDLE = "IDLE"
    CHECKING = "CHECKING"
    UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
    DOWNLOADING = "DOWNLOADING"
    INSTALLING = "INSTALLING"
    RESTARTING = "RESTARTING"
    ERROR = "ERROR"

# TODO Phase 3: Tambahkan LauncherState, MonitorState, dll di sini.

