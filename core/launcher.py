"""
Modul: launcher.py
Tanggung Jawab: Membuka package Roblox dan menjalankan fungsi Smart Wait dengan aman.
"""
import subprocess
import time
import datetime
import select
from core.logger import log
from core.join_verifier import verify_join

def get_pid_quick(pkg_name):
    try:
        result = subprocess.run(['pidof', pkg_name], capture_output=True, text=True)
        pids = result.stdout.strip().split()
        return pids[0] if pids else ""
    except Exception:
        return ""

def launch_and_wait(pkg_name, intent_url, timeout_seconds):
    if not intent_url:
        log.error(f"LAUNCH FAILED: {pkg_name} tidak memiliki Intent URL.")
        return False

    log.info(f"LAUNCH: Membuka {pkg_name}...")
    
    start_time_str = datetime.datetime.now().strftime('%m-%d %H:%M:%S.000')
    
    subprocess.run(
        ['am', 'start', '-p', pkg_name, '-a', 'android.intent.action.VIEW', '-d', intent_url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    
    log.info(f"Smart Wait: Menunggu {pkg_name} terhubung ({timeout_seconds} detik)...")
    
    logcat_cmd = ['logcat', '-T', start_time_str, '-v', 'time']
    
    # --- BUG FIX IMPLEMENTATION ---
    process = subprocess.Popen(
        logcat_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding='utf-8',   # Pastikan decoding standar
        errors='replace',   # FIX: Abaikan/ganti byte cacat agar program tidak crash
        bufsize=1 
    )
    # ------------------------------
    
    keywords = ["gamejoinutil", "datamodel initialized", "successfully connected"]
    found_success = False
    start_time = time.time()
    
    try:
        while True:
            elapsed = time.time() - start_time
            if elapsed >= timeout_seconds:
                log.warning(f"FALLBACK: Logcat timeout. Menggunakan Dumb Wait untuk {pkg_name}.")
                break
                
            ready, _, _ = select.select([process.stdout], [], [], 1.0)
            
            if ready:
                line = process.stdout.readline()
                if not line:
                    break 
                
                line_lower = line.lower()
                if any(kw in line_lower for kw in keywords):
                    found_success = True
                    break
    finally:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill() 
        
    final_pid = get_pid_quick(pkg_name)
    if not final_pid:
        log.error(f"LAUNCH FAILED: {pkg_name} gagal diluncurkan (Proses mati secara prematur).")
        return False

    verified, reason = verify_join(pkg_name)
    if not verified:
        log.error(f"VERIFY FAILED: {pkg_name} -> {reason}")
        return False

    log.info(f"VERIFY: {pkg_name} -> {reason}")
    log.info(f"SUCCESS: {pkg_name} selesai diproses.")
    return True
    
