"""
Modul: logger.py
Tanggung Jawab: Menyediakan sistem logging terpusat dengan rotasi file.
"""
import os
import logging
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

LOG_FILE = os.path.join(LOG_DIR, "latest.log")

def setup_logger():
    logger = logging.getLogger("CARRERA_HUB")
    
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3)
        file_handler.setFormatter(formatter)
        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.set_name("console_handler") # DITAMBAHKAN: Memberi nama spesifik untuk identifikasi
        
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        
    return logger

log = setup_logger()

# --- FUNGSI BARU UNTUK MENGONTROL CONSOLE OUTPUT ---
def set_console_logging(enabled=True):
    """
    Mengontrol output console tanpa menyentuh file logger.
    Dipanggil saat memasuki/keluar dari Rich Live Dashboard.
    """
    for handler in log.handlers:
        if handler.get_name() == "console_handler":
            # Jika disabled, naikkan level di atas CRITICAL agar tidak ada yang dicetak
            # Jika enabled, kembalikan ke level INFO
            handler.setLevel(logging.INFO if enabled else logging.CRITICAL + 1)
            
