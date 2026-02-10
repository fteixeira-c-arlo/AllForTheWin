"""UART/serial connection handler for camera console. Lists ports, connects at selected baud rate, runs commands."""
import re
import sys
import threading
import time
from typing import Any

from utils.logger import get_logger

logger = get_logger()

# Optional: pyserial for serial port access
try:
    import serial
    from serial.tools import list_ports
    _SERIAL_AVAILABLE = True
except ImportError:
    _SERIAL_AVAILABLE = False
    serial = None
    list_ports = None


def _probe_windows_com_ports() -> list[tuple[str, str]]:
    """Fallback when list_ports.comports() is empty on Windows: try opening COM1–COM30."""
    if not _SERIAL_AVAILABLE or sys.platform != "win32":
        return []
    result = []
    for i in range(1, 31):
        port = f"COM{i}"
        try:
            s = serial.Serial(port=port, baudrate=115200, timeout=0.1)
            s.close()
            result.append((port, port))
        except (OSError, serial.SerialException):
            continue
    return result


def list_uart_ports() -> list[tuple[str, str]]:
    """
    Return list of (port_device, description) for available serial ports.
    e.g. [("COM3", "USB Serial (COM3)"), ("COM4", "USB-UART (COM4)")].
    On Windows, if comports() returns nothing, falls back to probing COM1–COM30.
    """
    if not _SERIAL_AVAILABLE:
        return []
    try:
        result = []
        for info in list_ports.comports():
            dev = getattr(info, "device", None) or getattr(info, "name", None)
            if dev is None:
                # Older pyserial: info might be a tuple (port, desc, hwid)
                if isinstance(info, (list, tuple)) and len(info) >= 1:
                    dev = info[0]
                else:
                    dev = str(info)
            desc = getattr(info, "description", "") or (info[1] if isinstance(info, (list, tuple)) and len(info) > 1 else dev)
            if dev:
                result.append((str(dev), str(desc) if desc else str(dev)))
        if not result and sys.platform == "win32":
            result = _probe_windows_com_ports()
        return result
    except Exception as e:
        logger.exception("List UART ports: %s", e)
        if sys.platform == "win32":
            return _probe_windows_com_ports()
        return []


# Camera console password when UART prompts for it (e.g. before running some commands)
UART_CONSOLE_PASSWORD = "arlo"

# Prompt patterns that indicate the device is asking for the password
_UART_PASSWORD_PROMPT = re.compile(r"password\s*:?\s*$", re.IGNORECASE | re.MULTILINE)


class UARTHandler:
    """Handle UART/serial connect, disconnect, and command execution (send command, read response)."""

    def __init__(self) -> None:
        self._serial: Any = None
        self._port: str | None = None
        self._baud: int = 115200
        self._connected = False

    def connect(self, port: str, baud_rate: int = 115200) -> tuple[bool, str, dict[str, Any] | None]:
        """
        Open serial port at given baud rate. Returns (success, message, settings_for_config).
        """
        if not _SERIAL_AVAILABLE:
            return False, "pyserial is required for UART. Install with: pip install pyserial", None
        try:
            ser = serial.Serial(
                port=port,
                baudrate=baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.5,
                write_timeout=5,
            )
            self._serial = ser
            self._port = port
            self._baud = baud_rate
            self._connected = True
            return True, f"Connected to {port} at {baud_rate} baud", {
                "port": port,
                "baud_rate": baud_rate,
            }
        except serial.SerialException as e:
            err_str = str(e).lower()
            if "access is denied" in err_str or "permission" in err_str or "permissionerror" in err_str:
                return False, (
                    f"{e}\n\n"
                    "Try: (1) Close any other app using this port (PuTTY, Arduino IDE, other terminals). "
                    "(2) Unplug and replug the USB–serial cable. "
                    "(3) Run this terminal as Administrator."
                ), None
            return False, str(e), None
        except PermissionError as e:
            return False, (
                f"Access denied opening {port}. "
                "Close other programs using the port, unplug/replug the cable, or run as Administrator."
            ), None
        except OSError as e:
            if getattr(e, "errno", None) == 13:
                return False, (
                    f"Access denied opening {port}. "
                    "Close other programs using the port, unplug/replug the cable, or run as Administrator."
                ), None
            return False, str(e), None
        except Exception as e:
            logger.exception("UART connect")
            return False, str(e), None

    def disconnect(self) -> None:
        """Close serial connection."""
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        self._port = None
        self._connected = False

    def execute(self, command: str, args: list[str] | None = None) -> tuple[bool, str]:
        """
        Send command (and args) over UART, read response. Returns (success, output_or_error).
        Sends command + \\r\\n, then reads until idle (no data for ~1.5s) or max 30s.
        If the camera prompts for a password, sends the console password (arlo) automatically.
        """
        if not self._connected or not self._serial:
            return False, "Not connected."
        try:
            full_cmd = command
            if args:
                full_cmd = f"{command} {' '.join(args)}"
            self._serial.reset_input_buffer()
            self._serial.write((full_cmd + "\r\n").encode())
            self._serial.flush()
            chunks = []
            password_sent = False
            total_timeout = 30.0
            idle_timeout = 1.5
            deadline = time.monotonic() + total_timeout
            last_data = time.monotonic()
            while time.monotonic() < deadline:
                if self._serial.in_waiting:
                    data = self._serial.read(self._serial.in_waiting)
                    if data:
                        chunks.append(data)
                        last_data = time.monotonic()
                        if not password_sent:
                            text = b"".join(chunks).decode(errors="replace")
                            if _UART_PASSWORD_PROMPT.search(text):
                                self._serial.write((UART_CONSOLE_PASSWORD + "\r\n").encode())
                                self._serial.flush()
                                password_sent = True
                                last_data = time.monotonic()
                else:
                    if time.monotonic() - last_data >= idle_timeout and chunks:
                        break
                    time.sleep(0.05)
            out = b"".join(chunks).decode(errors="replace").strip()
            return True, out or "OK"
        except Exception as e:
            err = str(e).lower()
            if "could not open" in err or "access is denied" in err or "permission" in err:
                self._connected = False
                self._serial = None
                self._port = None
                return False, "Device disconnected."
            return False, str(e)

    def is_connected(self) -> bool:
        return self._connected and self._serial is not None and getattr(self._serial, "is_open", True)

    def device_identifier(self) -> str | None:
        if not self._port:
            return None
        return f"{self._port}@{self._baud}"

    def start_tail_logs_to_file(self, log_path: str) -> tuple[bool, str]:
        """Start streaming tail -f over UART to a file. Returns (success, error_message). Use stop_tail_logs() to stop and save."""
        if not _SERIAL_AVAILABLE or not self._connected or not self._serial:
            return False, "Not connected."
        try:
            stop_event = threading.Event()
            log_file = open(log_path, "ab")

            def reader() -> None:
                try:
                    self._serial.write(b"tail -f /tmp/logs/system-log_V1_0\r\n")
                    self._serial.flush()
                    while not stop_event.is_set():
                        if self._serial.in_waiting:
                            data = self._serial.read(self._serial.in_waiting)
                            if data:
                                log_file.write(data)
                                log_file.flush()
                        else:
                            time.sleep(0.05)
                except Exception:
                    pass
                finally:
                    try:
                        log_file.close()
                    except Exception:
                        pass

            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            setattr(self, "_tail_file", log_file)
            setattr(self, "_tail_thread", thread)
            setattr(self, "_tail_stop_event", stop_event)
            return True, ""
        except Exception as e:
            return False, str(e)

    def stop_tail_logs(self) -> None:
        """Stop UART tail stream; log file is closed and saved."""
        stop_event = getattr(self, "_tail_stop_event", None)
        thread = getattr(self, "_tail_thread", None)
        if stop_event is not None:
            stop_event.set()
        if thread is not None:
            thread.join(timeout=3.0)
        setattr(self, "_tail_thread", None)
        setattr(self, "_tail_stop_event", None)
        setattr(self, "_tail_file", None)
