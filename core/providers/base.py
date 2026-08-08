"""
Modul: providers/base.py
Tanggung Jawab: Kontrak (Interface) mutlak untuk semua implementasi Updater Provider.
"""
from abc import ABC, abstractmethod
from core.models.update import UpdateInfo, UpdateResult

class BaseUpdateProvider(ABC):
    """
    Kelas abstrak yang mendefinisikan standar input dan output untuk proses update.
    Tidak peduli mekanisme di baliknya (Git/HTTP/Zip), Orchestrator hanya peduli
    pada dua fungsi di bawah ini.
    """
    
    @abstractmethod
    def fetch_update_info(self, current_version: str) -> UpdateInfo:
        """
        Mengekstrak informasi versi terbaru dari remote source.
        """
        pass
        
    @abstractmethod
    def perform_update(self, current_version: str, target_version: str) -> UpdateResult:
        """
        Mengeksekusi proses update secara aman dan mengembalikan hasilnya.
        """
        pass
      
