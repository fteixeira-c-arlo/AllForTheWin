"""ADB connection handler using subprocess. Uses USB connection and adb shell auth + password."""
import subprocess
import threading
import time
from typing import Any, Callable

from utils.logger import get_logger
from utils.subprocess_helpers import win_subprocess_kwargs

logger = get_logger()

_WSK = win_subprocess_kwargs

# Post–debug-mode connect: wait for `adb devices` to show state `device`.
ADB_WAIT_TIMEOUT_SEC = 30.0
ADB_WAIT_POLL_SEC = 1.5

_MSG_UNAUTHORIZED = (
    "Device found but not authorized. Check the device screen and tap 'Allow' if prompted, then retry."
)
_MSG_OFFLINE = "Device is offline. Try unplugging and reconnecting the USB cable."
_MSG_NOT_FOUND = (
    "Device not found after 30 seconds. Make sure you pressed the sync button 6 times "
    "and the USB cable is connected."
)


def parse_adb_devices_lines(stdout: str) -> list[tuple[str, str]]:
    """
    Parse `adb devices` stdout into (serial, state) pairs.
    Skips the header line, daemon noise lines, and empty rows. State is lowercased (device, offline, …).
    """
    out: list[tuple[str, str]] = []
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("*"):
            continue
        if line.lower() == "list of devices attached":
            continue
        if "\t" not in line:
            continue
        parts = line.split("\t")
        serial = (parts[0] or "").strip()
        rest = (parts[1] or "").strip()
        state = (rest.split()[0] if rest else "").strip().lower()
        if serial:
            out.append((serial, state))
    return out


def _parse_adb_devices_stdout(stdout: str) -> list[str]:
    """Serial numbers with ADB state ``device`` only (ready for shell/auth)."""
    return [s for s, st in parse_adb_devices_lines(stdout) if st == "device"]


class ADBHandler:
    """Handle ADB via USB: adb shell auth (with password), then adb shell for commands."""

    def __init__(self) -> None:
        self._device_serial: str | None = None  # from adb devices
        self._connected = False

    @staticmethod
    def list_attached_usb_serials() -> list[str]:
        """Return serials in ``device`` state (ready for ``adb shell``), from `adb devices`."""
        try:
            result = subprocess.run(
                [ADBHandler._adb_cmd(), "devices"],
                capture_output=True,
                text=True,
                timeout=10,
                **_WSK(),
            )
            return _parse_adb_devices_stdout(result.stdout or "")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

    @staticmethod
    def _run_adb_devices() -> str:
        try:
            result = subprocess.run(
                [ADBHandler._adb_cmd(), "devices"],
                capture_output=True,
                text=True,
                timeout=10,
                **_WSK(),
            )
            return result.stdout or ""
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""

    def wait_for_adb_device(
        self,
        device_serial: str | None,
        *,
        log_callback: Callable[[str], None] | None = None,
        timeout_sec: float = ADB_WAIT_TIMEOUT_SEC,
        poll_sec: float = ADB_WAIT_POLL_SEC,
    ) -> tuple[bool, str | None, str]:
        """
        Poll until a usable device appears (state ``device``) or timeout.

        Returns (success, chosen_serial, error_message). On success error_message is "".
        """
        log = log_callback or (lambda _m: None)
        deadline = time.monotonic() + timeout_sec
        preferred = (device_serial or "").strip() or None
        warned_unauthorized = False
        last_issue: str | None = None

        log("Waiting for device…\n")

        while time.monotonic() < deadline:
            lines = parse_adb_devices_lines(self._run_adb_devices())
            by_serial = {s: st for s, st in lines}
            ready = [s for s, st in lines if st == "device"]

            if preferred:
                st = by_serial.get(preferred, "")
                if st == "device":
                    return True, preferred, ""
                if st == "unauthorized":
                    last_issue = "unauthorized"
                    if not warned_unauthorized:
                        log(_MSG_UNAUTHORIZED + "\n")
                        warned_unauthorized = True
                elif st == "offline":
                    last_issue = "offline"
                    log(_MSG_OFFLINE + "\n")
            else:
                if len(ready) > 1:
                    return (
                        False,
                        None,
                        "Multiple ADB devices attached; select one in the connect dialog.",
                    )
                if len(ready) == 1:
                    return True, ready[0], ""
                for _s, st in lines:
                    if st == "unauthorized":
                        last_issue = "unauthorized"
                        if not warned_unauthorized:
                            log(_MSG_UNAUTHORIZED + "\n")
                            warned_unauthorized = True
                        break
                    if st == "offline":
                        last_issue = "offline"
                        log(_MSG_OFFLINE + "\n")

            time.sleep(poll_sec)

        if last_issue == "unauthorized":
            return False, None, _MSG_UNAUTHORIZED
        if last_issue == "offline":
            return False, None, _MSG_OFFLINE
        return False, None, _MSG_NOT_FOUND

    def connect(
        self,
        password: str,
        device_serial: str | None = None,
        *,
        log_callback: Callable[[str], None] | None = None,
        skip_wait: bool = False,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """
        Run 'adb shell auth' with password for the chosen USB device, then mark connected.
        If device_serial is None, uses the only attached device in ``device`` state; fails if more than one.
        Polls `adb devices` until a device is ready unless skip_wait is True.
        Returns (success, message, settings_for_config).
        """
        try:
            serial: str | None = None
            if skip_wait:
                devices = self.list_attached_usb_serials()
                if not devices:
                    return (
                        False,
                        "No ADB device found. Connect the camera via USB and ensure USB debugging is enabled.",
                        None,
                    )
                pref = (device_serial or "").strip() or None
                if pref:
                    if pref not in devices:
                        return (
                            False,
                            f"ADB device {pref!r} is not connected or not authorized.",
                            None,
                        )
                    serial = pref
                else:
                    if len(devices) > 1:
                        return (
                            False,
                            "Multiple ADB devices attached; select one in the dialog.",
                            None,
                        )
                    serial = devices[0]
            else:
                ok, chosen, err = self.wait_for_adb_device(
                    (device_serial or "").strip() or None,
                    log_callback=log_callback,
                )
                if not ok or not chosen:
                    return False, err, None
                serial = chosen

            self._device_serial = serial

            lc = log_callback
            if lc:
                lc("Device found, authenticating…\n")

            # Run adb shell auth and send password on stdin
            proc = subprocess.Popen(
                [self._adb_cmd(), "-s", serial, "shell", "auth"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **_WSK(),
            )
            out, err = proc.communicate(input=password + "\n", timeout=15)
            combined = (out or "").strip() + (err or "").strip()

            if proc.returncode != 0:
                if "auth" in combined.lower() or "password" in combined.lower() or "fail" in combined.lower():
                    return False, "Authentication failed. Check the password.", None
                return False, combined or f"auth failed (exit code {proc.returncode})", None

            self._connected = True
            return True, f"Connected to device {serial} (USB)", {
                "device_serial": serial,
                "connection": "USB",
            }
        except FileNotFoundError:
            return False, "ADB not found. Install Android SDK platform-tools and add 'adb' to PATH.", None
        except subprocess.TimeoutExpired:
            return False, "Authentication timeout. Check the device and try again.", None
        except Exception as e:
            logger.exception("ADB connect error")
            return False, str(e), None

    def disconnect(self) -> None:
        """Clear connection state. No adb disconnect for USB."""
        self._device_serial = None
        self._connected = False

    def execute(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        timeout_sec: float | None = None,
    ) -> tuple[bool, str]:
        """Run a command on the device via adb shell. Returns (success, output_or_error)."""
        if not self._connected or not self._device_serial:
            return False, "Not connected."
        try:
            # adb shell takes a single command string; join command + args
            shell_cmd = command if not args else (command + " " + " ".join(args))
            cmd = [self._adb_cmd(), "-s", self._device_serial, "shell", shell_cmd]
            tmo = 30.0 if timeout_sec is None else max(1.0, float(timeout_sec))
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=tmo, **_WSK()
            )
            stderr = (result.stderr or "").strip().lower()
            stdout = (result.stdout or "").strip()
            out = stdout or (result.stderr or "").strip()
            # Detect device disconnected (unplugged from USB or offline)
            if _is_adb_disconnect_error(stderr, result.returncode):
                self._connected = False
                self._device_serial = None
                return False, "Device disconnected."
            return result.returncode == 0, out or ("OK" if result.returncode == 0 else "Command failed")
        except Exception as e:
            err = str(e).lower()
            if "device" in err and ("offline" in err or "not found" in err):
                self._connected = False
                self._device_serial = None
                return False, "Device disconnected."
            return False, str(e)

    def pull_file(self, remote_path: str, local_path: str) -> tuple[bool, str]:
        """Pull a file from the device via adb pull. Returns (success, message)."""
        if not self._connected or not self._device_serial:
            return False, "Not connected."
        try:
            result = subprocess.run(
                [self._adb_cmd(), "-s", self._device_serial, "pull", remote_path, local_path],
                capture_output=True,
                text=True,
                timeout=120,
                **_WSK(),
            )
            if result.returncode == 0:
                return True, f"Downloaded to {local_path}"
            return False, (result.stderr or result.stdout or "Pull failed.").strip()
        except Exception as e:
            return False, str(e)

    def is_connected(self) -> bool:
        return self._connected

    def transport_heartbeat(self) -> bool:
        """Return False if the USB device is gone (get-state not device). Cheap periodic check for GUI."""
        if not self._connected or not self._device_serial:
            return False
        try:
            r = subprocess.run(
                [self._adb_cmd(), "-s", self._device_serial, "get-state"],
                capture_output=True,
                text=True,
                timeout=8,
                **_WSK(),
            )
            stderr = (r.stderr or "").strip().lower()
            stdout = (r.stdout or "").strip().lower()
            if _is_adb_disconnect_error(stderr, r.returncode):
                self._connected = False
                self._device_serial = None
                return False
            if "offline" in stdout or "offline" in stderr:
                self._connected = False
                self._device_serial = None
                return False
            if r.returncode != 0:
                combined = stderr + " " + stdout
                if "no devices" in combined or "not found" in combined or "device not found" in combined:
                    self._connected = False
                    self._device_serial = None
                    return False
                return True
            if stdout == "unauthorized":
                return True
            return stdout == "device"
        except subprocess.TimeoutExpired:
            return True
        except Exception as e:
            err = str(e).lower()
            if "device" in err and ("offline" in err or "not found" in err):
                self._connected = False
                self._device_serial = None
                return False
            return True

    def device_identifier(self) -> str | None:
        return self._device_serial or None

    def get_tail_logs_command(self) -> str | None:
        """Return shell command to run tail -f system log in another terminal, or None if not connected."""
        if not self._connected or not self._device_serial:
            return None
        return (
            f"{self._adb_cmd()} -s {self._device_serial} shell sh -c 'tail -f /tmp/logs/system-log_V1_0'"
        )

    def start_tail_logs_to_file(
        self, log_path: str, line_callback: Callable[[str], None] | None = None
    ) -> tuple[bool, str]:
        """Start streaming tail -f to a file. Optional line_callback(line) for each line. Use stop_tail_logs() to stop."""
        if not self._connected or not self._device_serial:
            return False, "Not connected."
        cmd = self.get_tail_logs_command()
        if not cmd:
            return False, "Not connected."
        try:
            f = open(log_path, "ab")
            if line_callback is None:
                proc = subprocess.Popen(
                    cmd, shell=True, stdout=f, stderr=subprocess.DEVNULL, **_WSK()
                )
                setattr(self, "_tail_process", proc)
                setattr(self, "_tail_file", f)
                setattr(self, "_tail_reader_thread", None)
                return True, ""

            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                **_WSK(),
            )

            def reader() -> None:
                try:
                    assert proc.stdout is not None
                    for line_bytes in iter(proc.stdout.readline, b""):
                        if not line_bytes:
                            break
                        f.write(line_bytes)
                        f.flush()
                        try:
                            text = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                            if text:
                                line_callback(text)
                        except Exception:
                            pass
                finally:
                    try:
                        f.close()
                    except Exception:
                        pass

            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            setattr(self, "_tail_process", proc)
            setattr(self, "_tail_file", None)
            setattr(self, "_tail_reader_thread", thread)
            return True, ""
        except OSError as e:
            return False, str(e)

    def stop_tail_logs(self) -> None:
        """Stop tail stream and close the log file (logs are saved to the file)."""
        proc = getattr(self, "_tail_process", None)
        f = getattr(self, "_tail_file", None)
        thread = getattr(self, "_tail_reader_thread", None)
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                pass
            setattr(self, "_tail_process", None)
        if thread is not None:
            thread.join(timeout=3.0)
            setattr(self, "_tail_reader_thread", None)
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
            setattr(self, "_tail_file", None)

    @staticmethod
    def _adb_cmd() -> str:
        return "adb"


def _is_adb_disconnect_error(stderr_lower: str, returncode: int) -> bool:
    """Return True if stderr indicates the device was disconnected from the PC."""
    if not stderr_lower:
        return False
    return (
        "device offline" in stderr_lower
        or "no devices/emulators found" in stderr_lower
        or "device not found" in stderr_lower
        or "device '" in stderr_lower and "not found" in stderr_lower
    )


def adb_serial_transport_alive(device_serial: str, *, timeout_sec: float = 5.0) -> bool:
    """
    True if ``adb -s <serial> get-state`` indicates the USB device is still present
    (state ``device`` or ``unauthorized``). Does not mutate any ADBHandler instance.

    Used from a GUI watchdog thread while the session worker may be blocked in a long command.
    Missing ``adb`` in PATH is treated as inconclusive (True). ``get-state`` is run with a short
    timeout; expiry counts as not alive so unplugged devices do not leave the UI stuck on “connected”.
    """
    ds = (device_serial or "").strip()
    if not ds:
        return False
    try:
        r = subprocess.run(
            [ADBHandler._adb_cmd(), "-s", ds, "get-state"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            **_WSK(),
        )
    except FileNotFoundError:
        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return True
    stderr = (r.stderr or "").strip().lower()
    stdout = (r.stdout or "").strip().lower()
    if _is_adb_disconnect_error(stderr, r.returncode):
        return False
    if "offline" in stdout or "offline" in stderr:
        return False
    if r.returncode != 0:
        combined = stderr + " " + stdout
        if "no devices" in combined or "not found" in combined or "device not found" in combined:
            return False
        return True
    if stdout == "unauthorized":
        return True
    return stdout == "device"
