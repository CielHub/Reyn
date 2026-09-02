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
from core.agent_client import start_agent_background

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

    # PHASE 1: nyalakan agent Joki Control Bot di background thread, kalau
    # config-nya sudah diisi (opsional -- lihat agent_client.py). Dibungkus
    # try/except supaya kalau ada error tak terduga di sini, startup menu
    # utama TETAP JALAN seperti biasa (fitur ini tidak boleh jadi titik gagal).
    try:
        cfg = load_config()
        start_agent_background(
            device_id=cfg.get("DEVICE_ID", ""),
            token=cfg.get("DEVICE_TOKEN", ""),
            ws_url=cfg.get("BOT_WS_URL", ""),
        )
    except Exception:
        log.error("STARTUP: Gagal menyalakan agent Joki Control Bot (non-fatal).", exc_info=True)

    # Langsung panggil Menu Interaktif
    show_main_menu()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("") # Jarak enter
        log.info("SHUTDOWN: Script dihentikan oleh user (CTRL+C).")
        sys.exit(0)

