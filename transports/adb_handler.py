"""ADB connection handler using subprocess. Uses USB connection and adb shell auth + password."""
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from utils.logger import get_logger

logger = get_logger()


@dataclass(frozen=True)
class AdbPickerDeviceInfo:
    """One row in the ADB device picker (queried before the dialog opens)."""

    serial: str
    model: str
    firmware: str | None


def _parse_adb_devices_stdout(stdout: str) -> list[str]:
    """Parse `adb devices` stdout; return serials from each data line (same rules as historical connect)."""
    lines = (stdout or "").strip().splitlines()
    devices: list[str] = []
    for line in lines[1:]:  # skip "List of devices attached"
        line = line.strip()
        if line and not line.startswith("*") and "\t" in line:
            serial = line.split("\t")[0].strip()
            if serial:
                devices.append(serial)
    return devices


def _adb_getprop(adb: str, serial: str, prop: str, timeout: float) -> str:
    try:
        if timeout <= 0:
            return ""
        r = subprocess.run(
            [adb, "-s", serial, "shell", "getprop", prop],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (r.stdout or "").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


class ADBHandler:
    """Handle ADB via USB: adb shell auth (with password), then adb shell for commands."""

    def __init__(self) -> None:
        self._device_serial: str | None = None  # from adb devices
        self._connected = False

    @staticmethod
    def list_attached_usb_serials() -> list[str]:
        """Return device serials reported by `adb devices` (same lines the connect flow considers attached)."""
        try:
            result = subprocess.run(
                [ADBHandler._adb_cmd(), "devices"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return _parse_adb_devices_stdout(result.stdout or "")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

    @staticmethod
    def probe_device_row_detail(serial: str, timeout: float = 1.0) -> str:
        """
        Best-effort subtitle for device picker (Model / FW). Uses `cli mfg build_info` with a short timeout;
        does not run auth. Returns empty string if unavailable.
        """
        adb = ADBHandler._adb_cmd()
        try:
            r = subprocess.run(
                [adb, "-s", serial, "shell", "cli", "mfg", "build_info"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            text = (r.stdout or "") + "\n" + (r.stderr or "")
            if not text.strip():
                return ""
            from core.build_info import parse_build_info

            parsed = parse_build_info(text)
            model = parsed.get("model")
            fw = parsed.get("fw_version")
            parts: list[str] = []
            if model:
                parts.append(f"Model: {model}")
            if fw:
                parts.append(f"FW: {fw}")
            if parts:
                return "  ".join(parts)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        except Exception:
            logger.debug("ADB probe_device_row_detail failed for %s", serial, exc_info=True)
        return ""

    @staticmethod
    def _probe_picker_info_for_serial(serial: str, budget: float) -> AdbPickerDeviceInfo:
        """Fill model (getprop + optional build_info) and FW within ``budget`` seconds (wall)."""
        adb = ADBHandler._adb_cmd()
        deadline = time.monotonic() + max(0.15, budget)
        model = ""
        fw: str | None = None

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        t0 = min(1.2, remaining())
        model = _adb_getprop(adb, serial, "ro.product.model", t0)
        if not model and remaining() > 0.08:
            model = _adb_getprop(adb, serial, "ro.product.device", min(0.55, remaining()))
        if not model and remaining() > 0.08:
            model = _adb_getprop(adb, serial, "ro.hardware", min(0.45, remaining()))

        if remaining() >= 0.35:
            try:
                t_left = min(1.25, remaining())
                r = subprocess.run(
                    [adb, "-s", serial, "shell", "cli", "mfg", "build_info"],
                    capture_output=True,
                    text=True,
                    timeout=t_left,
                )
                text = (r.stdout or "") + "\n" + (r.stderr or "")
                if text.strip():
                    from core.build_info import parse_build_info

                    parsed = parse_build_info(text)
                    if not model and parsed.get("model"):
                        model = str(parsed["model"]).strip()
                    if parsed.get("fw_version"):
                        fw = str(parsed["fw_version"]).strip() or None
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass
            except Exception:
                logger.debug("picker build_info probe failed for %s", serial, exc_info=True)

        display_model = model.strip() if model else "Unknown model"
        return AdbPickerDeviceInfo(serial=serial, model=display_model, firmware=fw)

    @staticmethod
    def gather_picker_device_infos(
        serials: list[str], *, per_serial_timeout: float = 2.0
    ) -> list[AdbPickerDeviceInfo]:
        """
        Query each serial in parallel (getprop + quick build_info). Per-device wall time is capped
        by ``per_serial_timeout`` (default 2s).
        """
        if not serials:
            return []
        budget = max(0.5, float(per_serial_timeout))
        max_workers = min(8, len(serials))

        def one(s: str) -> AdbPickerDeviceInfo:
            return ADBHandler._probe_picker_info_for_serial(s, budget)

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            return list(ex.map(one, serials))

    def connect(
        self, password: str, device_serial: str | None = None
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """
        Run 'adb shell auth' with password for the chosen USB device, then mark connected.
        If device_serial is None, uses the only attached device; fails if more than one is attached.
        Returns (success, message, settings_for_config).
        """
        try:
            devices = self.list_attached_usb_serials()
            if not devices:
                return False, "No ADB device found. Connect the camera via USB and ensure USB debugging is enabled.", None

            if device_serial and device_serial.strip():
                serial = device_serial.strip()
                if serial not in devices:
                    return (
                        False,
                        f"ADB device {serial!r} is not connected or not authorized.",
                        None,
                    )
            else:
                if len(devices) > 1:
                    return (
                        False,
                        "Multiple ADB devices attached; select one in the dialog.",
                        None,
                    )
                serial = devices[0]

            self._device_serial = serial

            # Run adb shell auth and send password on stdin
            proc = subprocess.Popen(
                [self._adb_cmd(), "-s", serial, "shell", "auth"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
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

    def execute(self, command: str, args: list[str] | None = None) -> tuple[bool, str]:
        """Run a command on the device via adb shell. Returns (success, output_or_error)."""
        if not self._connected or not self._device_serial:
            return False, "Not connected."
        try:
            # adb shell takes a single command string; join command + args
            shell_cmd = command if not args else (command + " " + " ".join(args))
            cmd = [self._adb_cmd(), "-s", self._device_serial, "shell", shell_cmd]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
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
                proc = subprocess.Popen(cmd, shell=True, stdout=f, stderr=subprocess.DEVNULL)
                setattr(self, "_tail_process", proc)
                setattr(self, "_tail_file", f)
                setattr(self, "_tail_reader_thread", None)
                return True, ""

            proc = subprocess.Popen(
                cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
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
