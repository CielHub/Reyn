"""
Modul: monitor.py
Tanggung Jawab: Memantau status proses, Dashboard Real-time (Responsive), 
                dan memicu Watchdog Recovery (Hanya untuk Crash/Single Recovery).
"""
import os
import time
import sys
import shutil

try:
    import pyfiglet
except ImportError:
    pass

from core.logger import log, set_console_logging
from core.ui import (
    console,
    reset_terminal,
    start_dashboard_resize_watcher,
    stop_dashboard_resize_watcher,
    consume_dashboard_resize_event,
)
from core.error_detector import start_error_detector
from core.recovery_manager import start_recovery_manager, trigger_recovery, is_global_recovery
from core.memory_guard import start_memory_guard
from core.process_manager import get_pid
from rich.live import Live
from rich.table import Table
from rich.console import Group
from rich.text import Text

_CACHED_HEADER_ART = None


def get_dashboard_terminal_size():
    """Return the current terminal size used by the dashboard renderer."""
    size = shutil.get_terminal_size(fallback=(80, 24))
    return size.columns, size.lines


def create_dashboard_live(renderable, *, screen=False):
    """Create a deterministic Rich Live renderer for the dashboard.

    Auto-refresh is intentionally disabled so only the dashboard loop controls
    terminal writes. This prevents a background Rich refresh from racing a
    terminal resize on Android/Termux.
    """
    return Live(
        renderable,
        console=console,
        refresh_per_second=1,
        auto_refresh=False,
        transient=False,
        screen=screen,
    )


def refresh_dashboard_live(live, renderable, previous_size=None):
    """Refresh the dashboard and fully redraw only when terminal size changes.

    A resize (for example Android keyboard open/close) invalidates Rich's
    previous cursor/height calculation. We clear the visible terminal and
    reset Rich's cached frame shape before drawing the new frame. This is a
    visual redraw only; it is deliberately NOT a terminal `reset`.
    """
    current_size = get_dashboard_terminal_size()
    resized = previous_size is not None and current_size != previous_size

    if resized or consume_dashboard_resize_event():
        # Invalidate Rich's previous frame geometry BEFORE clearing so it won't
        # emit cursor-up sequences based on the old terminal height.
        try:
            live._live_render._shape = None
        except AttributeError:
            pass
        console.clear(home=True)

    live.update(renderable, refresh=True)
    return current_size, resized

def format_uptime(start_time, current_time):
    if start_time == 0: return "00:00:00"
    elapsed = int(current_time - start_time)
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def draw_dashboard(stats, current_time, pkg_count, include_header=True):
    renderables = []
    term_cols, term_lines = shutil.get_terminal_size()
    
    DASHBOARD_WIDTH = min(60, term_cols)
    rule = Text.from_markup(f"[dim cyan]{'─' * DASHBOARD_WIDTH}[/]")

    if include_header:
        if term_lines > 22:
            global _CACHED_HEADER_ART
            if _CACHED_HEADER_ART is None:
                try:
                    ascii_art = pyfiglet.figlet_format("CARRERA", font="slant")
                    lines = [f"[bold green]{line}[/]" for line in ascii_art.split('\n') if line.strip()]
                    _CACHED_HEADER_ART = "\n".join(lines)
                except Exception:
                    _CACHED_HEADER_ART = "[bold green]CARRERA[/]"
            
            header_render = Text.from_markup(_CACHED_HEADER_ART)
            info_render = Text.from_markup(f"[dim white]Version 1.0.0   |   Status Monitoring   |   Packages {pkg_count}[/]")
            renderables.extend([header_render, info_render, rule])
        else:
            header_render = Text.from_markup(f"[bold green]CARRERA-HUB[/] [dim white]| Compact Mode | Packages {pkg_count}[/]")
            renderables.extend([header_render, rule])

    running = sum(1 for s in stats.values() if s['status'] == 'ONLINE')
    recover = sum(1 for s in stats.values() if s['status'] in ['RECOVERY', 'LOGIN', 'LOADING'])
    offline = sum(1 for s in stats.values() if s['status'] in ['FAILED', 'COOLDOWN', 'LOGIN FAILED', 'CAPTCHA'])
    
    summary_text = f"Clones {running}/{pkg_count}   |   [bold yellow]● Recover {recover}[/]   |   [bold red]● Offline {offline}[/]"
    renderables.append(Text.from_markup(summary_text))
    renderables.append(rule)

    # Keep the table inside the 60-column dashboard rule. With one cell of
    # horizontal padding on both sides, these widths total exactly 60 columns.
    # The old PACKAGE=16/PID=5 layout totaled 67 columns and pushed the right
    # side of the table beyond the rule on normal 60-column terminals.
    table = Table(box=None, padding=(0, 1), show_header=True, header_style="dim white", expand=False)
    table.add_column("ID", style="bold cyan", width=3, no_wrap=True)
    table.add_column("PACKAGE", style="white", width=11, no_wrap=True, overflow="ellipsis")
    table.add_column("PID", style="cyan", width=6, no_wrap=True)
    table.add_column("STATUS", width=10, no_wrap=True)
    table.add_column("UPTIME", style="white", width=8, no_wrap=True)
    table.add_column("L", style="dim white", width=2, justify="right", no_wrap=True)
    table.add_column("R", style="dim white", width=2, justify="right", no_wrap=True)
    table.add_column("C", style="dim white", width=2, justify="right", no_wrap=True)
    
    for idx, (pkg, s) in enumerate(stats.items(), 1):
        uptime_str = format_uptime(s['uptime_start'], current_time) if s['status'] == 'ONLINE' else "--:--:--"
        display_pkg = pkg.replace("com.roblox.", "..") if "com.roblox." in pkg else pkg
        
        if s['status'] == 'ONLINE': stat_fmt = "[bold green]● Farming[/]"
        elif s['status'] == 'LOADING': stat_fmt = "[bold blue]● Loading[/]"
        elif s['status'] == 'RECOVERY': stat_fmt = "[bold yellow]● Recover[/]"
        elif s['status'] == 'FAILED': stat_fmt = "[bold red]● Offline[/]"
        elif s['status'] == 'COOLDOWN': stat_fmt = "[bold red]● Cooldown[/]"
        elif s['status'] == 'LOGIN': stat_fmt = "[bold magenta]● Login[/]"
        elif s['status'] == 'LOGIN FAILED': stat_fmt = "[bold red]● Log Fail[/]"
        elif s['status'] == 'CAPTCHA': stat_fmt = "[bold red]● Captcha[/]"
        else: stat_fmt = f"[white]● {s['status'][:8]}[/]"
            
        table.add_row(
            f"[{idx}]", display_pkg, str(s['pid']), stat_fmt, uptime_str,
            str(s['launch_count']), str(s['recovery_count']), str(s['crash_count'])
        )
    renderables.append(table)
    renderables.append(rule)
    
    if term_lines > 22:
        renderables.append(Text.from_markup("[dim white]CTRL+C Back to Menu   |   CTRL+Z Exit   |   Refresh: 1s[/]"))
    
    return Group(*renderables)

def start_monitoring(packages, intent_url, timeout_seconds, max_retries, cooldown_secs, stats=None, config_data=None):
    log.info("MONITORING: Memasuki mode penjagaan (Watchdog & Error Detector)...")
    time.sleep(1)

    current_time = time.time()
    pkg_count = len(packages)
    
    if stats is None:
        stats = {pkg: {
            'pid': '-', 'status': 'ONLINE', 'uptime_start': current_time, 
            'launch_count': 1, 'recovery_count': 0, 'crash_count': 0,
            'consecutive_crashes': 0, 'last_recovery_time': current_time, 'cooldown_until': 0
        } for pkg in packages}

    tracked_pids = {}
    for pkg in packages:
        pid = get_pid(pkg)
        tracked_pids[pkg] = pid
        stats[pkg]['pid'] = pid if pid else '-'

    # ERROR DETECTOR, RECOVERY MANAGER, & MEMORY GUARD AKTIF
    start_error_detector()
    start_recovery_manager(packages, stats, tracked_pids, intent_url, timeout_seconds, config_data)
    start_memory_guard(config_data)

    check_interval = 15
    last_check_time = current_time
    STABILITY_THRESHOLD = 300 

    set_console_logging(False)

    dashboard_size = get_dashboard_terminal_size()
    resize_handler = start_dashboard_resize_watcher()

    try:
        with create_dashboard_live(
            draw_dashboard(stats, current_time, pkg_count, include_header=True),
            screen=False,
        ) as live:
            try:
                while True:
                    current_time = time.time()
                    
                    # Watchdog Check
                    if current_time - last_check_time >= check_interval:
                        
                        if is_global_recovery():
                            last_check_time = current_time
                            continue

                        # Snapshot all packages before starting any recovery
                        # worker so simultaneous force-closes share one tick.
                        crash_batch = []
                        healthy_batch = []

                        for pkg in packages:
                            if stats[pkg]['status'] in ['RECOVERY', 'LOGIN', 'LOADING', 'CAPTCHA']:
                                continue

                            if stats[pkg]['cooldown_until'] > current_time:
                                stats[pkg]['status'] = 'COOLDOWN'
                                stats[pkg]['pid'] = '-'
                                continue

                            current_pid = get_pid(pkg)
                            expected_pid = tracked_pids.get(pkg, '')

                            if not current_pid or current_pid != expected_pid:
                                crash_batch.append((pkg, current_pid, expected_pid))
                            else:
                                healthy_batch.append((pkg, current_pid))

                        # Commit all crash states before any worker can run.
                        for pkg, current_pid, expected_pid in crash_batch:
                            stats[pkg]['crash_count'] += 1
                            stats[pkg]['consecutive_crashes'] += 1

                            if stats[pkg]['consecutive_crashes'] > max_retries:
                                stats[pkg]['cooldown_until'] = current_time + cooldown_secs
                                stats[pkg]['status'] = 'COOLDOWN'
                                stats[pkg]['pid'] = '-'
                                continue

                            stats[pkg]['status'] = 'RECOVERY'
                            stats[pkg]['pid'] = '-'
                            tracked_pids[pkg] = ''

                        # Spawn only after the complete batch is committed.
                        for pkg, _, _ in crash_batch:
                            if stats[pkg]['status'] == 'RECOVERY':
                                trigger_recovery(pkg)

                        for pkg, current_pid in healthy_batch:
                            stats[pkg]['pid'] = current_pid
                            if stats[pkg]['status'] in ['FAILED', 'LOGIN FAILED', 'CAPTCHA']:
                                continue

                            if stats[pkg]['consecutive_crashes'] > 0:
                                if current_time - stats[pkg]['last_recovery_time'] > STABILITY_THRESHOLD:
                                    stats[pkg]['consecutive_crashes'] = 0

                        last_check_time = current_time

                    # Dashboard refresh only. A terminal resize gets a clean
                    # visual redraw, never a full terminal `reset`.
                    dashboard_size, _ = refresh_dashboard_live(
                        live,
                        draw_dashboard(stats, current_time, pkg_count, include_header=True),
                        dashboard_size,
                    )
                    time.sleep(1)
                    
            except KeyboardInterrupt:
                pass
    finally:
        stop_dashboard_resize_watcher(resize_handler)
        set_console_logging(True)
          
