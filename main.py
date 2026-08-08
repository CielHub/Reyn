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
from core.menu import show_main_menu

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
    ensure_root()
    
    log.info("STARTUP: Menginisialisasi CARRERA-HUB Menu Utama...")
    
    # Langsung panggil Menu Interaktif
    show_main_menu()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("") # Jarak enter
        log.info("SHUTDOWN: Script dihentikan oleh user (CTRL+C).")
        sys.exit(0)
        
