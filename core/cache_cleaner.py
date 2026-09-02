"""
Modul: cache_cleaner.py
Tanggung Jawab: Membersihkan cache package secara aman sebelum aplikasi diluncurkan.
"""
import subprocess
from core.logger import log

def clean_package_cache(pkg):
    """
    Menghapus folder cache (getCacheDir()) secara terisolasi -- sama persis
    dengan cakupan tombol "Clear Cache" bawaan Android di Settings > App Info.

    CATATAN: code_cache/ SENGAJA tidak disentuh. Untuk app seberat Roblox
    (compile Luau script, shader, dll), code_cache bisa menyimpan ratusan MB
    dan kemungkinan turut menyimpan state yang dibutuhkan biar sesi login
    tetap valid. Menghapusnya (seperti sebelumnya) membuat hasil clear cache
    jauh lebih destruktif dibanding tombol native -- storage tersisa cuma
    puluhan kB dan akun ikut ter-logout tiap kali dijalankan.

    Hanya boleh dipanggil saat aplikasi dalam keadaan mati (Initial Launch / Recovery).
    """
    log.info(f"CACHE CLEANER: Membersihkan cache untuk {pkg}...")
    try:
        subprocess.run(
            ['su', '-c', f'rm -rf /data/data/{pkg}/cache/*'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL,
            close_fds=True
        )
        log.info(f"CACHE CLEANER: Selesai membersihkan {pkg}.")
    except Exception as e:
        log.error(f"CACHE CLEANER: Gagal membersihkan {pkg} - {str(e)}")
      
