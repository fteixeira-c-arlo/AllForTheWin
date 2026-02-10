"""UART/serial connection handler for camera console. Lists ports, connects at selected baud rate, runs commands."""
import re
import sys
import threading
import time
from typing import Any, Callable

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


# Camera console credentials when UART prompts (login root, password arlo)
UART_CONSOLE_LOGIN = "root"
UART_CONSOLE_PASSWORD = "arlo"

# Prompt patterns: device asks for password (e.g. "Password:" or "Password: " at end of buffer)
_UART_PASSWORD_PROMPT = re.compile(r"password\s*:?\s*(\r?\n)?\s*$", re.IGNORECASE | re.DOTALL)
# Incomplete prompt (device might still be sending "Password:")
_UART_PASSWORD_PREFIX = re.compile(r"pass\s*$", re.IGNORECASE)


def _strip_password_prompt_from_output(text: str) -> str:
    """Remove 'Password:' and 'login:' prompt lines from output so they are not shown to the user."""
    lines = text.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^password\s*:?\s*$", stripped, re.IGNORECASE):
            continue
        if re.match(r"^login\s*:?\s*$", stripped, re.IGNORECASE):
            continue
        out.append(line)
    return "\n".join(out).strip()


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
            # Perform login (root / arlo) so the session is authenticated
            self._uart_do_login()
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

    def _uart_do_login(self) -> None:
        """Send login (root) and password (arlo) when the device prompts. Does not fail if no prompt."""
        if not self._serial:
            return
        try:
            self._serial.reset_input_buffer()
            # Wait for optional "login:" prompt and send username
            login_chunks = []
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                if self._serial.in_waiting:
                    data = self._serial.read(self._serial.in_waiting)
                    if data:
                        login_chunks.append(data)
                        text = b"".join(login_chunks).decode(errors="replace")
                        if re.search(r"login\s*:?\s*", text, re.IGNORECASE):
                            self._serial.write((UART_CONSOLE_LOGIN + "\r\n").encode())
                            self._serial.flush()
                            break
                time.sleep(0.05)
            # Wait for "Password:" and send password
            chunks = list(login_chunks)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if self._serial.in_waiting:
                    data = self._serial.read(self._serial.in_waiting)
                    if data:
                        chunks.append(data)
                        text = b"".join(chunks).decode(errors="replace")
                        if _UART_PASSWORD_PROMPT.search(text) or re.search(r"password\s*:\s*", text, re.IGNORECASE):
                            self._serial.write((UART_CONSOLE_PASSWORD + "\r\n").encode())
                            self._serial.flush()
                            break
                time.sleep(0.05)
            # Drain remainder so next command sees a clean state
            time.sleep(0.3)
            while self._serial.in_waiting:
                self._serial.read(self._serial.in_waiting)
                time.sleep(0.05)
        except Exception:
            pass

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
            login_sent = False
            password_sent = False
            total_timeout = 30.0
            # Use longer idle timeout so device has time to run command and send output (e.g. caliget model_num)
            idle_timeout = 4.0
            # After sending password, wait longer for the actual command response
            idle_after_password = 6.0
            deadline = time.monotonic() + total_timeout
            last_data = time.monotonic()
            while time.monotonic() < deadline:
                if self._serial.in_waiting:
                    data = self._serial.read(self._serial.in_waiting)
                    if data:
                        chunks.append(data)
                        last_data = time.monotonic()
                        if not login_sent or not password_sent:
                            text = b"".join(chunks).decode(errors="replace")
                            # Device may show "login:" then "Password:"; send username first, then password
                            if not login_sent and re.search(r"login\s*:?\s*", text, re.IGNORECASE):
                                self._serial.write((UART_CONSOLE_LOGIN + "\r\n").encode())
                                self._serial.flush()
                                login_sent = True
                                last_data = time.monotonic()
                            elif _UART_PASSWORD_PROMPT.search(text):
                                if not login_sent:
                                    # No login prompt seen; send username then password so device gets both
                                    self._serial.write((UART_CONSOLE_LOGIN + "\r\n").encode())
                                    self._serial.flush()
                                    time.sleep(0.15)
                                    login_sent = True
                                self._serial.write((UART_CONSOLE_PASSWORD + "\r\n").encode())
                                self._serial.flush()
                                password_sent = True
                                last_data = time.monotonic()
                else:
                    current_idle = idle_after_password if password_sent else idle_timeout
                    idle = time.monotonic() - last_data
                    if idle >= current_idle and chunks:
                        text = b"".join(chunks).decode(errors="replace")
                        if not password_sent and _UART_PASSWORD_PREFIX.search(text):
                            last_data = time.monotonic()
                        elif not login_sent and re.search(r"login\s*$", text, re.IGNORECASE):
                            last_data = time.monotonic()
                        else:
                            break
                    time.sleep(0.05)
            raw = b"".join(chunks).decode(errors="replace").strip()
            out = _strip_password_prompt_from_output(raw)
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

    def start_tail_logs_to_file(
        self, log_path: str, line_callback: Callable[[str], None] | None = None
    ) -> tuple[bool, str]:
        """Start streaming tail -f over UART to a file. Optional line_callback(line) for each line. Use stop_tail_logs() to stop."""
        if not _SERIAL_AVAILABLE or not self._connected or not self._serial:
            return False, "Not connected."
        try:
            stop_event = threading.Event()
            log_file = open(log_path, "ab")
            line_buf = bytearray()

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
                                if line_callback is not None:
                                    line_buf.extend(data)
                                    while b"\n" in line_buf or b"\r" in line_buf:
                                        idx = line_buf.find(b"\n")
                                        if idx < 0:
                                            idx = line_buf.find(b"\r")
                                        if idx < 0:
                                            break
                                        line_bytes = bytes(line_buf[: idx + 1])
                                        line_buf[:] = line_buf[idx + 1 :]
                                        try:
                                            text = line_bytes.decode(
                                                "utf-8", errors="replace"
                                            ).rstrip("\r\n")
                                            if text:
                                                line_callback(text)
                                        except Exception:
                                            pass
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
