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
    # Standalone Auto Rejoin dihapus -- satu-satunya konfigurasi yang
    # tersisa adalah kredensial agent Joki Control Bot (jalur Discord/
    # Headless). Lihat refactor: remove standalone auto rejoin engine.
    config = {
        # --- PHASE 1: kredensial agent Joki Control Bot (opsional) ---
        # BOT_WS_URL sudah di-hardcode ke alamat NuraHost yang sudah dites
        # berhasil -- user cuma perlu isi DEVICE_ID/DEVICE_TOKEN lewat menu.
        "DEVICE_ID": "",
        "DEVICE_TOKEN": "",
        "BOT_WS_URL": "ws://nano-1.nura.host:5067",
        # Diagnostic release: Phase B active by default.
        "KILL_ONLY_TEST_MODE": False,
        "KILL_THEN_LAUNCH_TEST_MODE": True,
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
                
                if key in ["DEVICE_ID", "DEVICE_TOKEN", "BOT_WS_URL"]:
                    config[key] = val
                elif key in ["KILL_ONLY_TEST_MODE", "KILL_THEN_LAUNCH_TEST_MODE"]:
                    config[key] = val.lower() in {"1", "true", "yes", "on"}
                    
    log.info("CONFIG: Konfigurasi berhasil dimuat dengan aman.")
    return config


def save_config(config_data, config_path="config.conf"):
    directory = os.path.dirname(os.path.abspath(config_path)) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = config_path + ".tmp"
    with open(temp_path, 'w', encoding='utf-8') as f:
        for key, value in config_data.items():
            if isinstance(value, str):
                escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
                f.write(f'{key}="{escaped}"\n')
            else:
                f.write(f'{key}={value}\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, config_path)
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass
    log.info("CONFIG: Konfigurasi berhasil disimpan.")
