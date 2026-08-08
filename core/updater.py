"""
Modul: updater.py
Tanggung Jawab: Orchestrator utama yang menghubungkan Provider, State, Event, dan Validasi Keamanan.
"""
from typing import Tuple
from core.states import UpdaterState
from core.events import event_bus, UpdaterEvent
from core.models.update import UpdateInfo, UpdateResult, UpdateErrorCode
from core.providers.base import BaseUpdateProvider

class AutoUpdater:
    def __init__(self, provider: BaseUpdateProvider):
        self._state = UpdaterState.IDLE
        self.provider = provider

    def set_state(self, new_state: UpdaterState, reason: str = None):
        """Satu pintu perubahan state, mendukung metadata untuk log/event."""
        self._state = new_state
        # Logger/Discord nanti tinggal mendengarkan event ini
        # event_bus.emit(UpdaterEvent.STATE_CHANGED, state=new_state, reason=reason)

    def get_state(self) -> UpdaterState:
        return self._state

    def is_safe_to_update(self) -> Tuple[bool, str]:
        """
        Validasi tersentralisasi.
        TODO: Cek keberadaan thread Monitor, Cache Cleaner, dll.
        """
        return True, "Sistem dalam keadaan aman untuk dihentikan sementara."

    def check_for_updates(self, current_version: str) -> UpdateInfo:
        self.set_state(UpdaterState.CHECKING)
        event_bus.emit(UpdaterEvent.UPDATE_CHECK_STARTED, version=current_version)
        
        try:
            info = self.provider.fetch_update_info(current_version)
            
            if info.has_update:
                self.set_state(UpdaterState.UPDATE_AVAILABLE)
                event_bus.emit(UpdaterEvent.UPDATE_AVAILABLE, info=info)
            else:
                self.set_state(UpdaterState.IDLE)
                
            return info
            
        except Exception as e:
            error_reason = str(e)
            self.set_state(UpdaterState.ERROR, reason=error_reason)
            event_bus.emit(UpdaterEvent.UPDATE_FAILED, reason=error_reason)
            
            return UpdateInfo(
                current_version=current_version,
                latest_version=current_version,
                has_update=False,
                error_code=UpdateErrorCode.UNKNOWN_ERROR,
                reason=error_reason
            )

    def execute_update(self, current_version: str, latest_version: str) -> UpdateResult:
        # 1. Validasi Keamanan Internal
        safe, reason = self.is_safe_to_update()
        if not safe:
            self.set_state(UpdaterState.ERROR, reason=reason)
            event_bus.emit(UpdaterEvent.UPDATE_FAILED, reason=reason)
            
            return UpdateResult(
                success=False,
                reason=reason,
                restart_required=False,
                current_version=current_version,
                latest_version=latest_version,
                error_code=UpdateErrorCode.PERMISSION_DENIED
            )

        # 2. Persiapan Download
        self.set_state(UpdaterState.DOWNLOADING)
        event_bus.emit(UpdaterEvent.DOWNLOAD_STARTED)
        
        # 3. Eksekusi lewat Provider
        result = self.provider.perform_update(current_version, latest_version)
        
        # 4. Evaluasi Hasil (Atomic/Rollback Handling)
        if result.success:
            event_bus.emit(UpdaterEvent.DOWNLOAD_FINISHED)
            self.set_state(UpdaterState.RESTARTING)
            event_bus.emit(UpdaterEvent.UPDATE_COMPLETED, result=result)
            
            # Restart dieksekusi oleh Main Menu, bukan Updater.
            # Updater mengubah state kembali ke IDLE setelah ini.
            self.set_state(UpdaterState.IDLE)
        else:
            self.set_state(UpdaterState.ERROR, reason=result.reason)
            event_bus.emit(UpdaterEvent.UPDATE_FAILED, reason=result.reason)
            
            # Kembali ke IDLE tanpa merusak program lama (Rollback)
            self.set_state(UpdaterState.IDLE)
            
        return result
      
