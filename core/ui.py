"""
Modul: ui.py
Tanggung Jawab: Menyediakan komponen UI terminal, warna ANSI, dan Header statis responsif.
"""
import os
import sys
import time
import signal

try:
    import pyfiglet
except ImportError:
    os.system("pip install pyfiglet")
    import pyfiglet

from rich.console import Console
from rich.text import Text
from rich.prompt import Prompt

console = Console()
LAYOUT_WIDTH = 60 

# --- SISTEM DETEKSI RESIZE (SIGWINCH) AMAN ---
class WindowResizeError(Exception):
    pass

def _sigwinch_handler(signum, frame):
    raise WindowResizeError()

def _set_sigwinch_handler():
    if hasattr(signal, 'SIGWINCH'):
        return signal.signal(signal.SIGWINCH, _sigwinch_handler)
    return None

def _restore_sigwinch_handler(old_handler):
    if hasattr(signal, 'SIGWINCH') and old_handler is not None:
        signal.signal(signal.SIGWINCH, old_handler)

def safe_prompt_ask(text, choices=None, password=False):
    """Menangkap event resize (keyboard muncul) HANYA saat menunggu input."""
    old = _set_sigwinch_handler()
    try:
        return Prompt.ask(text, choices=choices, password=password)
    except WindowResizeError:
        return "RESIZE_EVENT"
    finally:
        _restore_sigwinch_handler(old)

def safe_console_input(text):
    old = _set_sigwinch_handler()
    try:
        return console.input(text)
    except WindowResizeError:
        return "RESIZE_EVENT"
    finally:
        _restore_sigwinch_handler(old)
# ---------------------------------------------

def reset_terminal():
    os.system('clear' if os.name == 'posix' else 'cls')

def get_compact_header(title="CARRERA-HUB v1.0", user="root", pkg_count="-", status="Active"):
    t = time.strftime("%H:%M:%S")
    header_text = Text.from_markup(
        f"[bold green]{title}[/]  |  "
        f"[dim white]User:[/] [cyan]{user}[/]  |  "
        f"[dim white]Pkg:[/] [cyan]{pkg_count}[/]  |  "
        f"[dim white]Status:[/] [cyan]{status}[/]  |  "
        f"[dim white]Time:[/] [cyan]{t}[/]",
        justify="center"
    )
    return header_text

def draw_header(subtitle="MENU"):
    """Membangun Header responsif yang mendeteksi ukuran kolom & baris Terminal secara dinamis."""
    current_width = console.width
    current_height = console.height
    
    # BUG FIX: Fallback cerdas untuk layar sempit (Zoom in) ATAU layar pendek (Landscape Keyboard)
    if current_width < 45 or current_height < 22:
        console.print("[bold green]CARRERA-HUB[/]")
    else:
        try:
            ascii_art = pyfiglet.figlet_format("CARRERA", font="small")
        except Exception:
            ascii_art = pyfiglet.figlet_format("CARRERA")
            
        for line in ascii_art.split('\n'):
            if line.strip():
                console.print(f"[bold green]{line}[/]")
    
    console.print(f"[bold cyan]{subtitle}[/] [dim white]| Version 1.0.0 | User root[/]")
    line_width = min(current_width, 60)
    console.print("[dim cyan]" + "─" * line_width + "[/]")

def show_transition(message="Loading..."):
    with console.status(f"[dim cyan]{message}[/]", spinner="dots"):
        time.sleep(0.4) 
    reset_terminal()

def draw_footer(text="CTRL+C  Dashboard    CTRL+Z  Exit"):
    console.print(f"\n[dim white]{text}[/]")
    
