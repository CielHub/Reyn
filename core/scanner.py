"""
Modul: scanner.py
Tanggung Jawab: Memindai seluruh package Roblox yang terinstal di sistem.
"""
import subprocess
import sys
from core.logger import log

def get_roblox_packages():
    log.info("SCANNER: Melakukan scan package Roblox...")
    try:
        # [PHASE 7 OPTIMIZATION]
        # Menggunakan pure Python untuk memparsing output (Hindari pipe shell yang boros memori)
        raw_output = subprocess.check_output(['pm', 'list', 'packages'], text=True)
        packages = [line.split(':')[1].strip() for line in raw_output.splitlines() if 'roblox' in line.lower() and ':' in line]
    except (subprocess.CalledProcessError, FileNotFoundError):
        packages = []

    if not packages:
        log.error("SCANNER: Tidak ada package Roblox yang terdeteksi!")
        sys.exit(1)

    log.info(f"SCANNER: Ditemukan {len(packages)} package Roblox:")
    for pkg in packages:
        log.info(f" -> {pkg}")
    
    return packages
    
