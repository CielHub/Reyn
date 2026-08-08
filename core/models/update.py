"""
Modul: models/update.py
Tanggung Jawab: Data Transfer Objects (DTO) khusus untuk domain Auto Updater.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

class UpdateErrorCode(Enum):
    NONE = "NONE"
    NETWORK_ERROR = "NETWORK_ERROR"
    LOCAL_MODIFICATION = "LOCAL_MODIFICATION"
    VERSION_NOT_FOUND = "VERSION_NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"

@dataclass
class UpdateInfo:
    current_version: str
    latest_version: str
    has_update: bool
    release_date: Optional[str] = None
    release_notes: Optional[str] = None
    error_code: UpdateErrorCode = UpdateErrorCode.NONE
    reason: Optional[str] = None

@dataclass
class UpdateResult:
    success: bool
    reason: str
    restart_required: bool
    current_version: str
    latest_version: str
    error_code: UpdateErrorCode = UpdateErrorCode.NONE
  
