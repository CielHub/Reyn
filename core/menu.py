"""
Modul: menu.py
Tanggung Jawab: Menampilkan CLI interaktif dan routing eksekusi.
"""
import os
import sys
import time
import logging
import shutil

from core.logger import log
from core.config import load_config, save_config
from core.deeplink import get_intent_url
from core.target_resolver import TargetResolver
from core.state_machine import set_state
from core.states import PackageState
from core.scanner import get_roblox_packages
from core.launcher import launch_and_wait
from core.monitor import (
    start_monitoring,
    draw_dashboard,
    create_dashboard_live,
    refresh_dashboard_live,
    get_dashboard_terminal_size,
)
from core.ui import (
    console, clear_screen, reset_terminal, full_terminal_reset, draw_header, show_transition,
    draw_footer, safe_prompt_ask, safe_console_input,
    start_dashboard_resize_watcher, stop_dashboard_resize_watcher,
)
from core.tester import show_test_menu
from core.cache_cleaner import clean_package_cache
from core.discord_controller import mask_secret, test_discord_token, test_controller_connection, start_discord_controller, stop_discord_controller
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

def resolve_join_intent(config_data, pkg):
    """Backward-compatible wrapper around the centralized TargetResolver."""
    target = TargetResolver(config_data).resolve(pkg)
    if target is None:
        return None, "NONE"
    label = f"{target.target_type} ({target.scope})"
    return target.intent_url, label


def show_map_manager(config_data):
    clear_screen()
    all_packages = get_roblox_packages()
    if not all_packages:
        console.print("\n[bold red][!] Tidak ada package Roblox terdeteksi.[/]")
        safe_console_input("\n[dim]Tekan Enter untuk kembali...[/]")
        return

    while True:
        reset_terminal()
        draw_header("MAP ID PER PACKAGE")

        table = Table(box=None, padding=(0, 0), show_header=True, header_style="dim white")
        table.add_column("ID", style="bold cyan", width=4, no_wrap=True)
        table.add_column("PACKAGE NAME", style="white", width=20, no_wrap=True)
        table.add_column("MAP / PLACE ID", style="cyan", width=20, no_wrap=True)

        for idx, pkg in enumerate(all_packages, 1):
            place_id = config_data.get(f"PKG_{pkg}_PLACE_ID", "")
            display = place_id if place_id else "[dim white]<Global Map ID>[/]"
            table.add_row(f"[{idx}]", pkg, display)

        console.print(table)
        draw_footer("[1,2,3..] Pilih ID untuk edit   |   [0] Kembali")

        choice = safe_console_input("\n[dim]Pilih ID (0 untuk keluar):[/] ")
        if choice == "RESIZE_EVENT":
            continue
        choice = choice.strip()

        if choice == '0':
            break
        if not choice.isdigit() or not (1 <= int(choice) <= len(all_packages)):
            console.print("[bold red][!] ID tidak valid.[/]")
            time.sleep(1)
            continue

        selected_pkg = all_packages[int(choice) - 1]
        key = f"PKG_{selected_pkg}_PLACE_ID"
        current = config_data.get(key, "")
        console.print(f"\n[bold cyan]Map ID untuk {selected_pkg}[/]")
        console.print("[dim]Kosongkan lalu Enter untuk menggunakan Global Map ID.[/]")
        new_id = safe_console_input(f"[dim]Place ID baru [{current or 'Global'}]:[/] ")
        if new_id == "RESIZE_EVENT":
            continue

        new_id = new_id.strip()
        if new_id and not new_id.isdigit():
            console.print("[bold red][!] Place ID harus berupa angka.[/]")
            time.sleep(1)
            continue

        if new_id:
            # Satu package hanya boleh punya satu target aktif.
            # Jika Map ID diisi, Private Server Link package otomatis dikosongkan.
            config_data[key] = new_id
            config_data.pop(f"PKG_{selected_pkg}", None)
        else:
            config_data.pop(key, None)
        save_config(config_data, "config.conf")


def show_link_manager(config_data):
    clear_screen()
    all_packages = get_roblox_packages()
    if not all_packages:
        console.print("\n[bold red][!] Tidak ada package Roblox terdeteksi.[/]")
        safe_console_input("\n[dim]Tekan Enter untuk kembali...[/]")
        return

    while True:
        reset_terminal()
        draw_header("LINK PER PACKAGE")
        
        table = Table(box=None, padding=(0, 0), show_header=True, header_style="dim white")
        table.add_column("ID", style="bold cyan", width=4, no_wrap=True)
        table.add_column("PACKAGE NAME", style="white", width=20, no_wrap=True)
        table.add_column("DEEP LINK", style="cyan", width=30, no_wrap=True, overflow="ellipsis")
        
        for idx, pkg in enumerate(all_packages, 1):
            pkg_key = f"PKG_{pkg}"
            link = config_data.get(pkg_key, "")
            display_link = link if link else "[dim white]<Global Link>[/]"
            table.add_row(f"[{idx}]", pkg, display_link)
            
        console.print(table)
        draw_footer("[1,2,3..] Pilih ID untuk edit   |   [0] Kembali")
        
        choice = safe_console_input("\n[dim]Pilih ID (0 untuk keluar):[/] ")
        if choice == "RESIZE_EVENT": continue
        choice = choice.strip()
        
        if choice == '0':
            break
        elif choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(all_packages):
                selected_pkg = all_packages[idx-1]
                pkg_key = f"PKG_{selected_pkg}"
                console.print(f"\n[dim]Kosongkan lalu Enter untuk menggunakan Global Link.[/]")
                
                new_link = safe_console_input(f"[dim]Link baru untuk [white]{selected_pkg}[/]:[/] ")
                if new_link == "RESIZE_EVENT": continue
                
                if new_link.strip():
                    # Satu package hanya boleh punya satu target aktif.
                    # Jika Private Server diisi, Map ID package otomatis dikosongkan.
                    config_data[pkg_key] = new_link.strip()
                    config_data.pop(f"{pkg_key}_PLACE_ID", None)
                else:
                    config_data.pop(pkg_key, None)

                save_config(config_data, "config.conf")
            else:
                console.print("[bold red][!] ID tidak valid.[/]")
                time.sleep(1)

def run_auto_rejoiner():
    clear_screen()
    draw_header("INITIALIZING AUTO REJOINER")
    
    config_data = load_config("config.conf")
    timeout_seconds = config_data.get("TIMEOUT_SECONDS", 45)
    delay_seconds = config_data.get("DELAY_SECONDS", 3)
    max_retries = config_data.get("MAX_RETRIES", 3)
    cooldown_secs = config_data.get("COOLDOWN_SECONDS", 300)
    
    all_packages = get_roblox_packages()
    if not all_packages:
        console.print("\n[bold red][!] Tidak ada package Roblox terdeteksi.[/]")
        safe_console_input("\n[dim]Tekan Enter untuk kembali...[/]")
        return
        
    console.print("\n[bold cyan]Detected Packages:[/]")
    for idx, pkg in enumerate(all_packages, 1):
        console.print(f"[{idx}] {pkg}")
    console.print("\n[bold cyan][A][/] All Packages")
    
    packages = []
    while True:
        choice = safe_console_input("\n[dim]Input (A / 1,2,3...):[/] ")
        if choice == "RESIZE_EVENT":
            reset_terminal()
            draw_header("INITIALIZING AUTO REJOINER")
            console.print("\n[bold cyan]Detected Packages:[/]")
            for idx, pkg in enumerate(all_packages, 1):
                console.print(f"[{idx}] {pkg}")
            console.print("\n[bold cyan][A][/] All Packages")
            continue
            
        choice = choice.strip().upper()
        
        if choice == '':
            console.print("[bold red][!] Input tidak boleh kosong. Silakan coba lagi.[/]")
            continue
        elif choice == 'A':
            packages = all_packages
            break
        else:
            parts = choice.split(',')
            new_active = []
            invalid_nums = []
            seen = set()
            for p in parts:
                p = p.strip()
                if p.isdigit():
                    idx = int(p)
                    if 1 <= idx <= len(all_packages):
                        pkg_name = all_packages[idx-1]
                        if pkg_name not in seen:
                            seen.add(pkg_name)
                            new_active.append(pkg_name)
                    else:
                        invalid_nums.append(p)
                else:
                    invalid_nums.append(p)
                    
            if invalid_nums:
                console.print(f"[bold red][!] Input tidak valid/tidak ditemukan: {', '.join(invalid_nums)}[/]")
                continue
            else:
                packages = new_active
                break

    intent_dict = {}

    for pkg in packages:
        try:
            intent, target_type = resolve_join_intent(config_data, pkg)
            intent_dict[pkg] = intent
            if intent:
                log.info(f"TARGET: {pkg} -> {target_type}")
            else:
                console.print(f"[bold yellow][!] PERINGATAN: Tidak ada target join untuk {pkg} (PS/Map ID). Akan terhenti di Home.[/]")
        except ValueError as exc:
            console.print(f"[bold red][!] Target {pkg} tidak valid: {exc}[/]")
            intent_dict[pkg] = None
    
    current_time = time.time()
    stats = {}
    for pkg in packages:
        stats[pkg] = {
            'pid': '-', 'status': 'OFFLINE', 'uptime_start': 0,
            'launch_count': 0, 'recovery_count': 0, 'crash_count': 0,
            'consecutive_crashes': 0, 'last_recovery_time': current_time, 'cooldown_until': 0, 'state': 'OFFLINE'
        }
    
    for handler in log.handlers[:]:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            log.removeHandler(handler)
            
    # Full terminal reset happens exactly once, immediately before the first dashboard frame.
    # After this point the dashboard lifecycle never performs another terminal reset.
    full_terminal_reset()
    
    dashboard_size = get_dashboard_terminal_size()
    resize_handler = start_dashboard_resize_watcher()
    try:
        with create_dashboard_live(
            draw_dashboard(stats, time.time(), len(packages), include_header=True),
            screen=True,
        ) as live:
            for pkg in packages:
                clean_package_cache(pkg)

                set_state(stats, pkg, PackageState.LAUNCHING)
                stats[pkg]['launch_count'] += 1
                dashboard_size, _ = refresh_dashboard_live(
                    live,
                    draw_dashboard(stats, time.time(), len(packages), include_header=True),
                    dashboard_size,
                )

                intent_url = intent_dict.get(pkg)
                if not intent_url:
                    set_state(stats, pkg, PackageState.NO_TARGET)
                    dashboard_size, _ = refresh_dashboard_live(
                        live,
                        draw_dashboard(stats, time.time(), len(packages), include_header=True),
                        dashboard_size,
                    )
                    log.error(f"LAUNCH SKIPPED: {pkg} tidak memiliki target join yang valid.")
                    continue

                success = launch_and_wait(pkg, intent_url, timeout_seconds)

                if not success:
                    try:
                        from core.autologin import run as run_autologin
                        stats[pkg]['status'] = 'LOGIN'
                        dashboard_size, _ = refresh_dashboard_live(
                            live,
                            draw_dashboard(stats, time.time(), len(packages), include_header=True),
                            dashboard_size,
                        )

                        login_status = run_autologin(pkg)

                        if login_status in ["SUCCESS", "ALREADY_LOGGED_IN"]:
                            stats[pkg]['status'] = 'LOADING'
                            dashboard_size, _ = refresh_dashboard_live(
                                live,
                                draw_dashboard(stats, time.time(), len(packages), include_header=True),
                                dashboard_size,
                            )
                            success = launch_and_wait(pkg, intent_url, timeout_seconds)
                        elif login_status == "CAPTCHA":
                            stats[pkg]['status'] = 'CAPTCHA'
                        else:
                            stats[pkg]['status'] = 'LOGIN FAILED'
                    except ImportError:
                        pass

                if success:
                    set_state(stats, pkg, PackageState.ONLINE)
                    stats[pkg]['uptime_start'] = time.time()
                else:
                    if stats[pkg]['status'] not in ['LOGIN FAILED', 'CAPTCHA']:
                        set_state(stats, pkg, PackageState.FAILED)

                time.sleep(delay_seconds)
                dashboard_size, _ = refresh_dashboard_live(
                    live,
                    draw_dashboard(stats, time.time(), len(packages), include_header=True),
                    dashboard_size,
                )
    finally:
        stop_dashboard_resize_watcher(resize_handler)

    try:
        sniper_agent.start()
    except NameError:
        pass
        
    start_monitoring(packages, intent_dict, timeout_seconds, max_retries, cooldown_secs, stats, config_data)

def show_discord_controller_menu():
    """Integrated D3R1 Discord Controller settings.

    D3R1 owns configuration and connection tests. The actual /status bot runtime
    is optional and starts only from main.py when explicitly enabled.
    """
    clear_screen()
    config_data = load_config("config.conf")

    while True:
        reset_terminal()
        draw_header("DISCORD CONTROLLER")

        enabled = str(config_data.get("DISCORD_BOT_ENABLED", 0)).lower() in {"1", "true", "yes", "on"}
        token = str(config_data.get("DISCORD_BOT_TOKEN", ""))
        guild_id = str(config_data.get("DISCORD_GUILD_ID", ""))
        controller_enabled = str(config_data.get("CONTROLLER_ENABLED", 0)).lower() in {"1", "true", "yes", "on"}
        controller_url = str(config_data.get("DISCORD_CONTROLLER_URL", "http://127.0.0.1:8765"))

        table = Table(box=None, padding=(0, 0), show_header=False)
        table.add_column("No", style="bold cyan", width=5, no_wrap=True)
        table.add_column("Icon", style="white", width=3, no_wrap=True)
        table.add_column("Setting", style="white", width=28, no_wrap=True)
        table.add_column("Value", style="dim white", justify="right", width=22, no_wrap=True)
        table.add_row("[0]", "●", "Discord Bot", "[green]ENABLED[/]" if enabled else "[dim]DISABLED[/]")
        table.add_row("[1]", "🔑", "Bot Token", f"[cyan]{mask_secret(token)}[/]")
        table.add_row("[2]", "🏠", "Discord Server / Guild ID", f"[cyan]{guild_id or '<Not Set>'}[/]")
        table.add_row("[3]", "🌐", "Controller", f"[green]LOCAL[/] {controller_url}")
        table.add_row("[4]", "🔌", "Controller Hub", "[green]ENABLED[/]" if controller_enabled else "[yellow]DISABLED[/]")
        table.add_row("[5]", "🧪", "Test Discord Token", ">")
        table.add_row("[6]", "🧪", "Test Controller", ">")
        table.add_row("[7]", "💾", "Save / Apply", ">")
        table.add_row("[8]", "⏻", "Enable / Disable Bot", ">")
        table.add_row("[9]", "↩", "Kembali", ">")
        console.print(table)
        draw_footer("D3R1  Local Controller  |  Token selalu dimasking")

        choice = safe_prompt_ask("\n[dim]Pilih (1-9)[/]", choices=[str(i) for i in range(1, 10)])
        if choice == "RESIZE_EVENT":
            continue

        if choice == '1':
            new_token = safe_console_input("\n[dim]Masukkan Discord Bot Token (kosongkan untuk hapus):[/] ")
            if new_token != "RESIZE_EVENT":
                config_data["DISCORD_BOT_TOKEN"] = new_token.strip()
                save_config(config_data, "config.conf")
        elif choice == '2':
            new_guild = safe_console_input("\n[dim]Masukkan Discord Server/Guild ID (kosongkan untuk hapus):[/] ")
            if new_guild != "RESIZE_EVENT":
                new_guild = new_guild.strip()
                if new_guild and not new_guild.isdigit():
                    console.print("[bold red][!] Guild ID harus berupa angka.[/]")
                    time.sleep(1)
                else:
                    config_data["DISCORD_GUILD_ID"] = new_guild
                    save_config(config_data, "config.conf")
        elif choice == '3':
            console.print("\n[dim]D3R1 menggunakan Controller lokal. URL default adalah http://127.0.0.1:8765.[/]")
            new_url = safe_console_input(f"[dim]Controller URL [{controller_url}]:[/] ")
            if new_url != "RESIZE_EVENT":
                new_url = new_url.strip()
                if new_url:
                    config_data["DISCORD_CONTROLLER_URL"] = new_url
                    save_config(config_data, "config.conf")
        elif choice == '4':
            config_data["CONTROLLER_ENABLED"] = 0 if controller_enabled else 1
            save_config(config_data, "config.conf")
            config_data = load_config("config.conf")
        elif choice == '5':
            ok, message = test_discord_token(config_data.get("DISCORD_BOT_TOKEN", ""), config_data.get("DISCORD_STATUS_TIMEOUT", 8))
            console.print(f"\n{'[bold green]✔' if ok else '[bold red]✘'} {message}[/]")
            safe_console_input("\n[dim]Tekan Enter...[/]")
        elif choice == '6':
            ok, message = test_controller_connection(config_data)
            console.print(f"\n{'[bold green]✔' if ok else '[bold red]✘'} {message}[/]")
            safe_console_input("\n[dim]Tekan Enter...[/]")
        elif choice == '7':
            # Keep the Discord status credential synchronized with the local Controller credential.
            controller_token = str(config_data.get("CONTROLLER_STATUS_TOKEN", "")).strip()
            if controller_token:
                config_data["DISCORD_CONTROLLER_STATUS_TOKEN"] = controller_token
            save_config(config_data, "config.conf")
            console.print("\n[bold green]✔ Konfigurasi Discord Controller tersimpan.[/]")
            time.sleep(0.8)
        elif choice == '8':
            if not str(config_data.get("DISCORD_BOT_TOKEN", "")).strip():
                console.print("\n[bold red][!] Bot Token belum diisi.[/]")
                time.sleep(1)
                continue
            config_data["DISCORD_BOT_ENABLED"] = 0 if enabled else 1
            save_config(config_data, "config.conf")
            config_data = load_config("config.conf")
            if config_data["DISCORD_BOT_ENABLED"]:
                if not config_data.get("CONTROLLER_ENABLED"):
                    console.print("\n[bold yellow][!] Controller Hub masih DISABLED. Aktifkan Controller Hub terlebih dahulu.[/]")
                    config_data["DISCORD_BOT_ENABLED"] = 0
                    save_config(config_data, "config.conf")
                else:
                    started = start_discord_controller(config_data)
                    if not started:
                        console.print("\n[bold yellow][!] Bot belum berjalan. Pastikan discord.py terpasang dan credential lengkap.[/]")
            else:
                stop_discord_controller()
        elif choice == '9':
            break


def show_settings():
    clear_screen()
    config_data = load_config("config.conf")
    while True:
        reset_terminal()
        draw_header("SETTINGS")
        link = config_data.get('PRIVATE_SERVER_LINK', '')
        display_link = link[:25] + "..." if len(link) > 25 else link
        table = Table(box=None, padding=(0, 0), show_header=False)
        table.add_column("No", style="bold cyan", width=5, no_wrap=True)
        table.add_column("Icon", style="white", width=3, no_wrap=True)
        table.add_column("Config", style="white", width=25, no_wrap=True)
        table.add_column("Value", style="dim white", justify="right", width=23, no_wrap=True)
        global_place_id = config_data.get('GLOBAL_PLACE_ID', '')
        target_mode = "PRIVATE SERVER" if link else ("MAP / PLACE ID" if global_place_id else "NOT SET")
        table.add_row("[0]", "🎯", "Global Target", f"[cyan]{target_mode}[/]")
        table.add_row("[1]", "🔗", "Global Server Link", f"[cyan]{display_link or '<Not Set>'}[/]")
        table.add_row("[2]", "🗺", "Global Map / Place ID", f"[cyan]{global_place_id or '<Not Set>'}[/]")
        table.add_row("[3]", "⏱", "Timeout Wait", f"[cyan]{config_data.get('TIMEOUT_SECONDS', 45)}s[/]")
        table.add_row("[4]", "⏳", "Delay Package", f"[cyan]{config_data.get('DELAY_SECONDS', 3)}s[/]")
        table.add_row("[5]", "🔄", "Max Retries", f"[cyan]{config_data.get('MAX_RETRIES', 3)}x[/]")
        table.add_row("[6]", "❄", "Cooldown", f"[cyan]{config_data.get('COOLDOWN_SECONDS', 300)}s[/]")
        table.add_row("[7]", "🧹", "Auto Clear Cache", f"[cyan]{config_data.get('CLEAR_CACHE_MINUTES', 30)}m[/]")
        table.add_row("[8]", "📦", "Atur Link per Package", ">")
        table.add_row("[9]", "🗺", "Atur Map ID per Package", ">")
        table.add_row("[10]", "↩", "Kembali", ">")
        console.print(table)
        draw_footer("ESC / 10  Back to Menu  |  Global Link dan Map ID saling eksklusif")
        choice = safe_prompt_ask("\n[dim]Pilih (1-10)[/]", choices=[str(i) for i in range(1, 11)])
        if choice == "RESIZE_EVENT": continue
        if choice == '1':
            new_link = safe_console_input("\n[dim]Masukkan Global Private Server Link (kosongkan untuk hapus):[/] ")
            if new_link != "RESIZE_EVENT":
                new_link = new_link.strip()
                if new_link:
                    config_data['PRIVATE_SERVER_LINK'] = new_link
                    config_data['GLOBAL_PLACE_ID'] = ''
                else:
                    config_data['PRIVATE_SERVER_LINK'] = ''
                save_config(config_data, "config.conf")
        elif choice == '2':
            new_place_id = safe_console_input("\n[dim]Masukkan Global Map / Place ID (kosongkan untuk hapus):[/] " )
            if new_place_id != "RESIZE_EVENT":
                new_place_id = new_place_id.strip()
                if new_place_id and new_place_id.isdigit() and int(new_place_id) > 0:
                    config_data['GLOBAL_PLACE_ID'] = new_place_id
                    config_data['PRIVATE_SERVER_LINK'] = ''
                    save_config(config_data, "config.conf")
                elif not new_place_id:
                    config_data['GLOBAL_PLACE_ID'] = ''
                    save_config(config_data, "config.conf")
                else:
                    console.print("[bold red][!] Place ID harus berupa angka positif.[/]")
                    time.sleep(1)
        elif choice == '3':
            new_timeout = safe_console_input("\n[dim]Masukkan Timeout (detik):[/] ")
            if new_timeout != "RESIZE_EVENT" and new_timeout.isdigit():
                config_data['TIMEOUT_SECONDS'] = int(new_timeout)
                save_config(config_data, "config.conf")
        elif choice == '4':
            new_delay = safe_console_input("\n[dim]Masukkan Delay (detik):[/] ")
            if new_delay != "RESIZE_EVENT" and new_delay.isdigit():
                config_data['DELAY_SECONDS'] = int(new_delay)
                save_config(config_data, "config.conf")
        elif choice == '5':
            new_retries = safe_console_input("\n[dim]Masukkan Max Retries:[/] ")
            if new_retries != "RESIZE_EVENT" and new_retries.isdigit():
                config_data['MAX_RETRIES'] = int(new_retries)
                save_config(config_data, "config.conf")
        elif choice == '6':
            new_cooldown = safe_console_input("\n[dim]Masukkan Cooldown (detik):[/] ")
            if new_cooldown != "RESIZE_EVENT" and new_cooldown.isdigit():
                config_data['COOLDOWN_SECONDS'] = int(new_cooldown)
                save_config(config_data, "config.conf")
        elif choice == '7':
            new_cache = safe_console_input("\n[dim]Masukkan Interval Clear Cache (menit, 0 untuk nonaktif):[/] ")
            if new_cache != "RESIZE_EVENT" and new_cache.isdigit():
                config_data['CLEAR_CACHE_MINUTES'] = int(new_cache)
                save_config(config_data, "config.conf")
        elif choice == '8':
            show_link_manager(config_data)
        elif choice == '9':
            show_map_manager(config_data)
        elif choice == '10':
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
        table.add_row("[1]", "▶", "Auto Rejoiner", ">")
        table.add_row("[2]", "⚙", "Settings", ">")
        table.add_row("[3]", "🤖", "Discord Controller", ">")
        table.add_row("[4]", "🔑", "Auto Login Roblox", ">")
        table.add_row("[5]", "🧪", "Test (Unit Testing)", ">")
        table.add_row("[6]", "📝", "Logs (Lihat Log)", ">")
        table.add_row("[7]", "ⓘ", "About", ">")
        table.add_row("[8]", "🔄", "Update Program", ">")
        table.add_row("[9]", "⏻", "Exit", ">")
        console.print(table)
        draw_footer("CTRL+C  Dashboard    CTRL+Z  Exit")
        choice = safe_prompt_ask("\n[dim]Pilih menu (1-9)[/]", choices=[str(i) for i in range(1, 10)])
        if choice == "RESIZE_EVENT":
            continue
        clear_screen()
        if choice == '1':
            show_transition("Starting Engine...")
            run_auto_rejoiner()
        elif choice == '2':
            show_transition("Loading Settings...")
            show_settings()
        elif choice == '3':
            show_transition("Loading Discord Controller...")
            show_discord_controller_menu()
        elif choice == '4':
            show_transition("Loading Auto Login...")
            show_auto_login_menu()
        elif choice == '5':
            show_transition("Loading Unit Testing...")
            show_test_menu()
        elif choice == '6':
            show_transition("Fetching Logs...")
            reset_terminal()
            draw_header("LOGS VIEWER")
            log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "latest.log")
            if os.path.exists(log_path):
                console.print("[dim]Menampilkan 20 baris terakhir...[/]\n")
                os.system(f"tail -n 20 {log_path}")
            else:
                console.print("[dim]File log belum tersedia.[/]")
            draw_footer("Enter  Back to Menu")
            safe_console_input("\n[dim]Tekan Enter...[/]")
        elif choice == '7':
            show_transition("Opening About...")
            reset_terminal()
            draw_header("ABOUT")
            table = Table(box=None, padding=(0, 0), show_header=False)
            table.add_column("Key", style="dim white", width=20)
            table.add_column("Value", style="bold white", width=35)
            table.add_row("Aplikasi", "CARRERA-HUB Auto Rejoiner")
            table.add_row("Versi", "[cyan]Python Modular Edition[/]")
            table.add_row("Status", "[green]Stabil & Termux Root Ready[/]")
            table.add_row("Developer", "[magenta]Carrera-Hub Team[/]")
            console.print(table)
            draw_footer("Enter  Back to Menu")
            safe_console_input("\n[dim]Tekan Enter...[/]")
        elif choice == '8':
            show_transition("Checking Server...")
            run_updater()
        elif choice == '9':
            show_transition("Shutting Down...")
            try:
                sniper_agent.stop()
            except Exception:
                pass
            reset_terminal()
            sys.exit(0)

