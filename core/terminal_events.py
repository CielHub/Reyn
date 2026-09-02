"""Terminal UI events shared between background workers and the dashboard renderer.

Background threads never write to the terminal directly. They only request a
reset; the dashboard thread consumes the request and performs the actual
terminal operation.
"""
import threading

_reset_event = threading.Event()


def request_terminal_reset():
    """Request a one-shot terminal reset on the next dashboard render."""
    _reset_event.set()


def consume_terminal_reset() -> bool:
    """Atomically consume a pending terminal reset request."""
    if not _reset_event.is_set():
        return False
    _reset_event.clear()
    return True
