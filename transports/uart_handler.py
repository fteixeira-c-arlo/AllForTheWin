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

# #region agent log
_AGENT_DEBUG_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debug-2d906c.log"
)


def _agent_debug_ndjson(hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    import json

    try:
        line = json.dumps(
            {
                "sessionId": "2d906c",
                "timestamp": int(time.time() * 1000),
                "hypothesisId": hypothesis_id,
                "location": location,
                "message": message,
                "data": data,
            },
            ensure_ascii=False,
            default=str,
        )
        with open(_AGENT_DEBUG_LOG_PATH, "a", encoding="utf-8") as _df:
            _df.write(line + "\n")
    except Exception:
        pass


# #endregion

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


def _port_key_for_match(port: str) -> str:
    """Normalize COM port names for comparison (handles \\\\.\\COM12 style)."""
    x = str(port).upper()
    if x.startswith("\\\\.\\"):
        return x[4:]
    return x


def _uart_probe_windows_port_exists(port: str) -> bool:
    """
    When ``comports()`` is empty, probe whether ``port`` still exists.
    If the session already holds the port open, open fails with access/busy — treat as present.
    """
    if not _SERIAL_AVAILABLE or sys.platform != "win32":
        return True
    p = (port or "").strip()
    if not p:
        return False
    try:
        s = serial.Serial(port=p, baudrate=115200, timeout=0.01)
        s.close()
        return True
    except serial.SerialException as e:
        msg = str(e).lower()
        if (
            "access" in msg
            or "denied" in msg
            or "busy" in msg
            or "being used" in msg
            or "in use" in msg
            or "permission" in msg
        ):
            return True
        if "could not find" in msg or "cannot find" in msg:
            return False
        if "no such file" in msg or "does not exist" in msg:
            return False
        return True
    except OSError as e:
        winerr = getattr(e, "winerror", None)
        if winerr in (2, 3):
            return False
        return True
    except Exception:
        return True


def uart_port_transport_alive_for_watchdog(port: str) -> bool:
    """
    OS-level check that a UART device is still attached. Safe to call from a thread
    while another thread holds the same port open (Windows: busy → still alive).

    While the app holds the port open, ``comports()`` sometimes omits it or lists it
    under another name briefly — do not treat ``not in list`` as unplugged on Windows
    until ``_uart_probe_windows_port_exists`` also fails (see transport_heartbeat).
    """
    if not _SERIAL_AVAILABLE:
        return True
    p = (port or "").strip()
    if not p:
        return False
    try:
        if list_ports is None:
            return True
        present: set[str] = set()
        for info in list_ports.comports():
            dev = getattr(info, "device", None) or getattr(info, "name", None)
            if isinstance(info, (list, tuple)) and len(info) >= 1 and dev is None:
                dev = info[0]
            if dev:
                present.add(str(dev).upper())
        my_key = _port_key_for_match(p)
        if present:
            keys = {_port_key_for_match(x) for x in present}
            if my_key in keys:
                return True
            if sys.platform == "win32":
                return _uart_probe_windows_port_exists(p)
            return True
        if sys.platform == "win32":
            return _uart_probe_windows_port_exists(p)
        return True
    except Exception:
        return True


# Camera console credentials when UART prompts (login root, password arlo)
UART_CONSOLE_LOGIN = "root"
UART_CONSOLE_PASSWORD = "arlo"

# Prompt patterns: device asks for password (e.g. "Password:" or "Password: " at end of buffer)
_UART_PASSWORD_PROMPT = re.compile(r"password\s*:?\s*(\r?\n)?\s*$", re.IGNORECASE | re.DOTALL)
# Incomplete prompt (device might still be sending "Password:")
_UART_PASSWORD_PREFIX = re.compile(r"pass\s*$", re.IGNORECASE)


def _uart_execute_raw_shows_unauthenticated_state(raw: str) -> bool:
    """
    True when a completed command capture shows the shell is back at BusyBox login/password.
    Used only after execute() I/O (not for global log scans): line-anchored ``Password:`` / ``login:``
    so dmesg is unlikely to false-trigger, but ``Password: [timestamp]`` on one line still matches.
    """
    if not (raw or "").strip():
        return False
    if re.search(r"(?m)(?:^|[\r\n])\s*password\s*:\s*", raw, re.IGNORECASE):
        return True
    if re.search(r"(?m)(?:^|[\r\n])\s*login\s*:\s*", raw, re.IGNORECASE):
        return True
    tail = raw[-2000:] if len(raw) > 2000 else raw
    for ln in tail.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if re.match(r"^\s*login\s+incorrect\s*$", ln.strip(), re.IGNORECASE):
            return True
    if re.search(r"login\s*:?\s*$", raw, re.IGNORECASE) or _UART_PASSWORD_PROMPT.search(raw):
        return True
    return False


def _uart_buffer_shows_login_or_password_prompt(text: str) -> bool:
    """
    True if the UART capture ends in BusyBox login/password (not an authenticated shell).
    Used to reject false-positive verify when 'echo TOKEN' is consumed as a username and
    TOKEN still appears in the echoed line (see debug: tail ends with Password:).

    Do not scan the whole buffer for 'login incorrect' — kernel/firmware logs often contain
    that substring and would falsely trigger credential injection mid-session.
    """
    t = text or ""
    if not t.strip():
        return False
    if _UART_PASSWORD_PROMPT.search(t):
        return True
    if re.search(r"(?:^|[\r\n])\s*login\s*:\s*$", t, re.IGNORECASE | re.MULTILINE):
        return True
    tail = t[-400:] if len(t) > 400 else t
    tail_lines = [ln.strip() for ln in tail.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    if tail_lines and re.match(r"^login\s+incorrect\s*$", tail_lines[-1], re.IGNORECASE):
        return True
    return False


# Base64 line: only valid base64 chars (BusyBox may wrap at 76 chars)
_BASE64_LINE = re.compile(r"^[A-Za-z0-9+/=]+$")

# Shell prompt at end of output (device ready for next command)
# Require start-of-buffer or newline before VMC so "fooVMC1234>" mid-line does not complete early.
_UART_PROMPT_AT_END = re.compile(r"(?:^|[\r\n])VMC\d+\s*>\s*$", re.IGNORECASE)
# Prompt at start of line (device may prefix base64 output with e.g. VMC3073> )
_UART_PROMPT_AT_START = re.compile(r"^\s*VMC\d+\s*>\s*", re.IGNORECASE)


def _uart_command_response_complete(decoded: str) -> bool:
    """
    True when the UART buffer ends with a known interactive prompt (command finished).
    Covers VMC####> (legacy), E3 [ipc]#, Linux # prompts, and AmebaPro2-style > lines.
    """
    if not decoded:
        return False
    if _UART_PROMPT_AT_END.search(decoded):
        return True
    if re.search(r"\[ipc\]#\s*$", decoded):
        return True
    # Line ending with # (BusyBox/root), e.g. "#", " ~ #", "root@host:/#"
    if re.search(r"(?:^|[\r\n])[^\r\n]{0,160}#\s*$", decoded):
        return True
    # Some AmebaPro2 consoles: last line ends with >
    if re.search(r"(?:^|[\r\n])[^\r\n]{0,160}>\s*$", decoded):
        return True
    return False


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


def _garbage_ratio(text: str) -> float:
    """Fraction of non-printable chars (excluding common whitespace). High => likely wrong baud."""
    if not text:
        return 0.0
    bad = 0
    for ch in text:
        o = ord(ch)
        if ch in "\r\n\t":
            continue
        if o < 32 or o == 127:
            bad += 1
    return bad / max(len(text), 1)


class UARTHandler:
    """Handle UART/serial connect, disconnect, and command execution (send command, read response)."""

    def __init__(self) -> None:
        self._serial: Any = None
        self._port: str | None = None
        self._baud: int = 115200
        self._connected = False
        self._console_style: str = "linux_shell"  # linux_shell | amebapro2 | mcu

    def connect(
        self,
        port: str,
        baud_rate: int = 115200,
        *,
        console_style: str | None = None,
        device_display_name: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """
        Open serial port at given baud rate. Returns (success, message, settings_for_config).

        console_style:
          - linux_shell (default): BusyBox/Linux login (root/arlo) then echo verify.
          - amebapro2: no login; verify with `build_info` and AGW_MODEL_ID / model token.
          - mcu: Gen5 MCU CLI; no login; open port and drain (no shell echo verify).
        """
        if not _SERIAL_AVAILABLE:
            return False, "pyserial is required for UART. Install with: pip install pyserial", None
        style = (console_style or "linux_shell").strip().lower()
        if style not in ("linux_shell", "amebapro2", "mcu"):
            style = "linux_shell"
        dev_label = (device_display_name or "device").strip()

        def _try_once(baud: int) -> tuple[bool, str | None]:
            try:
                ser = serial.Serial(
                    port=port,
                    baudrate=baud,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.5,
                    write_timeout=5,
                )
            except serial.SerialException as e:
                return False, str(e)
            self._serial = ser
            self._port = port
            self._baud = baud
            self._connected = True
            self._console_style = style
            # #region agent log
            _agent_debug_ndjson(
                "H4",
                "uart_handler.py:connect",
                "serial opened",
                {
                    "port": port,
                    "baud": baud,
                    "style": style,
                    "dtr": bool(getattr(ser, "dtr", False)),
                    "rts": bool(getattr(ser, "rts", False)),
                },
            )
            # #endregion
            if style == "linux_shell":
                self._uart_do_login()
                if not self._uart_verify_connection():
                    return False, None
            elif style == "amebapro2":
                if not self._uart_verify_amebapro2():
                    return False, None
            else:
                if not self._uart_verify_mcu():
                    return False, None
            return True, None

        try:
            ok, err = _try_once(baud_rate)

            if not ok:
                try:
                    if self._serial:
                        self._serial.close()
                except Exception:
                    pass
                self._serial = None
                self._port = None
                self._connected = False
                detail = (
                    (err or "").strip()
                    or "Connection could not be verified. Check baud rate and cable, then try again."
                )
                # #region agent log
                _agent_debug_ndjson(
                    "H3",
                    "uart_handler.py:connect",
                    "uart connect failed",
                    {"detail": (detail or "")[:400], "err": (err or "")[:200]},
                )
                # #endregion
                return False, detail, None

            msg = f"Connected to {port} at {self._baud} baud"
            return True, msg, {
                "port": port,
                "baud_rate": self._baud,
                "console_style": style,
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
        saw_login = False
        sent_username = False
        saw_password = False
        sent_password = False
        pre_drain = ""
        err_note = ""
        in_waiting_before_reset = 0
        try:
            in_waiting_before_reset = int(self._serial.in_waiting or 0)
            self._serial.reset_input_buffer()
            # Wait for "login:" prompt and send username (allow time for device to boot)
            login_chunks: list[bytes] = []
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                if self._serial.in_waiting:
                    data = self._serial.read(self._serial.in_waiting)
                    if data:
                        login_chunks.append(data)
                        text = b"".join(login_chunks).decode(errors="replace")
                        if re.search(r"login\s*:?\s*", text, re.IGNORECASE):
                            saw_login = True
                            self._serial.write((UART_CONSOLE_LOGIN + "\r\n").encode())
                            self._serial.flush()
                            sent_username = True
                            break
                time.sleep(0.03)
            # Wait for "Password:" and send password
            chunks = list(login_chunks)
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                if self._serial.in_waiting:
                    data = self._serial.read(self._serial.in_waiting)
                    if data:
                        chunks.append(data)
                        text = b"".join(chunks).decode(errors="replace")
                        if _UART_PASSWORD_PROMPT.search(text) or re.search(r"password\s*:\s*", text, re.IGNORECASE):
                            saw_password = True
                            self._serial.write((UART_CONSOLE_PASSWORD + "\r\n").encode())
                            self._serial.flush()
                            sent_password = True
                            break
                time.sleep(0.03)
            pre_drain = b"".join(chunks).decode(errors="replace")
            # Drain remainder so the first command sees a clean shell prompt
            time.sleep(0.12)
            while self._serial.in_waiting:
                self._serial.read(self._serial.in_waiting)
                time.sleep(0.03)
        except Exception as ex:
            err_note = type(ex).__name__
        finally:
            # #region agent log
            tail = (pre_drain[-280:] if pre_drain else "").replace("\r", "\\r").replace("\n", "\\n")
            _agent_debug_ndjson(
                "H1-H2-H5",
                "uart_handler.py:_uart_do_login",
                "login sequence complete",
                {
                    "in_waiting_before_reset": in_waiting_before_reset,
                    "saw_login": saw_login,
                    "sent_username": sent_username,
                    "saw_password": saw_password,
                    "sent_password": sent_password,
                    "buffer_len": len(pre_drain),
                    "has_username_prompt": bool(re.search(r"username\s*:", pre_drain, re.IGNORECASE)),
                    "tail_preview": tail[:220],
                    "error": err_note,
                },
            )
            # #endregion

    def _uart_break_echo_used_as_login_name(self, verify_token: str) -> str:
        """
        When verify sends `echo TOKEN` while the UART is at login:, BusyBox treats the line as the
        username and prompts for Password. Sending root's password then fails (wrong user). Submit a
        bogus password so we return to `login:`, then callers can send root/arlo.
        """
        if not self._serial:
            return ""
        try:
            self._serial.write(b"__uart_not_a_user_password__\r\n")
            self._serial.flush()
        except Exception:
            return ""
        time.sleep(0.22)
        pre = ""
        t_end = time.monotonic() + 0.9
        while time.monotonic() < t_end:
            if self._serial.in_waiting:
                pre += self._serial.read(self._serial.in_waiting).decode(errors="replace")
            else:
                time.sleep(0.04)
        return pre

    def _uart_respond_to_auth_prompts(self, seed: str = "", deadline_s: float = 3.0) -> None:
        """Send root/arlo when the buffer shows BusyBox login: or Password: (no initial reset)."""
        if not self._serial:
            return
        deadline = time.monotonic() + float(deadline_s)
        buf = seed
        while time.monotonic() < deadline:
            if self._serial.in_waiting:
                buf += self._serial.read(self._serial.in_waiting).decode(errors="replace")
            sent = False
            if re.search(r"(?:^|[\r\n])\s*login\s*:\s*$", buf, re.IGNORECASE | re.MULTILINE):
                self._serial.write((UART_CONSOLE_LOGIN + "\r\n").encode())
                self._serial.flush()
                buf = ""
                sent = True
            elif _UART_PASSWORD_PROMPT.search(buf) or re.search(
                r"(?:^|[\r\n])\s*password\s*:\s*$", buf, re.IGNORECASE | re.MULTILINE
            ):
                self._serial.write((UART_CONSOLE_PASSWORD + "\r\n").encode())
                self._serial.flush()
                buf = ""
                sent = True
            if sent:
                time.sleep(0.08)
                continue
            if not self._serial.in_waiting:
                time.sleep(0.04)
        time.sleep(0.1)
        while self._serial.in_waiting:
            self._serial.read(self._serial.in_waiting)
            time.sleep(0.02)

    def _uart_verify_connection(self) -> bool:
        """Send a test command and check the response. Returns False if baud rate or link is wrong."""
        if not self._serial:
            return False
        verify_token = "UART_VERIFY_7X9K"
        try:
            for attempt in range(3):
                self._serial.reset_input_buffer()
                self._serial.write(f"echo {verify_token}\r\n".encode())
                self._serial.flush()
                chunks: list[bytes] = []
                deadline = time.monotonic() + 2.5
                last_data = time.monotonic()
                while time.monotonic() < deadline:
                    if self._serial.in_waiting:
                        data = self._serial.read(self._serial.in_waiting)
                        if data:
                            chunks.append(data)
                            last_data = time.monotonic()
                    else:
                        if time.monotonic() - last_data >= 0.35 and chunks:
                            break
                        time.sleep(0.03)
                raw = b"".join(chunks).decode(errors="replace")
                while self._serial.in_waiting:
                    raw += self._serial.read(self._serial.in_waiting).decode(errors="replace")
                stuck = _uart_buffer_shows_login_or_password_prompt(raw)
                ok = verify_token in raw and not stuck
                # #region agent log
                rt = raw[-320:] if raw else ""
                rt_safe = rt.replace("\r", "\\r").replace("\n", "\\n")
                _agent_debug_ndjson(
                    "H3",
                    "uart_handler.py:_uart_verify_connection",
                    f"verify attempt {attempt}",
                    {
                        "attempt": attempt,
                        "stuck": stuck,
                        "token_present": verify_token in raw,
                        "raw_len": len(raw),
                        "tail_preview": rt_safe[:220],
                        "garbage_ratio": round(_garbage_ratio(raw), 4),
                    },
                )
                # #endregion
                if ok:
                    return True
                if stuck:
                    if verify_token in raw:
                        pre = self._uart_break_echo_used_as_login_name(verify_token)
                        self._uart_respond_to_auth_prompts(seed=pre, deadline_s=3.0)
                    else:
                        self._uart_respond_to_auth_prompts(seed=raw, deadline_s=3.0)
                    continue
                # Partial read (silence before full echo) is not a failure — retry.
                if attempt < 2:
                    continue
                return False
            return False
        except Exception:
            return False

    def _uart_verify_amebapro2(self) -> bool:
        """ISP console: send build_info; accept AGW_MODEL_ID or VMC/AVD token, reject heavy garbage."""
        if not self._serial:
            return False
        try:
            self._serial.reset_input_buffer()
            self._serial.write(b"build_info\r\n")
            self._serial.flush()
            chunks: list[bytes] = []
            deadline = time.monotonic() + 3.0
            last_data = time.monotonic()
            while time.monotonic() < deadline:
                if self._serial.in_waiting:
                    data = self._serial.read(self._serial.in_waiting)
                    if data:
                        chunks.append(data)
                        last_data = time.monotonic()
                else:
                    if time.monotonic() - last_data >= 0.45 and chunks:
                        break
                    time.sleep(0.03)
            raw = b"".join(chunks).decode(errors="replace")
            if _garbage_ratio(raw) > 0.08 and len(raw) > 40:
                return False
            if re.search(r"AGW_MODEL_ID", raw, re.IGNORECASE):
                return True
            if re.search(r"\bVMC\d{4}[A-Z]?\b", raw, re.IGNORECASE):
                return True
            if re.search(r"\bAVD\d{4}\b", raw, re.IGNORECASE):
                return True
            return len(raw.strip()) >= 8 and _garbage_ratio(raw) <= 0.05
        except Exception:
            return False

    def _uart_verify_mcu(self) -> bool:
        """Gen5 MCU UART: no Linux shell; drain boot noise then treat link as ready."""
        if not self._serial:
            return False
        try:
            self._serial.reset_input_buffer()
            end = time.monotonic() + 0.22
            while time.monotonic() < end:
                if self._serial.in_waiting:
                    data = self._serial.read(self._serial.in_waiting)
                    if data:
                        text = data.decode(errors="replace")
                        if len(text) > 80 and _garbage_ratio(text) > 0.15:
                            return False
                time.sleep(0.03)
            return True
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

    def _mark_uart_dead(self) -> None:
        self._connected = False
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        self._port = None

    def transport_heartbeat(self) -> bool:
        """Return False if the serial port is closed, errors on access, or COM no longer exists."""
        if not self._connected or not self._serial or not self._port:
            return False
        if not _SERIAL_AVAILABLE:
            return False
        try:
            if not self._serial.is_open:
                self._mark_uart_dead()
                return False
            _ = self._serial.in_waiting
        except Exception:
            self._mark_uart_dead()
            return False
        try:
            if list_ports is not None:
                present: set[str] = set()
                for info in list_ports.comports():
                    dev = getattr(info, "device", None) or getattr(info, "name", None)
                    if isinstance(info, (list, tuple)) and len(info) >= 1 and dev is None:
                        dev = info[0]
                    if dev:
                        present.add(str(dev).upper())
                my_key = _port_key_for_match(self._port)
                if present:
                    keys = {_port_key_for_match(x) for x in present}
                    if my_key not in keys:
                        if sys.platform == "win32" and _uart_probe_windows_port_exists(self._port):
                            pass
                        else:
                            self._mark_uart_dead()
                            return False
                elif sys.platform == "win32" and not _uart_probe_windows_port_exists(self._port):
                    self._mark_uart_dead()
                    return False
        except Exception:
            pass
        return True

    def execute(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        timeout_sec: float | None = None,
    ) -> tuple[bool, str]:
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
            # FOTA helpers can run tens of seconds with sparse serial output; short idle_timeout
            # makes us return before the shell prompt, leaving the device busy for the next command.
            low = full_cmd.lower()
            is_fw_shell_long = (
                not (is_tar or is_tftp or is_base64)
                and (
                    "update_refresh" in low
                    or "update_url" in low
                    or low.strip() == "arlocmd reboot"
                    or low.strip().startswith("arlocmd reboot ")
                )
            )
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
            elif is_fw_shell_long:
                total_timeout = max(300.0, float(timeout_sec or 0) or 300.0)
                idle_timeout = 45.0
            else:
                total_timeout = 120.0
                idle_timeout = 12.0
            if timeout_sec is not None and timeout_sec > 0 and not is_tar and not is_tftp and not is_base64:
                total_timeout = max(total_timeout, float(timeout_sec))
            _style = getattr(self, "_console_style", "linux_shell")
            if (
                _style in ("amebapro2", "mcu")
                and not (is_tar or is_tftp or is_base64)
                and not is_fw_shell_long
            ):
                idle_timeout = 2.0
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
                        if _uart_execute_raw_shows_unauthenticated_state(decoded):
                            break
                        # Done when we see a known shell prompt (command finished)
                        if _uart_command_response_complete(decoded):
                            break
                else:
                    # Stop after idle_timeout with no new bytes — including when the device
                    # never responded (chunks empty), so we do not burn full total_timeout (120s)
                    # per detect_device command.
                    if time.monotonic() - last_data >= idle_timeout:
                        break
                    time.sleep(0.05)
            if (is_tar or is_tftp) and print_offset > 0:
                sys.stdout.write("\n")
                sys.stdout.flush()
            raw = b"".join(chunks).decode(errors="replace").strip()
            if _uart_execute_raw_shows_unauthenticated_state(raw):
                self._mark_uart_dead()
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
            if _uart_execute_raw_shows_unauthenticated_state(output):
                self._mark_uart_dead()
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
