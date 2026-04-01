"""UART/serial connection handler for camera console. Lists ports, connects at selected baud rate, runs commands."""
import base64
import os
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

# Base64 line: only valid base64 chars (BusyBox may wrap at 76 chars)
_BASE64_LINE = re.compile(r"^[A-Za-z0-9+/=]+$")

# Shell prompt at end of output (device ready for next command)
_UART_PROMPT_AT_END = re.compile(r"[\r\n]+VMC\d+\s*>\s*$", re.IGNORECASE)
# Prompt at start of line (device may prefix base64 output with e.g. VMC3073> )
_UART_PROMPT_AT_START = re.compile(r"^\s*VMC\d+\s*>\s*", re.IGNORECASE)


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


def _clean_uart_command_output(raw: str, sent_cmd: str) -> str:
    """
    Normalize UART response so it matches ADB behavior: command result only, no echo or prompt.
    - Normalize line endings (\\r\\n, \\r -> \\n)
    - Strip first non-empty line if it is exactly the echoed command
    - Strip trailing shell prompt line (e.g. VMC3073>, #, root@...)
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()
    # Strip leading echoed command (device echoes what we sent)
    sent_stripped = sent_cmd.strip()
    while lines:
        first = lines[0].strip()
        if not first:
            lines.pop(0)
            continue
        if first == sent_stripped:
            lines.pop(0)
            continue
        # Partial echo (e.g. command with trailing space from device)
        if first.endswith(sent_stripped) and len(first) <= len(sent_stripped) + 2:
            lines.pop(0)
            continue
        break
    # Strip trailing prompt line(s): only lines that are exactly the prompt (e.g. VMC3073>), not lines that start with prompt + output
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        if re.match(r"^VMC\d+\s*>?\s*$", last, re.IGNORECASE):
            lines.pop()
            continue
        if re.match(r"^#\s*$|^>\s*$", last):
            lines.pop()
            continue
        if re.match(r"^root@[\w\-]+[#\$]\s*$", last):
            lines.pop()
            continue
        break
    return "\n".join(lines).strip()


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
            # Verify we can talk to the device (wrong baud rate would pass login but commands fail)
            if not self._uart_verify_connection():
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
                self._port = None
                self._connected = False
                return False, (
                    "Connection could not be verified. Check baud rate (e.g. 115200) and cable, then try again."
                ), None
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
        except PermissionError:
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
        """Log in (root / arlo) when connecting so the session is authenticated for all later commands."""
        if not self._serial:
            return
        try:
            self._serial.reset_input_buffer()
            # Wait for "login:" prompt and send username (allow time for device to boot)
            login_chunks: list[bytes] = []
            deadline = time.monotonic() + 5.0
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
            deadline = time.monotonic() + 5.0
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
            # Drain remainder so the first command sees a clean shell prompt
            time.sleep(0.5)
            while self._serial.in_waiting:
                self._serial.read(self._serial.in_waiting)
                time.sleep(0.05)
        except Exception:
            pass

    def _uart_verify_connection(self) -> bool:
        """Send a test command and check the response. Returns False if baud rate or link is wrong."""
        if not self._serial:
            return False
        verify_token = "UART_VERIFY_7X9K"
        try:
            self._serial.reset_input_buffer()
            self._serial.write(f"echo {verify_token}\r\n".encode())
            self._serial.flush()
            chunks: list[bytes] = []
            deadline = time.monotonic() + 5.0
            last_data = time.monotonic()
            while time.monotonic() < deadline:
                if self._serial.in_waiting:
                    data = self._serial.read(self._serial.in_waiting)
                    if data:
                        chunks.append(data)
                        last_data = time.monotonic()
                else:
                    if time.monotonic() - last_data >= 1.0 and chunks:
                        break
                    time.sleep(0.05)
            raw = b"".join(chunks).decode(errors="replace")
            return verify_token in raw
        except Exception:
            return False

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
        Session is already authenticated at connect; if a login prompt appears, the session expired.
        For tar commands: wraps in sh -c so the shell waits, streams output, and waits for prompt.
        """
        if not self._connected or not self._serial:
            return False, "Not connected."
        try:
            full_cmd = command
            if args:
                full_cmd = f"{command} {' '.join(args)}"
            # tar (e.g. tar_logs), base64 (pull_logs), and tftp can run many min over UART; use long timeouts and wait for prompt
            is_tar = full_cmd.strip().startswith("tar ") or " tar " in full_cmd
            is_base64 = full_cmd.strip().startswith("base64 ") or " base64 " in full_cmd
            is_tftp = full_cmd.strip().startswith("tftp ") or " tftp " in full_cmd
            self._serial.reset_input_buffer()
            self._serial.write((full_cmd + "\r\n").encode())
            self._serial.flush()
            time.sleep(0.05)  # Let the device process the command before we read
            chunks: list[bytes] = []
            if is_tar:
                total_timeout = 300.0
                idle_timeout = 45.0
            elif is_tftp:
                total_timeout = 600.0
                idle_timeout = 60.0
            elif is_base64:
                total_timeout = 600.0
                idle_timeout = 120.0
            else:
                total_timeout = 120.0
                idle_timeout = 12.0
            deadline = time.monotonic() + total_timeout
            last_data = time.monotonic()
            print_offset = 0  # For streaming: how many chars we've already printed
            while time.monotonic() < deadline:
                if self._serial.in_waiting:
                    data = self._serial.read(self._serial.in_waiting)
                    if data:
                        chunks.append(data)
                        last_data = time.monotonic()
                        decoded = b"".join(chunks).decode(errors="replace")
                        # Stream new output to user so they see tar/tftp progress
                        if (is_tar or is_tftp) and len(decoded) > print_offset:
                            sys.stdout.write(decoded[print_offset:])
                            sys.stdout.flush()
                            print_offset = len(decoded)
                        # Done when we see the shell prompt (command finished)
                        if _UART_PROMPT_AT_END.search(decoded):
                            break
                else:
                    if time.monotonic() - last_data >= idle_timeout and chunks:
                        break
                    time.sleep(0.05)
            if (is_tar or is_tftp) and print_offset > 0:
                sys.stdout.write("\n")
                sys.stdout.flush()
            raw = b"".join(chunks).decode(errors="replace").strip()
            if re.search(r"login\s*:?\s*$", raw, re.IGNORECASE) or _UART_PASSWORD_PROMPT.search(raw):
                return False, "Session expired (login prompt). Disconnect and reconnect to log in again."
            cleaned = _clean_uart_command_output(raw, full_cmd)
            out = _strip_password_prompt_from_output(cleaned)
            if is_tar:
                return True, "Tar completed. Run pull_logs to download."
            # When cleaned output is empty, return raw so callers see actual device response (e.g. kvcmd get)
            if not out or not out.strip():
                return True, raw
            return True, out
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

    def pull_file(self, remote_path: str, local_path: str) -> tuple[bool, str]:
        """Pull a file from the device over UART by running base64 via execute() (same path as tar_logs), then decode and save."""
        if not _SERIAL_AVAILABLE or not self._connected or not self._serial:
            return False, "Not connected."
        try:
            # Use same execute() path as tar_logs so we read until prompt; long timeouts applied there for "base64 " commands
            success, output = self.execute(f"base64 {remote_path}")
            if not success:
                return False, output or "Command failed."
            if re.search(r"login\s*:?\s*$", output, re.IGNORECASE) or _UART_PASSWORD_PROMPT.search(output):
                return False, "Session expired (login prompt). Disconnect and reconnect to log in again."
            if "not found" in output.lower() or "no such file" in output.lower():
                return False, "File not found on device. Run tar_logs first to create the log archive."
            if "base64: not found" in output or "base64: command not found" in output.lower():
                return False, "base64 not available on device. Use ADB or SSH for pull_logs."
            # Extract base64 from cleaned output (may have prompt prefixes on lines)
            def _line_content(ln: str) -> str:
                s = ln.strip()
                s = _UART_PROMPT_AT_START.sub("", s)
                return s.strip()
            base64_lines = []
            for line in output.splitlines():
                content = _line_content(line)
                if content and _BASE64_LINE.match(content):
                    base64_lines.append(content)
            b64_string = "".join(base64_lines)
            if not b64_string and output:
                blocks = re.findall(r"[A-Za-z0-9+/=]{50,}", output)
                if blocks:
                    b64_string = max(blocks, key=len)
            if not b64_string:
                debug_path = os.path.join(os.path.dirname(os.path.abspath(local_path)) or ".", "pull_logs_debug.txt")
                try:
                    with open(debug_path, "w", encoding="utf-8", errors="replace") as f:
                        f.write("# UART output when pull_logs received no base64 data (via execute()).\n")
                        f.write(f"# remote_path={remote_path!r}\n")
                        f.write("# ---\n")
                        f.write(output)
                except OSError:
                    debug_path = "pull_logs_debug.txt"
                    try:
                        with open(debug_path, "w", encoding="utf-8", errors="replace") as f:
                            f.write(output)
                    except OSError:
                        debug_path = None
                # Log evidence: output often contains kernel/driver log lines instead of base64 (serial stream mixed)
                if output and (re.search(r"\[\d+\.\d+\]\s*\[", output) or "[atbm_log]" in output or "WSM]" in output):
                    msg = "UART returned log output instead of base64 (device may be streaming kernel logs on the same serial). Try pull_logs again, or use ADB/SSH to download the file."
                else:
                    msg = "No base64 data received. Check that the file exists (run tar_logs first)."
                if debug_path:
                    msg += f" Debug output saved to {debug_path}"
                return False, msg
            # Normalize padding: valid base64 length must be multiple of 4 (fixes "Incorrect padding" when last line is truncated or merged with prompt)
            if len(b64_string) % 4:
                b64_string += "=" * (4 - len(b64_string) % 4)
            try:
                data = base64.b64decode(b64_string)
            except Exception as decode_err:
                return False, str(decode_err)
            with open(local_path, "wb") as f:
                f.write(data)
            return True, f"Downloaded to {local_path}"
        except OSError as e:
            return False, str(e)
        except Exception as e:
            err = str(e).lower()
            if "could not open" in err or "access is denied" in err or "permission" in err:
                self._connected = False
                self._serial = None
                self._port = None
                return False, "Device disconnected."
            return False, str(e)

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
