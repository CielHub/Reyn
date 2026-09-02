"""
Modul: providers/git.py
Tanggung Jawab: Implementasi proses update menggunakan Git (Fetch & Pull).
"""
from core.providers.base import BaseUpdateProvider
from core.models.update import UpdateInfo, UpdateResult, UpdateErrorCode

class GitProvider(BaseUpdateProvider):
    
    def fetch_update_info(self, current_version: str) -> UpdateInfo:
        # TODO: Implementasi HTTP GET ke raw.githubusercontent.com
        # TODO: Parsing string versi dari response
        
        # Contoh struktur return yang akan dibuat nanti:
        return UpdateInfo(
            current_version=current_version,
            latest_version="1.0.0", 
            has_update=False,
            error_code=UpdateErrorCode.NONE
        )

    def perform_update(self, current_version: str, target_version: str) -> UpdateResult:
        # TODO: Eksekusi subprocess 'git status --porcelain'
        # TODO: Jika ada output (perubahan lokal), kembalikan UpdateResult dengan LOCAL_MODIFICATION
        # TODO: Eksekusi subprocess 'git pull origin main'
        
        # Contoh struktur return jika terdeteksi perubahan lokal:
        # return UpdateResult(
        #     success=False,
        #     reason="Terdeteksi perubahan lokal pada file. Harap commit/stash terlebih dahulu.",
        #     restart_required=False,
        #     current_version=current_version,
        #     latest_version=target_version,
        #     error_code=UpdateErrorCode.LOCAL_MODIFICATION
        # )
        pass
      
