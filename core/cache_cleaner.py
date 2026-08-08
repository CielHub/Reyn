"""
Modul: cache_cleaner.py
Tanggung Jawab: Membersihkan cache package secara aman sebelum aplikasi diluncurkan.
"""
import subprocess
from core.logger import log

def clean_package_cache(pkg):
    """
    Menghapus folder cache dan code_cache secara terisolasi.
    Hanya boleh dipanggil saat aplikasi dalam keadaan mati (Initial Launch / Recovery).
    """
    log.info(f"CACHE CLEANER: Membersihkan memori untuk {pkg}...")
    try:
        subprocess.run(
            ['su', '-c', f'rm -rf /data/data/{pkg}/cache/* && rm -rf /data/data/{pkg}/code_cache/*'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL,
            close_fds=True
        )
        log.info(f"CACHE CLEANER: Selesai membersihkan {pkg}.")
    except Exception as e:
        log.error(f"CACHE CLEANER: Gagal membersihkan {pkg} - {str(e)}")
      
