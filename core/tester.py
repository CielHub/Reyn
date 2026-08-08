"""
Modul: tester.py
Tanggung Jawab: Menyediakan framework pengujian (Unit Test) untuk setiap modul secara terisolasi.
"""
import os
import subprocess
from core.logger import log
from core.config import load_config
from core.deeplink import get_intent_url
from core.scanner import get_roblox_packages
from core.launcher import launch_and_wait
from core.monitor import get_pid

from core.ui import console, reset_terminal, draw_header, show_transition, draw_footer, LAYOUT_WIDTH
from rich.prompt import Prompt
from rich.table import Table

def pause():
    draw_footer("Enter  Kembali ke Menu Test")
    console.input("\n[dim]Tekan Enter...[/]")

def test_root():
    console.print("\n[dim]--- TEST ROOT ---[/]")
    try:
        uid = int(subprocess.check_output(['id', '-u']).decode('utf-8').strip())
    except Exception:
        import os
        uid = os.geteuid()
        
    console.print(f"\n[white]Current UID:[/] [cyan]{uid}[/]")
    if uid == 0:
        console.print("[bold green][OK] Sistem berjalan sebagai Root.[/]")
    else:
        console.print("[bold red][FAIL] Sistem TIDAK berjalan sebagai Root.[/]")
    pause()

def test_config():
    console.print("\n[dim]--- TEST CONFIG ---[/]")
    config_data = load_config("config.conf")
    
    table = Table(box=None, padding=(0, 0), show_header=False, width=LAYOUT_WIDTH)
    table.add_column("Key", style="white", width=25)
    table.add_column("Value", style="cyan", width=35)
    
    for key, value in config_data.items():
        table.add_row(key, str(value))
    
    console.print("\n")
    console.print(table)
    console.print("\n[bold green][OK] Config berhasil dibaca.[/]")
    pause()

def test_logger():
    console.print("\n[dim]--- TEST LOGGER ---[/]")
    console.print("\n[white]Menulis pesan test ke dalam log...[/]")
    log.info("TESTING: Ini adalah pesan uji coba dari modul tester.py")
    log.warning("TESTING: Ini adalah pesan warning.")
    log.error("TESTING: Ini adalah pesan error.")
    console.print("\n[bold green][OK] Silakan cek file logs/latest.log untuk melihat hasilnya.[/]")
    pause()

def test_scanner():
    console.print("\n[dim]--- TEST SCANNER ---[/]")
    packages = get_roblox_packages()
    if packages:
        console.print("\n[bold green][OK] Scanner berfungsi dan menemukan package.[/]")
    pause()

def test_deeplink():
    console.print("\n[dim]--- TEST DEEP LINK ---[/]")
    config_data = load_config("config.conf")
    link = config_data.get("PRIVATE_SERVER_LINK", "")
    console.print(f"\n[white]Link Asli:[/] [cyan]{link}[/]")
    intent_url = get_intent_url(link)
    console.print(f"[white]Intent URL:[/] [cyan]{intent_url}[/]")
    if intent_url:
        console.print("\n[bold green][OK] Deep Link konversi berhasil.[/]")
    pause()

def test_launcher():
    console.print("\n[dim]--- TEST LAUNCHER ---[/]")
    packages = get_roblox_packages()
    if not packages:
        console.print("\n[bold red][!] Tidak ada package untuk dites.[/]")
        pause()
        return
        
    pkg = packages[0]
    console.print(f"\n[white]Akan melakukan test launch pada:[/] [cyan]{pkg}[/]")
    config_data = load_config("config.conf")
    intent_url = get_intent_url(config_data["PRIVATE_SERVER_LINK"])
    
    console.print("\n[dim]Mengeksekusi Launch & Smart Wait...[/]")
    success = launch_and_wait(pkg, intent_url, config_data["TIMEOUT_SECONDS"])
    
    if success:
        console.print("\n[bold green][OK] Launcher mengembalikan nilai True (Sukses).[/]")
    else:
        console.print("\n[bold red][FAIL] Launcher mengembalikan nilai False (Gagal).[/]")
    pause()

def test_monitor():
    console.print("\n[dim]--- TEST MONITOR ---[/]")
    packages = get_roblox_packages()
    if not packages:
        console.print("\n[bold red][!] Tidak ada package untuk dites.[/]")
        pause()
        return
        
    console.print("\n[white]Mencari PID aktif untuk package yang terdeteksi:[/]")
    for pkg in packages:
        pid = get_pid(pkg)
        if pid:
            console.print(f"[bold green][OK] {pkg} SEDANG BERJALAN (PID: {pid})[/]")
        else:
            console.print(f"[bold red][INFO] {pkg} SEDANG MATI[/]")
    pause()

def show_test_menu():
    show_transition("Initializing Test Environment...")
    while True:
        reset_terminal()
        draw_header("UNIT TESTING")
        
        table = Table(box=None, padding=(0, 0), show_header=False, width=LAYOUT_WIDTH)
        table.add_column("No", style="bold cyan", width=4)
        table.add_column("Icon", style="white", width=3)
        table.add_column("Test", style="white", width=50)
        table.add_column("Chevron", style="dim white", justify="right", width=3)
        
        table.add_row("[1]", "🔑", "Test Root Access", ">")
        table.add_row("[2]", "📄", "Test Config Loader", ">")
        table.add_row("[3]", "🐛", "Test Logger System", ">")
        table.add_row("[4]", "🔎", "Test Package Scanner", ">")
        table.add_row("[5]", "🔗", "Test Deep Link Converter", ">")
        table.add_row("[6]", "🚀", "Test Launcher & Smart Wait", ">")
        table.add_row("[7]", "📊", "Test Monitor (PID Check)", ">")
        table.add_row("[8]", "↩", "Kembali", ">")
        
        console.print(table)
        draw_footer("ESC / 8  Back to Menu")
        
        choice = Prompt.ask("\n[dim]Pilih test (1-8)[/]", choices=["1", "2", "3", "4", "5", "6", "7", "8"])
        
        if choice == '8': 
            break
            
        show_transition(f"Preparing Test {choice}...")
        reset_terminal()
        draw_header(f"TEST RUNNER: {choice}")
        
        if choice == '1': test_root()
        elif choice == '2': test_config()
        elif choice == '3': test_logger()
        elif choice == '4': test_scanner()
        elif choice == '5': test_deeplink()
        elif choice == '6': test_launcher()
        elif choice == '7': test_monitor()
            
