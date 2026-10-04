import os
import subprocess
import time

def main():
    print("=== CARRERA LOG CATCHER ===")
    
    # 1. Clear memory buffer logcat Android biar bersih
    print("[1] Membersihkan logcat Android lama...")
    subprocess.run(['su', '-c', 'logcat -c'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    print("\n[2] STANDBY. Silakan buka clone Roblox lu, masuk ke dalam map game...")
    print("[3] Lakukan Testing Kick (Error 267) menggunakan script/admin command lu.")
    
    # Script berhenti di sini sampai lu tekan Enter
    input("\n>>> TEKAN ENTER DI SINI HANYA SETELAH PESAN KICK MUNCUL DI LAYAR ROBLOX <<<")
    
    print("\n[4] Menyimpan log, mohon tunggu sebentar...")
    os.makedirs("logs", exist_ok=True)
    log_path = "logs/kick_evidence.txt"
    
    # Dump the log buffer into a file
    os.system(f"su -c 'logcat -d -v brief > {log_path}'")
    
    print(f"\n[V] SELESAI! Bukti berhasil disimpan ke: {log_path}")
    print("Buka file itu pakai text editor Termux, cari kata 'kicked' atau 'disconnect' atau 'error 267', dan copy-paste baris aslinya ke gw!")

if __name__ == "__main__":
    main()
  
