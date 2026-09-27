"""
Modul: menu.py
Tanggung Jawab: Menampilkan CLI interaktif dan routing eksekusi.
"""
import os
import sys
import time

from core.logger import log
from core.config import load_config, save_config
from core.scanner import get_roblox_packages
from core.agent_client import start_agent_background, get_agent_status
from core.ui import (
    console, clear_screen, reset_terminal, draw_header, show_transition,
    draw_footer, safe_prompt_ask, safe_console_input,
)
from core.tester import show_test_menu
#from core.accounts import load_accounts, save_accounts

try:
    from core.sniper import sniper_agent
except ImportError:
    pass

# BUG FIX: Import safe_prompt_ask dan safe_console_input
from rich.prompt import Prompt
from rich.table import Table

def show_auto_login_menu():
    clear_screen()
    while True:
        reset_terminal()
        draw_header("AUTO LOGIN ROBLOX")
        
        all_packages = get_roblox_packages()
        if not all_packages:
            console.print("\n[bold red][!] Tidak ada package Roblox terdeteksi.[/]")
            safe_console_input("\n[dim]Tekan Enter untuk kembali...[/]")
            return
            
        accounts = load_accounts()
        
        table = Table(box=None, padding=(0, 0), show_header=True, header_style="dim white")
        table.add_column("No", style="bold cyan", width=4, no_wrap=True)
        table.add_column("PACKAGE NAME", style="white", width=25, no_wrap=True)
        table.add_column("STATUS AKUN", style="green", width=30, no_wrap=True)
        
        for idx, pkg in enumerate(all_packages, 1):
            status = accounts.get(pkg, {}).get("username", "[dim red]Belum Dikonfigurasi[/]")
            table.add_row(f"[{idx}]", pkg, status)
            
        console.print(table)
        draw_footer("[1,2,3..] Pilih ID Package   |   [0] Kembali ke Menu")
        
        choice = safe_console_input("\n[dim]Select Package (0 untuk keluar):[/] ")
        if choice == "RESIZE_EVENT": continue
        choice = choice.strip()
        
        if choice == '0':
            break
        elif choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(all_packages):
                selected_pkg = all_packages[idx-1]
                
                console.print(f"\n[bold cyan]Konfigurasi Auto Login: {selected_pkg}[/]")
                username = safe_console_input("[white]Username:[/] ")
                if username == "RESIZE_EVENT": continue
                username = username.strip()
                
                if not username:
                    console.print("[red]Dibatalkan.[/]")
                    time.sleep(1)
                    continue
                    
                password = safe_prompt_ask("[white]Password[/]", password=True)
                if password == "RESIZE_EVENT": continue
                
                if selected_pkg not in accounts:
                    accounts[selected_pkg] = {}
                accounts[selected_pkg]["username"] = username
                accounts[selected_pkg]["password"] = password
                
                save_accounts(accounts)
                
                console.print("\n[bold green]Saved Successfully.[/]")
                again = safe_console_input("\n[dim]Configure another package? [Y/N]:[/] ")
                if again == "RESIZE_EVENT": continue
                if again.strip().upper() != 'Y':
                    break
            else:
                console.print("[bold red][!] ID tidak valid.[/]")
                time.sleep(1)


_AGENT_STATUS_LABELS = {
    "OFFLINE": "[dim]OFFLINE[/]",
    "CONNECTING": "[yellow]CONNECTING...[/]",
    "ONLINE": "[bold green]ONLINE[/]",
    "AUTH_FAILED": "[bold red]AUTH FAILED[/]",
    "RECONNECTING": "[yellow]RECONNECTING...[/]",
}


def _device_agent_status(config_data):
    """PHASE P1: teks ringkas status Device Agent LIVE (bukan cuma cek
    config terisi/tidak) untuk ditampilkan di tabel Settings -- lihat
    CARRERA_RECOMMENDATION_NEXT_STEP_SPEC.txt bag. 4."""
    device_id = str(config_data.get('DEVICE_ID', '') or '').strip()
    token = str(config_data.get('DEVICE_TOKEN', '') or '').strip()
    if not (device_id and token):
        return "[dim]BELUM DIKONFIGURASI[/]"
    state = get_agent_status()
    label = _AGENT_STATUS_LABELS.get(state["status"], f"[dim]{state['status']}[/]")
    if state["status"] == "AUTH_FAILED" and state.get("reason"):
        return f"{label} ({state['reason']})"
    return label


def show_device_agent_menu(config_data):
    """PHASE 1: input DEVICE_ID + DEVICE_TOKEN untuk konek ke Joki Control Bot.

    BOT_WS_URL tidak ditanya di sini -- sudah di-hardcode ke alamat NuraHost
    di core/config.py (default value), user tidak perlu tahu/isi ini.
    """
    while True:
        reset_terminal()
        draw_header("DEVICE AGENT (JOKI BOT)")

        device_id = str(config_data.get('DEVICE_ID', '') or '').strip()
        token = str(config_data.get('DEVICE_TOKEN', '') or '').strip()
        ws_url = str(config_data.get('BOT_WS_URL', '') or '').strip()

        console.print("[dim]Dapatkan Device ID & Token lewat command [white]/device-register[/] di Discord.[/]\n")

        table = Table(box=None, padding=(0, 0), show_header=False)
        table.add_column("No", style="bold cyan", width=5, no_wrap=True)
        table.add_column("Icon", style="white", width=3, no_wrap=True)
        table.add_column("Config", style="white", width=25, no_wrap=True)
        table.add_column("Value", style="dim white", justify="right", width=30, no_wrap=True)

        table.add_row("[1]", "🆔", "Device ID", f"[cyan]{device_id or '<Belum diisi>'}[/]")
        table.add_row("[2]", "🔑", "Device Token", f"[cyan]{'●' * 10 + ' (tersimpan)' if token else '<Belum diisi>'}[/]")
        table.add_row("", "📡", "Status Bot", _device_agent_status(config_data))
        table.add_row("", "🌐", "Server (otomatis)", f"[dim]{ws_url}[/]")
        table.add_row("[3]", "🗑", "Hapus Konfigurasi", ">")
        table.add_row("[4]", "↩", "Kembali", ">")

        console.print(table)
        draw_footer("ESC / 4  Back  |  Perubahan langsung dipakai (auto-reconnect)")

        choice = safe_prompt_ask("\n[dim]Pilih (1-4)[/]", choices=["1", "2", "3", "4"])
        if choice == "RESIZE_EVENT":
            continue

        if choice == '1':
            new_id = safe_console_input(f"\n[dim]Masukkan Device ID [{device_id or '-'}]:[/] ")
            if new_id != "RESIZE_EVENT" and new_id.strip():
                config_data['DEVICE_ID'] = new_id.strip()
                save_config(config_data, "config.conf")
                start_agent_background(
                    config_data.get('DEVICE_ID', ''),
                    config_data.get('DEVICE_TOKEN', ''),
                    config_data.get('BOT_WS_URL', ''),
                )
                console.print("[bold green][✓] Device ID disimpan, agent reconnect...[/]")
                time.sleep(1)
        elif choice == '2':
            new_token = safe_console_input("\n[dim]Masukkan Device Token (dari Discord):[/] ")
            if new_token != "RESIZE_EVENT" and new_token.strip():
                config_data['DEVICE_TOKEN'] = new_token.strip()
                save_config(config_data, "config.conf")
                start_agent_background(
                    config_data.get('DEVICE_ID', ''),
                    config_data.get('DEVICE_TOKEN', ''),
                    config_data.get('BOT_WS_URL', ''),
                )
                console.print("[bold green][✓] Device Token disimpan, agent reconnect...[/]")
                time.sleep(1)
        elif choice == '3':
            confirm = safe_prompt_ask("\n[bold yellow]Yakin hapus Device ID & Token? (Y/N)[/]", choices=["Y", "N"])
            if confirm == 'Y':
                config_data['DEVICE_ID'] = ''
                config_data['DEVICE_TOKEN'] = ''
                save_config(config_data, "config.conf")
                start_agent_background(
                    config_data.get('DEVICE_ID', ''),
                    config_data.get('DEVICE_TOKEN', ''),
                    config_data.get('BOT_WS_URL', ''),
                )
                console.print("[bold yellow][✓] Konfigurasi device dihapus, agent dihentikan.[/]")
                time.sleep(1)
        elif choice == '4':
            break


def show_settings():
    """Settings sekarang hanya berisi Device Agent (Joki Bot) -- satu-satunya
    konfigurasi yang masih dipakai jalur Discord/Headless. Seluruh
    konfigurasi standalone Auto Rejoin (Global Target, Link/Map per
    Package, Timeout/Delay/Retry/Cooldown, Auto Clear Cache, Scheduled
    Restart) sudah dihapus bersama engine-nya -- lihat
    refactor: remove standalone auto rejoin engine.
    """
    clear_screen()
    config_data = load_config("config.conf")

    while True:
        reset_terminal()
        draw_header("SETTINGS")

        table = Table(box=None, padding=(0, 0), show_header=False)
        table.add_column("No", style="bold cyan", width=5, no_wrap=True)
        table.add_column("Icon", style="white", width=3, no_wrap=True)
        table.add_column("Config", style="white", width=25, no_wrap=True)
        table.add_column("Value", style="dim white", justify="right", width=23, no_wrap=True)

        table.add_row("[1]", "🔌", "Device Agent (Joki Bot)", _device_agent_status(config_data))
        table.add_row("[2]", "↩", "Kembali", ">")

        console.print(table)
        draw_footer("ESC / 2  Back to Menu")

        choice = safe_prompt_ask("\n[dim]Pilih (1-2)[/]", choices=["1", "2"])
        if choice == "RESIZE_EVENT": continue

        if choice == '1':
            show_device_agent_menu(config_data)
        elif choice == '2':
            break

def run_updater():
    from core.version import VERSION
    from core.providers.git import GitProvider
    from core.updater import AutoUpdater
    
    reset_terminal()
    draw_header("AUTO UPDATER")
    
    console.print("[dim cyan]Mengecek versi terbaru di server...[/]")
    
    provider = GitProvider()
    updater = AutoUpdater(provider)
    
    info = updater.check_for_updates(VERSION)
    
    if not info.has_update:
        console.print("\n[bold green]✔ Lu udah pake versi terbaru.[/]")
        if info.reason:
            console.print(f"[dim white]Detail: {info.reason}[/]")
        draw_footer("Enter  Kembali ke Menu")
        safe_console_input("\n[dim]Tekan Enter...[/]")
        return
        
    console.print("\n[bold green]🌟 UPDATE TERSEDIA 🌟[/]")
    
    table = Table(box=None, padding=(0, 2), show_header=False)
    table.add_column("Key", style="dim white")
    table.add_column("Value", style="bold")
    table.add_row("Versi Saat Ini", f"[red]{info.current_version}[/]")
    table.add_row("Versi Terbaru", f"[green]{info.latest_version}[/]")
    console.print(table)
    
    choice = safe_prompt_ask("\n[white]Mau update sekarang?[/]", choices=["Y", "N"])
    if choice == "RESIZE_EVENT": return # Jika terminal di-resize, kembalikan ke menu utama untuk keamanan
    
    if choice.upper() == 'Y':
        console.print("\n[dim cyan]Downloading update secara silent...[/]")
        
        result = updater.execute_update(info.current_version, info.latest_version)
        
        if result.success:
            console.print("\n[bold green]✔ Update berhasil diinstal![/]")
            console.print("[bold yellow]Restarting sistem...[/]")
            time.sleep(1.5)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        else:
            console.print("\n[bold red]✘ Update dibatalkan / gagal![/]")
            console.print(f"[dim white]Kode Error : {result.error_code.name}[/]")
            console.print(f"[dim white]Alasan     : {result.reason}[/]")
            draw_footer("Enter  Kembali ke Menu")
            safe_console_input("\n[dim]Tekan Enter...[/]")
    else:
        console.print("\n[dim yellow]Update dibatalkan oleh user.[/]")
        time.sleep(1)

def show_main_menu():
    clear_screen()
    while True:
        reset_terminal()
        draw_header("MENU UTAMA")
        
        table = Table(box=None, padding=(0, 0), show_header=False)
        table.add_column("No", style="bold cyan", width=5, no_wrap=True)
        table.add_column("Icon", style="white", width=3, no_wrap=True)
        table.add_column("Menu", style="white", width=45, no_wrap=True)
        table.add_column("Chevron", style="dim white", justify="right", width=3, no_wrap=True)
        
        table.add_row("[1]", "⚙", "Settings", ">")
        table.add_row("[2]", "🔑", "Auto Login Roblox", ">")
        table.add_row("[3]", "🧪", "Test (Unit Testing)", ">")
        table.add_row("[4]", "📝", "Logs (Lihat Log)", ">")
        table.add_row("[5]", "ⓘ", "About", ">")
        table.add_row("[6]", "🔄", "Update Program", ">")
        table.add_row("[bold red][7][/]", "[red]⏻[/]", "[red]Exit[/]", "[red]>[/]")
        
        console.print(table)
        draw_footer("CTRL+C  Dashboard    CTRL+Z  Exit")
        
        choice = safe_prompt_ask("\n[dim]Pilih menu (1-7)[/]", choices=["1", "2", "3", "4", "5", "6", "7"])
        
        # BUG FIX: Deteksi event resize, ulangi loop untuk render ulang layar
        if choice == "RESIZE_EVENT": 
            continue

        # A menu selection is a UI boundary: remove the previous menu before
        # entering the selected screen. The destination menu may clear again
        # on entry; that is intentional and keeps every screen self-contained.
        clear_screen()
        
        if choice == '1':
            show_transition("Loading Menu...")
            show_settings()
        elif choice == '2':
            show_transition("Loading Auto Login...")
            show_auto_login_menu()
        elif choice == '3':
            show_test_menu()
            show_transition("Loading Menu...")
        elif choice == '4':
            show_transition("Fetching Logs...")
            reset_terminal()
            draw_header("LOGS VIEWER")
            
            log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "latest.log")
            if os.path.exists(log_path):
                console.print("[dim]Menampilkan 20 baris terakhir...[/]")
                console.print("")
                os.system(f"tail -n 20 {log_path}")
            else:
                console.print("[dim]File log belum tersedia.[/]")
            
            draw_footer("Enter  Back to Menu")
            safe_console_input("\n[dim]Tekan Enter...[/]")
        elif choice == '5':
            show_transition("Opening About...")
            reset_terminal()
            draw_header("ABOUT")
            
            table = Table(box=None, padding=(0, 0), show_header=False)
            table.add_column("Key", style="dim white", width=20)
            table.add_column("Value", style="bold white", width=35)
            
            table.add_row("Aplikasi", "CARRERA-HUB")
            table.add_row("Versi", "[cyan]Python Modular Edition[/]")
            table.add_row("Status", "[green]Stabil & Termux Root Ready[/]")
            table.add_row("Developer", "[magenta]Carrera-Hub Team[/]")
            
            console.print(table)
            draw_footer("Enter  Back to Menu")
            safe_console_input("\n[dim]Tekan Enter...[/]")
        elif choice == '6':
            show_transition("Checking Server...")
            run_updater()
        elif choice == '7':
            show_transition("Shutting Down...")
            try:
                sniper_agent.stop()
            except Exception:
                pass
            reset_terminal()
            sys.exit(0)
