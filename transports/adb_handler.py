"""ADB connection handler using subprocess. Uses USB connection and adb shell auth + password."""
import subprocess
import threading
from typing import Any, Callable

from utils.logger import get_logger

logger = get_logger()


class ADBHandler:
    """Handle ADB via USB: adb shell auth (with password), then adb shell for commands."""

    def __init__(self) -> None:
        self._device_serial: str | None = None  # from adb devices
        self._connected = False

    def connect(self, password: str) -> tuple[bool, str, dict[str, Any] | None]:
        """
        Ensure a single USB device is present, run 'adb shell auth' and send password,
        then mark connected. Returns (success, message, settings_for_config).
        No IP is used; connection is over USB.
        """
        try:
            # Get attached devices (USB)
            result = subprocess.run(
                [self._adb_cmd(), "devices"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            lines = (result.stdout or "").strip().splitlines()
            devices = []
            for line in lines[1:]:  # skip "List of devices attached"
                line = line.strip()
                if line and not line.startswith("*") and "\t" in line:
                    serial = line.split("\t")[0].strip()
                    if serial:
                        devices.append(serial)
            if not devices:
                return False, "No ADB device found. Connect the camera via USB and ensure USB debugging is enabled.", None
            if len(devices) > 1:
                return False, f"Multiple ADB devices found: {devices}. Disconnect others or use a single device.", None

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
