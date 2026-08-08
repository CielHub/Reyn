"""
Modul: main.py
Tanggung Jawab: Entry point utama, menangani Auto Root, dan Orkestrasi Modul.
"""
import os
import sys
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(SCRIPT_DIR)

from core.logger import log
from core.ui import clear_screen
from core.menu import show_main_menu
from core.config import load_config
from core.device_agent import start_device_agent, stop_device_agent

def ensure_root():
    """Memastikan script berjalan di bawah environment Root."""
    try:
        uid = int(subprocess.check_output(['id', '-u']).decode('utf-8').strip())
    except Exception:
        uid = os.geteuid()

    if uid != 0:
        print("[*] Script ini membutuhkan akses Root untuk bekerja.")
        print("[*] Meminta izin Root ke sistem...")
        
        python_bin = sys.executable
        cmd = f"su -c \"{python_bin} '{os.path.join(SCRIPT_DIR, 'main.py')}'\""
        exit_code = subprocess.call(cmd, shell=True)
        
        if exit_code != 0:
            print("[!] Gagal mendapatkan akses Root. Pastikan HP sudah di-root.")
            sys.exit(1)
            
        sys.exit(0)

def main():
    """Fungsi Orkestrasi Utama."""
    os.chdir(SCRIPT_DIR)

    # Always start from a clean terminal before doing any startup work.
    # The root-relaunched child process will clear again, so no parent output
    # can leak into the interactive UI.
    clear_screen()
    ensure_root()

    # Clear once more after privilege escalation so the actual interactive
    # process starts with a completely clean screen.
    clear_screen()
    
    log.info("STARTUP: Menginisialisasi CARRERA-HUB Menu Utama...")

    # Phase D1: device control-plane agent is optional and disabled by default.
    # It never owns the Roblox/recovery lifecycle.
    config_data = load_config("config.conf")
    start_device_agent(config_data)

    # Langsung panggil Menu Interaktif
    show_main_menu()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("") # Jarak enter
        stop_device_agent()
        log.info("SHUTDOWN: Script dihentikan oleh user (CTRL+C).")
        sys.exit(0)
        
