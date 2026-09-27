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

# TODO Phase 3: Tambahkan LauncherState, dll di sini.

# PackageState (dulu dipakai dashboard standalone Auto Rejoin lewat
# state_machine.py) sudah dihapus -- tidak ada consumer produksi lagi
# setelah refactor: remove standalone auto rejoin engine.
