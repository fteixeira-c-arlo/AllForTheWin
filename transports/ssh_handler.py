"""SSH connection handler using paramiko."""
import subprocess
import threading
from typing import Any, Callable

import paramiko
from paramiko import SSHException

from utils.logger import get_logger
from utils.subprocess_helpers import win_subprocess_kwargs

logger = get_logger()


class SSHHandler:
    """Handle SSH connect/disconnect and optional command execution."""

    def __init__(self) -> None:
        self._client: paramiko.SSHClient | None = None
        self._device_id: str | None = None  # ip:port
        self._host: str | None = None
        self._port: int = 22
        self._username: str = "root"
        self._connected = False

    def connect(
        self,
        ip_address: str,
        port: int = 22,
        username: str = "root",
        password: str = "",
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """
        Establish SSH connection. Returns (success, message, settings_for_config).
        """
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=ip_address,
                port=port,
                username=username,
                password=password or None,
                timeout=10,
                allow_agent=False,
                look_for_keys=False,
            )
            self._client = client
            self._device_id = f"{ip_address}:{port}"
            self._host = ip_address
            self._port = port
            self._username = username
            self._connected = True
            return True, f"Connected to {ip_address}:{port}", {
                "ip_address": ip_address,
                "port": port,
                "username": username,
            }
        except SSHException as e:
            err = str(e).lower()
            if "authentication" in err or "auth" in err:
                return False, "Authentication failed. Check username/password or SSH keys.", None
            if "timeout" in err or "timed out" in err:
                return False, "Connection timeout. Please check device IP and network connectivity.", None
            return False, str(e), None
        except OSError as e:
            err = str(e).lower()
            if "timed out" in err or "timeout" in err:
                return False, "Connection timeout. Please check device IP and network connectivity.", None
            if "refused" in err or "errno 111" in err or "errno 10061" in err:
                return False, "Connection refused. Ensure the device is powered on and accepting connections.", None
            return False, str(e), None
        except Exception as e:
            logger.exception("SSH connect error")
            return False, str(e), None

    def disconnect(self) -> None:
        """Close SSH connection."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self._device_id = None
        self._connected = False

    def execute(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        timeout_sec: float | None = None,
    ) -> tuple[bool, str]:
        """
        Run command over SSH. Returns (success, output_or_error).
        Returns (False, "Device disconnected.") when the connection was lost.
        """
        if not self._connected or not self._client:
            return False, "Not connected."
        try:
            full_cmd = command
            if args:
                full_cmd = f"{command} {' '.join(args)}"
            tmo = 30.0 if timeout_sec is None else max(1.0, float(timeout_sec))
            _, stdout, stderr = self._client.exec_command(full_cmd, timeout=int(tmo))
            out = (stdout.read().decode() or stderr.read().decode() or "").strip()
            return True, out or "OK"
        except Exception as e:
            err = str(e).lower()
            if _is_ssh_disconnect_error(err):
                self._connected = False
                self._client = None
                self._device_id = None
                return False, "Device disconnected."
            return False, str(e)

    def pull_file(self, remote_path: str, local_path: str) -> tuple[bool, str]:
        """Pull a file from the device via SFTP. Returns (success, message)."""
        if not self._connected or not self._client:
            return False, "Not connected."
        try:
            sftp = self._client.open_sftp()
            sftp.get(remote_path, local_path)
            sftp.close()
            return True, f"Downloaded to {local_path}"
        except Exception as e:
            return False, str(e)

    def is_connected(self) -> bool:
        return self._connected

    def transport_heartbeat(self) -> bool:
        """Return False if the SSH session is no longer active."""
        if not self._connected or not self._client:
            return False
        try:
            transport = self._client.get_transport()
            if transport is None or not transport.is_active():
                self._connected = False
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
                self._device_id = None
                return False
            return True
        except Exception:
            self._connected = False
            self._client = None
            self._device_id = None
            return False

    def device_identifier(self) -> str | None:
        return self._device_id

    def get_tail_logs_command(self) -> str | None:
        """Return shell command to run tail -f system log in another terminal, or None if not connected."""
        if not self._connected or not self._host:
            return None
        return (
            f"ssh -o StrictHostKeyChecking=no -p {self._port} "
            f"{self._username}@{self._host} \"sh -c 'tail -f /tmp/logs/system-log_V1_0'\""
        )

    def start_tail_logs_to_file(
        self, log_path: str, line_callback: Callable[[str], None] | None = None
    ) -> tuple[bool, str]:
        """Start streaming tail -f to a file. Optional line_callback(line) for each line. Use stop_tail_logs() to stop."""
        cmd = self.get_tail_logs_command()
        if not cmd:
            return False, "Not connected."
        try:
            f = open(log_path, "ab")
            if line_callback is None:
                proc = subprocess.Popen(
                    cmd,
                    shell=True,
                    stdout=f,
                    stderr=subprocess.DEVNULL,
                    **win_subprocess_kwargs(),
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
                **win_subprocess_kwargs(),
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
        except Exception as e:
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


def _is_ssh_disconnect_error(error_lower: str) -> bool:
    """Return True if the exception indicates the SSH connection was lost."""
    return (
        "connection reset" in error_lower
        or "connection lost" in error_lower
        or "socket is closed" in error_lower
        or "eof occurred" in error_lower
        or "broken pipe" in error_lower
        or "connection refused" in error_lower
        or "timed out" in error_lower
    )
