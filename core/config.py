"""
Modul: config.py
Tanggung Jawab: Membaca, mem-parsing, menduplikasi template (jika perlu), dan menyimpan file konfigurasi.
"""
import os
import sys
import shutil
from core.logger import log

def load_config(config_path="config.conf"):
    example_path = "config.example.conf"
    
    # --- HOOK MIGRASI & INISIALISASI ---
    if not os.path.isfile(config_path):
        log.warning(f"CONFIG: File {config_path} tidak ditemukan di sistem lokal.")
        
        if os.path.isfile(example_path):
            log.info(f"CONFIG: Menduplikasi template dari {example_path}...")
            try:
                shutil.copy(example_path, config_path)
                log.info(f"CONFIG: File {config_path} berhasil dibuat.")
            except Exception as e:
                log.error(f"CONFIG: Gagal menyalin template konfigurasi! Error: {str(e)}")
                sys.exit(1)
        else:
            log.error(f"CONFIG: Fatal Error! Template {example_path} juga tidak ditemukan.")
            sys.exit(1)
    # -----------------------------------

    # --- PARSING KONFIGURASI (BUG FIX SPASI GAIB) ---
    config = {
        "PRIVATE_SERVER_LINK": "",
        "GLOBAL_PLACE_ID": "",
        "TIMEOUT_SECONDS": 45,
        "RECOVERY_DELAY_SECONDS": 30,
        "DELAY_SECONDS": 3,
        "MAX_RETRIES": 3,
        "COOLDOWN_SECONDS": 300,
        "GRID_ENABLED": 0,        
        "GRID_COLS": 0,           
        "GRID_CELL_W": 0,         
        "GRID_CELL_H": 0,         
        "GRID_MARGIN": 10,
        "GRID_OFFSET_Y": 60,
        "CLEAR_CACHE_MINUTES": 30,
        # Phase D1: optional remote-control plane device identity/heartbeat.
        # Disabled by default so the existing CARRERA-HUB runtime is unchanged.
        "DEVICE_AGENT_ENABLED": 0,
        "DEVICE_NAME": "",
        "DEVICE_GATEWAY_URL": "",
        "DEVICE_ENROLLMENT_TOKEN": "",
        "DEVICE_HEARTBEAT_INTERVAL": 30,
        "DEVICE_GATEWAY_TIMEOUT": 8,
        # Phase D2-L: embedded local controller hub, disabled by default.
        "CONTROLLER_ENABLED": 0,
        "CONTROLLER_HOST": "0.0.0.0",
        "CONTROLLER_PORT": 8765,
        "CONTROLLER_ENROLLMENT_TOKEN": "",
        "CONTROLLER_STATUS_TOKEN": "",
        "CONTROLLER_OFFLINE_AFTER": 90,
    }

    with open(config_path, 'r') as f:
        for line in f:
            # Bersihkan ujung baris dulu
            line = line.strip()
            
            # Abaikan baris kosong atau komentar
            if not line or line.startswith("#"):
                continue
                
            if "=" in line:
                key, val = line.split("=", 1)
                # FIX: Bersihkan spasi di key dan value sebelum diproses
                key = key.strip()
                val = val.strip().strip('"\'')
                
                if key in ["PRIVATE_SERVER_LINK", "GLOBAL_PLACE_ID"]:
                    config[key] = val
                elif key in ["TIMEOUT_SECONDS", "RECOVERY_DELAY_SECONDS", "DELAY_SECONDS", "MAX_RETRIES", "COOLDOWN_SECONDS", 
                             "GRID_ENABLED", "GRID_COLS", "GRID_CELL_W", "GRID_CELL_H", 
                             "GRID_MARGIN", "GRID_OFFSET_Y", "CLEAR_CACHE_MINUTES",
                             "DEVICE_AGENT_ENABLED", "DEVICE_HEARTBEAT_INTERVAL", "DEVICE_GATEWAY_TIMEOUT",
                             "CONTROLLER_ENABLED", "CONTROLLER_PORT", "CONTROLLER_OFFLINE_AFTER"]:
                    try: 
                        config[key] = int(val)
                    except ValueError: 
                        pass
                elif key in ["DEVICE_NAME", "DEVICE_GATEWAY_URL", "DEVICE_ENROLLMENT_TOKEN",
                            "CONTROLLER_HOST", "CONTROLLER_ENROLLMENT_TOKEN", "CONTROLLER_STATUS_TOKEN"]:
                    config[key] = val
                elif key.startswith("PKG_"):
                    # Sekarang "PKG_com.roblox.client " akan otomatis jadi "PKG_com.roblox.client"
                    config[key] = val
                    
    _normalize_targets(config)

    log.info("CONFIG: Konfigurasi berhasil dimuat dengan aman.")
    return config

def _normalize_targets(config_data):
    """Menjamin hanya satu target aktif pada setiap scope."""
    # Global: Private Server menang jika keduanya terisi (kompatibilitas config lama).
    if str(config_data.get("PRIVATE_SERVER_LINK", "")).strip():
        config_data["GLOBAL_PLACE_ID"] = ""

    # Package: Private Server menang jika keduanya terisi.
    for key in list(config_data.keys()):
        if key.startswith("PKG_") and key.endswith("_PLACE_ID"):
            pkg = key[len("PKG_"):-len("_PLACE_ID")]
            if str(config_data.get(f"PKG_{pkg}", "")).strip():
                config_data[key] = ""

    return config_data


def save_config(config_data, config_path="config.conf"):
    _normalize_targets(config_data)
    with open(config_path, 'w') as f:
        for key, value in config_data.items():
            if isinstance(value, str):
                f.write(f'{key}="{value}"\n')
            else:
                f.write(f'{key}={value}\n')
    log.info("CONFIG: Konfigurasi berhasil disimpan.")
