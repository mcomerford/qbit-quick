import socket
import threading


def interruptible_sleep(seconds: float, stop_event: threading.Event) -> bool:
    """
    Sleeps for the given duration, unless interrupted by setting the provided stop_event.

    Args:
        seconds (float): Number of seconds to sleep.
        stop_event (threading.Event): Event to interrupt sleep.

    Returns:
        bool: True if the sleep was interrupted (stop_event was set), False otherwise.
    """
    return stop_event.wait(timeout=seconds)


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        result = s.connect_ex((host, port))
        return result == 0  # 0 means port is in use
