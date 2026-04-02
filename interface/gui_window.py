"""PySide6 main window: connect flow, command execution on a worker thread, prompt bridge."""
from __future__ import annotations

import codecs
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QFont,
    QIcon,
    QImage,
    QKeySequence,
    QPalette,
    QPixmap,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabBar,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from rich.console import Console

from core.build_info import detect_device
from core.command_definitions import load_device_commands
from core.command_parser import (
    ABSTRACT_DEFINITIONS,
    get_abstract_command_help_lines,
    get_system_commands_for_profile,
    get_tools_for_profile,
    get_visible_commands,
    parse_and_execute,
    set_tail_live_view_handlers,
)
from core.camera_models import (
    format_supported_connections,
    get_command_profile_for_model_name,
    get_model_by_name,
    get_models,
)
from transports.adb_handler import ADBHandler
from transports.ssh_handler import SSHHandler
from transports.uart_handler import UARTHandler, list_uart_ports
from transports.connection_config import ConnectionConfig
from interface.gui_bridge import GuiBridge, _SELECT_CANCELLED


DEFAULT_UART_BAUD = 115200
DEFAULT_SSH_PORT = 22

# App version (title bar, welcome). Bump when releasing.
ARLO_SHELL_VERSION = "1.0.0"
# Command input bottom border and shared UI accents (refined teal for dark UI).
ARLO_ACCENT_COLOR = "#00897B"
_STATUS_DOT_DISCONNECTED = "#e05555"
_STATUS_DOT_CONNECTING = "#e0a535"
_STATUS_DOT_CONNECTED = "#4caf7d"


def _main_window_icon_path() -> str | None:
    """Resolve app window icon to a PNG only (transparent alpha); .ico is not used here."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        candidates.append(base / "assets" / "ArloShell_icon.png")
    root = Path(__file__).resolve().parent.parent
    candidates.extend(
        [
            root / "assets" / "ArloShell_icon.png",
            root / "installer" / "arlo_icon.png",
            root / "installer" / "ArloShell_icon.png",
        ]
    )
    for rel in (
        "assets/ArloShell_icon.png",
        "installer/arlo_icon.png",
        "arlo_icon.png",
        "assets/icon.png",
    ):
        candidates.append(Path(rel))
    for p in candidates:
        try:
            if p.is_file():
                return str(p.resolve())
        except OSError:
            continue
    return None


def _load_icon(path: str) -> QIcon:
    """Load window icon preserving alpha (ARGB32). Intended for PNG assets."""
    img = QImage(path)
    if img.isNull():
        return QIcon()
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    pixmap = QPixmap.fromImage(img)
    return QIcon(pixmap)


def _e3_cli_reference_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "docs" / "e3_wired_cli_reference.md"
    return Path(__file__).resolve().parent.parent / "docs" / "e3_wired_cli_reference.md"


def _strip_rich_markup(s: str) -> str:
    if not s:
        return s
    try:
        from rich.text import Text

        return Text.from_markup(s).plain
    except Exception:
        return re.sub(r"\[/?[^\]]*]", "", s)


def _format_command_hover(name: str, meta: dict | None) -> str:
    """Plain-text tooltip for a command row (description, syntax, category, shell)."""
    m = meta or {}
    desc = _strip_rich_markup((m.get("description") or "").strip())
    parts: list[str] = []
    if desc:
        parts.append(desc)
    for label, key in (("Syntax", "syntax"), ("Category", "category"), ("Shell", "shell")):
        v = (m.get(key) or "").strip()
        if isinstance(v, str) and v:
            parts.append(f"{label}: {v}")
    if parts:
        return "\n\n".join(parts)
    return name


# --- Commands sidebar: grouping & display (visual only; dispatch keys unchanged) ---

_CMD_ROW_HEIGHT = 28

_ABSTRACT_CATEGORY_ORDER: list[tuple[str, frozenset[str]]] = [
    ("FIRMWARE", frozenset({"update url", "update check", "update apply", "update url get", "flash"})),
    ("DEVICE", frozenset({"migrate", "factory reset", "version", "info", "reboot", "serial"})),
    ("LOGS", frozenset({"log level", "log tail", "log save", "log pull"})),
    ("NETWORK", frozenset({"wifi connect"})),
    ("MANUFACTURING", frozenset({"mfg get", "mfg set", "mfg build"})),
    ("DEV", frozenset({"push arlod"})),
]

_ADV_DEVICE_CATEGORY_ORDER: tuple[str, ...] = (
    "firmware",
    "device",
    "logs",
    "network",
    "camera",
    "ptz",
    "mfg",
    "arlocmd",
    "cali",
    "kvcmd",
    "ota",
    "sv",
    "debug",
    "arlod",
)


def _display_command_label(name: str) -> str:
    return (name or "").replace("_", " ").strip()


def _abstract_args_hint(abstract_name: str) -> str:
    n = (abstract_name or "").strip()
    for d in ABSTRACT_DEFINITIONS:
        if not isinstance(d, dict):
            continue
        if (d.get("name") or "").strip() != n:
            continue
        args = d.get("args") or []
        if not args:
            return ""
        parts = []
        for a in args:
            s = str(a).strip()
            if s.endswith("?"):
                s = s[:-1]
            parts.append(f"<{s}>")
        return "  ·  " + ", ".join(parts)
    return ""


def _device_command_args_hint(meta: dict | None) -> str:
    if not meta:
        return ""
    syn = (meta.get("syntax") or "").strip()
    if "<" in syn:
        return "  ·  ···"
    return ""


def _tool_subgroup_for_system_name(name: str) -> str | None:
    disp = _display_command_label(name).lower()
    raw = name.strip().lower()
    if disp in ("fw local", "server stop", "server status"):
        return "FIRMWARE"
    if disp in ("log tail", "log tail stop", "log parse", "log parse stop", "log export"):
        return "LOGS"
    if raw.startswith("config_"):
        return "CONFIG"
    return None


_TOOL_SUBGROUP_ORDER = ("FIRMWARE", "LOGS", "CONFIG", "SESSION")

def _header_bar_pushbutton_qss() -> str:
    a = ARLO_ACCENT_COLOR
    return f"""
    QPushButton {{
        background: transparent;
        border: 1px solid #2a2a2a;
        border-radius: 4px;
        color: #cccccc;
        padding: 4px 14px;
    }}
    QPushButton:hover {{
        background: #1e1e1e;
        border-color: {a};
        color: #ffffff;
    }}
    QPushButton:pressed {{
        background: {a};
        color: #ffffff;
    }}
    QPushButton:disabled {{
        color: #444444;
        border-color: #1a1a1a;
    }}
    """


class _CollapsibleCategoryBlock(QWidget):
    """Category header toggles visibility of body; header is not a command."""

    def __init__(self, title: str, *, expanded_default: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._expanded = expanded_default
        self._title = title.upper()
        self._header = QPushButton(self)
        self._header.setFlat(True)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setStyleSheet(
            """
            QPushButton {
                text-align: left;
                border: none;
                padding: 10px 4px 4px 8px;
                color: #7a8494;
                font-size: 10px;
                letter-spacing: 0.14em;
                font-weight: 600;
            }
            QPushButton:hover { color: #9aa3b2; }
            """
        )
        self._header.clicked.connect(self._toggle)
        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._header)
        outer.addWidget(self._body)
        self._sync_header()
        self._body.setVisible(self._expanded)
        self._row_widgets: list[QWidget] = []

    def register_row(self, w: QWidget) -> None:
        self._row_widgets.append(w)

    def _chevron(self) -> str:
        return "▼" if self._expanded else "▸"

    def _sync_header(self) -> None:
        self._header.setText(f"{self._chevron()}  {self._title}")

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._sync_header()

    def set_expanded(self, on: bool) -> None:
        if self._expanded == on:
            return
        self._expanded = on
        self._body.setVisible(on)
        self._sync_header()

    def body_layout(self) -> QVBoxLayout:
        return self._body_layout


class _AdvancedTierBlock(QWidget):
    """Tier 3 shell: collapsed by default; header toggles body."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._expanded = False
        self._header = QPushButton(self)
        self._header.setFlat(True)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setStyleSheet(
            """
            QPushButton {
                text-align: left;
                border: none;
                padding: 12px 4px 6px 8px;
                color: #6d7685;
                font-size: 10px;
                letter-spacing: 0.14em;
                font-weight: 600;
            }
            QPushButton:hover { color: #8b95a5; }
            """
        )
        self._header.clicked.connect(self._toggle)
        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._header)
        outer.addWidget(self._body)
        self._sync_header()
        self._body.setVisible(False)
        self._row_widgets: list[QWidget] = []

    def register_row(self, w: QWidget) -> None:
        self._row_widgets.append(w)

    def _sync_header(self) -> None:
        ch = "▾" if self._expanded else "▸"
        self._header.setText(f"{ch}  ADVANCED")

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._sync_header()

    def set_expanded(self, on: bool) -> None:
        if self._expanded == on:
            return
        self._expanded = on
        self._body.setVisible(on)
        self._sync_header()

    def body_layout(self) -> QVBoxLayout:
        return self._body_layout


class _CommandRowFrame(QFrame):
    """Single click runs command after a short delay; double-click cancels run and prefills input."""

    def __init__(
        self,
        *,
        cmd_key: str,
        display_line: str,
        args_hint: str,
        tooltip: str,
        tier: int,
        on_run: Any,
        on_prefill: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("cmdRow")
        self._cmd_key = cmd_key
        self._on_run = on_run
        self._on_prefill = on_prefill
        self._pending_timer: QTimer | None = None
        self._suppress_next_release = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(_CMD_ROW_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(tooltip)
        self.setToolTipDuration(60000)

        if tier == 1:
            fg, fg_dim = "#e8eef4", "#6d7a8c"
            self._hover_bg = "#3a4352"
            self._tier = 1
        elif tier == 2:
            fg, fg_dim = "#c5ced9", "#5c677a"
            self._hover_bg = "#353d4a"
            self._tier = 2
        else:
            fg, fg_dim = "#8a939e", "#6a737e"
            self._hover_bg = "#333a45"
            self._tier = 3
            f = self.font()
            f.setFamily("Consolas, 'Cascadia Mono', monospace")
            self.setFont(f)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 6, 0)
        row.setSpacing(4)
        self._name_lbl = QLabel(display_line)
        self._name_lbl.setStyleSheet(f"color: {fg}; border: none; padding: 0; background: transparent;")
        row.addWidget(self._name_lbl, stretch=1)
        if args_hint:
            h = QLabel(args_hint)
            h.setStyleSheet(f"color: {fg_dim}; border: none; padding: 0; background: transparent;")
            if tier == 3:
                hf = h.font()
                hf.setFamily("Consolas, 'Cascadia Mono', monospace")
                h.setFont(hf)
            row.addWidget(h, stretch=0)
        self._apply_idle_style()

    def _apply_idle_style(self) -> None:
        if self._tier == 2:
            self.setStyleSheet(
                "QFrame#cmdRow { border-left: 3px solid #4a6fa5; background: transparent; border-radius: 4px; }"
            )
        else:
            self.setStyleSheet("QFrame#cmdRow { background: transparent; border-radius: 4px; }")

    def _apply_hover_style(self) -> None:
        if self._tier == 2:
            self.setStyleSheet(
                "QFrame#cmdRow { border-left: 3px solid #4a6fa5; background-color: "
                f"{self._hover_bg}; border-radius: 4px; }}"
            )
        else:
            self.setStyleSheet(
                f"QFrame#cmdRow {{ background-color: {self._hover_bg}; border-radius: 4px; }}"
            )

    def enterEvent(self, event: Any) -> None:
        self._apply_hover_style()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self._apply_idle_style()
        super().leaveEvent(event)

    def _cancel_pending_run(self) -> None:
        if self._pending_timer is not None:
            self._pending_timer.stop()
            self._pending_timer.deleteLater()
            self._pending_timer = None

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._cmd_key:
            if self._suppress_next_release:
                self._suppress_next_release = False
                super().mouseReleaseEvent(event)
                return
            self._cancel_pending_run()
            ms = QApplication.styleHints().mouseDoubleClickInterval() + 40
            t = QTimer(self)
            t.setSingleShot(True)

            def _go() -> None:
                self._pending_timer = None
                self._on_run(self._cmd_key)

            t.timeout.connect(_go)
            self._pending_timer = t
            t.start(ms)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._cmd_key:
            self._cancel_pending_run()
            self._suppress_next_release = True
            self._on_prefill(self._cmd_key)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def _make_config(conn_type: str, settings: dict, device_id: str) -> ConnectionConfig:
    return ConnectionConfig(
        type=conn_type,
        settings=settings,
        status="connected",
        connected_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        device_identifier=device_id,
    )


def install_gui_console_and_menus(bridge: GuiBridge) -> None:
    """Redirect Rich console and menu helpers to the GUI log."""
    import interface.menus as menus
    import interface.prompts as prompts

    class _LogStream:
        def __init__(self, b: GuiBridge) -> None:
            self._b = b

        def write(self, s: str) -> None:
            if s:
                self._b.append_log.emit(s)

        def flush(self) -> None:
            pass

    menus.console = Console(
        file=_LogStream(bridge),
        width=120,
        no_color=True,
        force_terminal=False,
        soft_wrap=True,
    )
    menus.set_gui_menu_bridge(bridge)
    prompts.set_gui_prompt_bridge(bridge)


class SessionWorker(QObject):
    """Owns connection handle; all I/O runs on this object's thread."""

    append_log = Signal(str)
    state_changed = Signal(dict)
    commands_updated = Signal(list)
    command_finished = Signal(str, object)
    connect_failed = Signal(str)

    connect_uart = Signal(str, int)
    connect_adb = Signal(str)
    connect_ssh = Signal(str, int, str, str)
    submit_command = Signal(str)
    disconnect_session = Signal()
    fw_shell_request = Signal(str, object)
    fw_shell_response = Signal(bool, str)

    def __init__(self, bridge: GuiBridge) -> None:
        super().__init__()
        self._bridge = bridge
        self._cfg: ConnectionConfig | None = None
        self._handle: Any = None
        self._device_commands: list[dict] = []
        self._detected: dict[str, Any] = {}
        self._command_profile: str = "none"

        self.connect_uart.connect(self._on_connect_uart)
        self.connect_adb.connect(self._on_connect_adb)
        self.connect_ssh.connect(self._on_connect_ssh)
        self.submit_command.connect(self._on_command)
        self.disconnect_session.connect(self._on_disconnect)
        self.fw_shell_request.connect(self._on_fw_shell_request)

    def _emit_state(self) -> None:
        if self._cfg and self._handle:
            name = (self._detected.get("model") or "Device").strip() or "Device"
            self.state_changed.emit(
                {
                    "connected": True,
                    "model": name,
                    "fw": self._detected.get("fw_version") or "—",
                    "env": (self._detected.get("env") or "—"),
                    "conn_type": self._cfg.type,
                    "device_id": self._cfg.device_identifier or "",
                    "commands_count": len(self._device_commands),
                    "command_profile": self._command_profile,
                    "is_onboarded": self._detected.get("is_onboarded"),
                    "raw_build_info": self._detected.get("raw_build_info") or "",
                }
            )
        else:
            self.state_changed.emit({"connected": False})

    @Slot(str, int)
    def _on_connect_uart(self, port: str, baud: int) -> None:
        self.append_log.emit("Connecting via UART...\n")
        handler = UARTHandler()
        ok, msg, settings = handler.connect(port=port, baud_rate=baud)
        if ok and settings:
            cfg = _make_config(
                "UART",
                settings,
                handler.device_identifier() or f"{port}@{baud}",
            )
            self._cfg = cfg
            self._handle = handler
            self.append_log.emit(f"Connected via UART ({cfg.device_identifier})\n")
            self._run_detect_and_load()
            return
        self._cfg = None
        self._handle = None
        err = (msg or "UART connection failed.").strip()
        self.append_log.emit(f"\nConnection failed: {err}\n\n")
        self.connect_failed.emit(err)

    @Slot(str)
    def _on_connect_adb(self, password: str) -> None:
        self.append_log.emit("Connecting via ADB...\n")
        handler = ADBHandler()
        ok, msg, settings = handler.connect(password=password)
        if ok and settings:
            device_id = settings.get("device_serial") or "USB"
            cfg = _make_config("ADB", settings, device_id)
            self._cfg = cfg
            self._handle = handler
            self.append_log.emit(f"Connected via USB ({device_id})\n")
            self._run_detect_and_load()
            return
        self._cfg = None
        self._handle = None
        err = (msg or "ADB connection failed.").strip()
        self.append_log.emit(f"\nConnection failed: {err}\n\n")
        self.connect_failed.emit(err)

    @Slot(str, int, str, str)
    def _on_connect_ssh(self, ip: str, port: int, username: str, password: str) -> None:
        self.append_log.emit("Connecting via SSH...\n")
        handler = SSHHandler()
        ok, msg, settings = handler.connect(
            ip_address=ip,
            port=port,
            username=username,
            password=password,
        )
        if ok and settings:
            device_id = f"{settings['ip_address']}:{settings['port']}"
            cfg = _make_config("SSH", settings, device_id)
            self._cfg = cfg
            self._handle = handler
            self.append_log.emit(f"Connected at {device_id}\n")
            self._run_detect_and_load()
            return
        self._cfg = None
        self._handle = None
        err = (msg or "SSH connection failed.").strip()
        self.append_log.emit(f"\nConnection failed: {err}\n\n")
        self.connect_failed.emit(err)

    def _run_detect_and_load(self) -> None:
        if not self._handle:
            return
        self.append_log.emit("Detecting device (build_info, kvcmd, device_info)...\n")
        self._detected = detect_device(self._handle.execute)
        model_for_commands = self._detected.get("model") or "Device"
        self._command_profile = get_command_profile_for_model_name(self._detected.get("model"))
        self._device_commands = load_device_commands(model_for_commands)
        self._emit_state()
        self.commands_updated.emit(list(self._device_commands))
        ob = self._detected.get("is_onboarded")
        ob_note = ""
        if ob is True:
            ob_note = " | Onboarded (Arlo account)"
        elif ob is False:
            ob_note = " | Not onboarded"
        self.append_log.emit(
            f"Model: {self._detected.get('model') or '—'} | "
            f"FW: {self._detected.get('fw_version') or '—'} | "
            f"Env: {self._detected.get('env') or '—'}{ob_note}\n"
        )
        self.append_log.emit(
            "Type a command below, or click a command in the list to run it "
            "(double-click to put the name in the input for editing).\n\n"
        )

    @Slot()
    def _on_disconnect(self) -> None:
        if self._handle:
            try:
                self._handle.disconnect()
            except Exception:
                pass
        self._handle = None
        self._cfg = None
        self._detected = {}
        self._device_commands = []
        self._command_profile = "none"
        # Log before state_changed so the UI still routes this line to the session tab.
        self.append_log.emit("Disconnected.\n\n")
        self._emit_state()

    @Slot(str)
    def _on_command(self, line: str) -> None:
        line = (line or "").strip()
        if not line:
            return
        if not self._cfg or not self._handle:
            self.append_log.emit("Not connected.\n")
            self.command_finished.emit("continue", None)
            return

        prompt_name = (self._detected.get("model") or "Device").strip() or "Device"
        m_info = get_model_by_name(prompt_name)
        if m_info:
            fw_search = list(m_info.get("fw_search_models") or [m_info["name"]])
        else:
            fw_search = [prompt_name] if self._detected.get("model") else []
        current_model_dict = {
            "name": prompt_name,
            "fw_search_models": fw_search,
            "command_profile": self._command_profile,
            "is_onboarded": self._detected.get("is_onboarded"),
        }
        action, message = parse_and_execute(
            line,
            current_model_dict,
            self._cfg.type,
            self._cfg.device_identifier or "",
            self._cfg.connected_at,
            self._device_commands,
            self._handle.execute,
            connection_pull_file=getattr(self._handle, "pull_file", None),
            pull_logs_local_dir=os.getcwd(),
            connection_get_tail_logs_command=getattr(self._handle, "get_tail_logs_command", None),
            connection_handle=self._handle,
            command_profile=self._command_profile,
        )
        if message:
            self.append_log.emit(_strip_rich_markup(message) + "\n")
        if action == "exit":
            self._on_disconnect()
        elif action == "disconnected":
            self._on_disconnect()
        self.command_finished.emit(action, message)

    @Slot(str, object)
    def _on_fw_shell_request(self, command: str, args_obj: object) -> None:
        args = list(args_obj) if isinstance(args_obj, (list, tuple)) else []
        if not self._handle:
            self.fw_shell_response.emit(False, "Not connected.")
            return
        ok, text = self._handle.execute(command, args)
        self.fw_shell_response.emit(ok, text or "")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"ArloShell  v{ARLO_SHELL_VERSION}")
        _icon_path = _main_window_icon_path()
        if _icon_path:
            self.setWindowIcon(_load_icon(_icon_path))
        self.resize(1100, 720)
        self._device_connected = False
        self._status_phase: str = "disconnected"
        self._status_detail_model: str = "—"
        self._status_detail_fw: str = "—"
        self._status_detail_transport: str = "—"
        self._status_detail_env: str = "—"
        self._device_is_onboarded: bool | None = None

        self._bridge = GuiBridge()
        self._bridge.set_main_window(self)
        install_gui_console_and_menus(self._bridge)

        self._thread = QThread()
        self._worker = SessionWorker(self._bridge)
        self._worker.moveToThread(self._thread)
        self._thread.start()

        self._bridge.append_log.connect(self._on_append_log)
        self._worker.append_log.connect(self._on_append_log)
        self._worker.state_changed.connect(self._on_state_changed)
        self._worker.commands_updated.connect(self._merge_command_list)
        self._worker.command_finished.connect(self._on_command_finished)
        self._worker.connect_failed.connect(self._on_connect_failed)
        self._worker.fw_shell_response.connect(self._on_fw_shell_response)

        self._fw_shell_pending: Callable[[bool, str], None] | None = None
        self._fw_wizard = None

        self._command_profile: str = "none"
        self._conn_type: str = ""
        self._cmd_filter_rows: list[tuple[QWidget, str]] = []
        self._cmd_advanced_rows: list[QWidget] = []
        self._cmd_panel_groups: list[_CollapsibleCategoryBlock] = []
        self._cmd_tier1_outer: QWidget | None = None
        self._cmd_tools_outer: QWidget | None = None
        self._cmd_adv_block_ref: _AdvancedTierBlock | None = None
        self._cmd_sep_t1_tools: QFrame | None = None
        self._cmd_sep_tools_adv: QFrame | None = None
        self._live_tail_sessions: dict[str, dict[str, Any]] = {}
        self._bridge.tail_live_start.connect(self._on_tail_live_start)
        self._bridge.tail_live_stop.connect(self._on_tail_live_stop)
        set_tail_live_view_handlers(
            lambda p, t: self._bridge.tail_live_start.emit(p, t),
            lambda p: self._bridge.tail_live_stop.emit(p),
        )

        title = QLabel("ArloShell")
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {ARLO_ACCENT_COLOR};")

        intro = QLabel("Camera developer tool  ·  ADB  ·  SSH  ·  UART")
        intro.setWordWrap(True)

        self._status_strip = QWidget()
        status_lay = QHBoxLayout(self._status_strip)
        status_lay.setContentsMargins(0, 0, 0, 0)
        status_lay.setSpacing(6)

        self._status_text = QLabel("Not connected")
        self._status_text.setWordWrap(True)
        self._status_text.setMinimumHeight(22)
        self._status_text.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        )

        dot_container = QWidget(self._status_strip)
        dot_container.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        dot_lay = QVBoxLayout(dot_container)
        dot_lay.setContentsMargins(0, 0, 0, 0)
        dot_lay.setSpacing(0)

        self._status_dot = QWidget(dot_container)
        self._status_dot.setObjectName("statusDot")
        self._status_dot.setFixedSize(10, 10)
        self._status_dot.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._status_dot.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._set_status_dot_color(_STATUS_DOT_DISCONNECTED)
        dot_lay.addStretch(1)
        dot_lay.addWidget(self._status_dot, 0, Qt.AlignmentFlag.AlignHCenter)
        dot_lay.addStretch(1)

        _text_h = max(22, self._status_text.sizeHint().height())
        dot_container.setFixedHeight(_text_h)

        self._onboarded_badge = QLabel("Onboarded")
        self._onboarded_badge.setVisible(False)
        self._onboarded_badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._onboarded_badge.setStyleSheet(
            "QLabel { background-color: #3949ab; color: #e8eaf6; border-radius: 10px; "
            "padding: 3px 10px; font-size: 11px; font-weight: 600; }"
        )

        status_lay.addWidget(dot_container, 0, Qt.AlignmentFlag.AlignVCenter)
        status_lay.addWidget(self._onboarded_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        status_lay.addWidget(self._status_text, 1, Qt.AlignmentFlag.AlignVCenter)
        self._sync_status_strip()

        btn_row = QHBoxLayout()
        self._btn_connect = QPushButton("Connect…")
        self._btn_connect.setStyleSheet(_header_bar_pushbutton_qss())
        self._btn_connect.clicked.connect(self._open_connect_dialog)
        self._btn_disconnect = QPushButton("Disconnect")
        self._btn_disconnect.setStyleSheet(_header_bar_pushbutton_qss())
        self._btn_disconnect.clicked.connect(self._disconnect)
        self._btn_disconnect.setEnabled(False)
        self._btn_help = QPushButton("Help")
        self._btn_help.setStyleSheet(_header_bar_pushbutton_qss())
        self._btn_help.clicked.connect(self._run_help)
        self._btn_clear = QPushButton("Clear log")
        self._btn_clear.setStyleSheet(_header_bar_pushbutton_qss())
        self._btn_clear.clicked.connect(self._clear_log)
        btn_row.addWidget(self._btn_connect)
        btn_row.addWidget(self._btn_disconnect)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_help)
        btn_row.addWidget(self._btn_clear)

        self._cmd_sidebar = QWidget()
        self._cmd_sidebar.setObjectName("cmdSidebar")
        self._cmd_sidebar.setStyleSheet(
            "#cmdSidebar { border-right: 1px solid #1e1e1e; background-color: #0d0d0d; }"
        )
        self._cmd_sidebar.setMinimumWidth(180)
        self._cmd_sidebar.setMaximumWidth(280)
        side_outer = QVBoxLayout(self._cmd_sidebar)
        side_outer.setContentsMargins(2, 0, 4, 0)
        side_outer.setSpacing(6)

        self._cmd_filter_row = QWidget()
        filter_lay = QHBoxLayout(self._cmd_filter_row)
        filter_lay.setContentsMargins(0, 0, 0, 0)
        filter_lay.setSpacing(0)
        self._cmd_filter_edit = QLineEdit()
        self._cmd_filter_edit.setPlaceholderText("Filter commands...")
        self._cmd_filter_edit.setClearButtonEnabled(True)
        self._cmd_filter_edit.setStyleSheet(
            """
            QLineEdit {
                background-color: #2a313a;
                border: 1px solid #3d4654;
                border-radius: 4px;
                padding: 5px 8px;
                color: #c5ced9;
                font-size: 12px;
            }
            QLineEdit:focus {
                border: 1px solid #4a6fa5;
            }
            """
        )
        self._cmd_filter_edit.textChanged.connect(self._on_cmd_filter_text_changed)
        filter_lay.addWidget(self._cmd_filter_edit, stretch=1)

        self._cmd_panel_title = QLabel("Commands")
        title_cmd_font = QFont()
        title_cmd_font.setBold(True)
        title_cmd_font.setPointSize(11)
        self._cmd_panel_title.setFont(title_cmd_font)
        self._cmd_panel_title.setStyleSheet(
            f"color: {ARLO_ACCENT_COLOR}; border-left: 3px solid {ARLO_ACCENT_COLOR}; "
            "padding: 4px 4px 0 8px; background: transparent;"
        )

        self._cmd_scroll = QScrollArea()
        self._cmd_scroll.setWidgetResizable(True)
        self._cmd_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._cmd_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._cmd_scroll_content = QWidget()
        self._cmd_body_layout = QVBoxLayout(self._cmd_scroll_content)
        self._cmd_body_layout.setContentsMargins(0, 0, 0, 0)
        self._cmd_body_layout.setSpacing(0)
        self._cmd_scroll.setWidget(self._cmd_scroll_content)

        side_outer.addWidget(self._cmd_filter_row)
        side_outer.addWidget(self._cmd_panel_title)
        side_outer.addWidget(self._cmd_scroll, stretch=1)

        self._tab_logs = QTabWidget()
        self._tab_logs.setObjectName("contentTabs")
        self._tab_logs.setDocumentMode(True)
        self._tab_logs.setMovable(True)
        self._tab_logs.setTabsClosable(True)
        self._tab_logs.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tab_logs.tabBar().tabMoved.connect(self._update_tab_close_buttons)
        self._tab_logs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._tab_logs.setStyleSheet(
            f"""
            QTabWidget#contentTabs::pane {{
                border: 1px solid #3d4654;
                top: -1px;
                background-color: #111111;
            }}
            QTabBar {{
                border-bottom: 1px solid #1e1e1e;
            }}
            QTabBar::tab {{
                color: #8b95a5;
                background: transparent;
                padding: 8px 14px;
                margin-right: 2px;
                border: none;
                border-bottom: 2px solid transparent;
            }}
            QTabBar::tab:selected {{
                color: #ffffff;
                background: transparent;
                border-bottom: 2px solid {ARLO_ACCENT_COLOR};
            }}
            QTabBar::tab:hover:!selected {{
                color: #aeb8c4;
            }}
            QTabBar::close-button {{
                width: 14px;
                height: 14px;
                margin: 2px 4px;
                padding: 0px;
                subcontrol-position: right;
            }}
            QTabBar::close-button:hover {{
                background-color: #5a6270;
                border-radius: 7px;
            }}
            """
        )
        self._welcome_tab_root, self._welcome_log = self._build_welcome_tab_widget()
        self._tab_logs.addTab(self._welcome_tab_root, "Welcome")
        self._e3_reference_widget: QWidget | None = None
        self._active_session_log: QTextEdit | None = None
        self._update_tab_close_buttons()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._cmd_sidebar)
        splitter.addWidget(self._tab_logs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self._cmd_input = QLineEdit()
        self._cmd_input.setEnabled(False)
        self._cmd_input.setPlaceholderText("Enter command (e.g. help, status, reboot)…")
        self._cmd_input.setStyleSheet(
            f"""
            QLineEdit {{
                background-color: #2a313a;
                border: 1px solid #3d4654;
                border-bottom: 2px solid {ARLO_ACCENT_COLOR};
                border-radius: 4px;
                padding: 5px 8px;
                color: #c5ced9;
                font-size: 12px;
            }}
            QLineEdit:focus {{
                border: 1px solid #4a6fa5;
                border-bottom: 2px solid {ARLO_ACCENT_COLOR};
            }}
            QLineEdit:disabled {{
                border: 1px solid #3d4654;
                border-bottom: 2px solid #4a5563;
                color: #7a8494;
            }}
            """
        )
        self._cmd_input.returnPressed.connect(self._send_command)
        send_btn = QPushButton("Send")
        send_btn.clicked.connect(self._send_command)
        cmd_input_area = QWidget()
        cmd_input_area.setStyleSheet("QWidget { border-top: 1px solid #1e1e1e; }")
        cmd_row = QHBoxLayout(cmd_input_area)
        cmd_row.addWidget(self._cmd_input)
        cmd_row.addWidget(send_btn)

        header_bar = QWidget()
        header_bar.setStyleSheet("QWidget { border-bottom: 1px solid #1e1e1e; }")
        header_layout = QVBoxLayout(header_bar)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.addWidget(title)
        header_layout.addWidget(intro)
        header_layout.addWidget(self._status_strip)
        self._device_info_panel = self._build_device_info_panel()
        header_layout.addWidget(self._device_info_panel)
        header_layout.addLayout(btn_row)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(header_bar)
        layout.addWidget(splitter, stretch=1)
        layout.addWidget(cmd_input_area)
        self.setCentralWidget(central)

        self._setup_menu_bar()

        self._set_command_list_disconnected()
        self._prompt_model_name = "Device"

    def _setup_menu_bar(self) -> None:
        menubar = self.menuBar()
        menu_view = menubar.addMenu("&View")
        act_session_log = QAction("&Session log", self)
        act_session_log.setShortcut(QKeySequence("Ctrl+Shift+L"))
        act_session_log.setStatusTip("Show the live connection and command output tab")
        act_session_log.triggered.connect(self._focus_session_log_tab)
        menu_view.addAction(act_session_log)

        menu_tools = menubar.addMenu("&Tools")
        self._action_fw_setup = QAction("FW &Setup…", self)
        self._action_fw_setup.setStatusTip(
            "Open the firmware setup wizard (Artifactory, local server, update URL). "
            "The fw_setup command runs the same steps as text prompts in the session log."
        )
        self._action_fw_setup.triggered.connect(self._menu_fw_setup)
        self._action_fw_setup.setEnabled(False)
        menu_tools.addAction(self._action_fw_setup)

        menu_help = menubar.addMenu("&Help")
        act_ref = QAction("Command &reference", self)
        act_ref.setShortcut(QKeySequence.StandardKey.HelpContents)
        act_ref.setStatusTip("Show all commands (same as typing help)")
        act_ref.triggered.connect(self._run_help)
        menu_help.addAction(act_ref)
        act_e3 = QAction("E3 Wired &CLI reference", self)
        act_e3.setStatusTip("Open the E3 Wired full CLI reference tab (Confluence-sourced)")
        act_e3.triggered.connect(self._focus_e3_reference_tab)
        menu_help.addAction(act_e3)
        menu_help.addSeparator()
        act_about = QAction("&About", self)
        act_about.triggered.connect(self._menu_about)
        menu_help.addAction(act_about)

    def _menu_fw_setup(self) -> None:
        if not self._device_connected:
            QMessageBox.information(
                self,
                "FW Setup",
                "Connect to a camera first (use Connect… on the toolbar), then choose Tools → FW Setup.",
            )
            return
        QMessageBox.information(
            self,
            "FW Setup — company VPN",
            "Firmware is downloaded from the company Artifactory server.\n\n"
            "Before continuing, make sure you are connected to the company VPN "
            "(GlobalProtect).\n\n"
            "Click OK to open the FW Setup wizard.",
        )
        prompt_name = (getattr(self, "_prompt_model_name", None) or "Device").strip() or "Device"
        m_info = get_model_by_name(prompt_name)
        if m_info:
            fw_search = list(m_info.get("fw_search_models") or [m_info["name"]])
            primary = (m_info.get("name") or prompt_name).strip() or prompt_name
        else:
            fw_search = [prompt_name] if prompt_name and prompt_name != "Device" else ["Camera"]
            primary = prompt_name
        model_dict = {
            "name": primary,
            "fw_search_models": fw_search,
            "command_profile": self._command_profile,
            "is_onboarded": self._device_is_onboarded,
        }
        from interface.fw_setup_wizard import FwSetupWizard

        wiz = FwSetupWizard(self, model_dict, self._fw_shell_async)
        wiz.server_started.connect(self._on_fw_wizard_server_started)
        wiz.update_sent.connect(self._on_fw_wizard_update_sent)
        wiz.wizard_closed.connect(self._on_fw_wizard_closed)
        self._fw_wizard = wiz
        wiz.show()

    def _fw_shell_async(
        self, cmd: str, args: list[str], on_done: Callable[[bool, str], None]
    ) -> None:
        self._fw_shell_pending = on_done
        self._worker.fw_shell_request.emit(cmd, args)

    @Slot(bool, str)
    def _on_fw_shell_response(self, ok: bool, text: str) -> None:
        cb = self._fw_shell_pending
        self._fw_shell_pending = None
        if cb:
            cb(ok, text or "")

    def _on_fw_wizard_server_started(self, url: str) -> None:
        self._on_append_log(f"FW Setup: camera update URL (local server): {url}\n")

    def _on_fw_wizard_update_sent(self, ok: bool) -> None:
        if ok:
            self._on_append_log("FW Setup: update_url command succeeded.\n")
        else:
            self._on_append_log("FW Setup: update_url command failed (see wizard).\n")

    def _on_fw_wizard_closed(self) -> None:
        self._fw_wizard = None

    def _menu_about(self) -> None:
        QMessageBox.about(
            self,
            "About ArloShell",
            "<h3>ArloShell</h3>"
            "<p>Connect to cameras over UART, ADB (USB), or SSH. "
            "Commands are loaded after the device is detected.</p>"
            "<p><b>Tools → FW Setup</b> opens the firmware wizard "
            "(Artifactory, local server, camera <code>update_url</code>). "
            "Typing <code>fw_setup</code> in the command line runs the same flow with text prompts.</p>",
        )

    def _build_e3_reference_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        path = _e3_cli_reference_path()
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            body = (
                "# E3 reference file not found\n\n"
                f"Expected:\n`{path}`\n\n"
                "From source, it lives at `docs/e3_wired_cli_reference.md`. "
                "Rebuild the frozen app with the current PyInstaller spec to bundle it."
            )
        browser.setMarkdown(body)
        bf = QFont("Menlo", 10) if sys.platform == "darwin" else QFont("Consolas", 10)
        browser.setFont(bf)
        layout.addWidget(browser)
        return panel

    def _ensure_e3_reference_tab(self) -> QWidget:
        if self._e3_reference_widget is None:
            self._e3_reference_widget = self._build_e3_reference_panel()
            self._tab_logs.addTab(self._e3_reference_widget, "E3 reference")
            self._update_tab_close_buttons()
        return self._e3_reference_widget

    def _focus_e3_reference_tab(self) -> None:
        w = self._ensure_e3_reference_tab()
        i = self._tab_logs.indexOf(w)
        if i >= 0:
            self._tab_logs.setCurrentIndex(i)

    def _new_log_editor(self) -> QTextEdit:
        log = QTextEdit()
        log.setReadOnly(True)
        f = QFont("Menlo", 11) if sys.platform == "darwin" else QFont("Consolas", 10)
        log.setFont(f)
        return log

    def _build_welcome_tab_widget(self) -> tuple[QWidget, QTextEdit]:
        """Centered branding above a log QTextEdit (log target when no session tab)."""
        root = QWidget()
        root.setStyleSheet("QWidget#welcomeTabRoot { background-color: #111111; }")
        root.setObjectName("welcomeTabRoot")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addStretch(2)

        centered_block = QWidget()
        block_lay = QVBoxLayout(centered_block)
        block_lay.setContentsMargins(0, 0, 0, 0)
        block_lay.setSpacing(8)
        block_lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        title_w = QLabel("ArloShell")
        title_w.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tf = QFont()
        tf.setPointSize(22)
        tf.setBold(True)
        title_w.setFont(tf)
        title_w.setStyleSheet(f"color: {ARLO_ACCENT_COLOR}; border: none; background: transparent;")
        block_lay.addWidget(title_w, 0, Qt.AlignmentFlag.AlignHCenter)

        adb_l = QLabel("ADB · SSH · UART")
        adb_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
        adbf = QFont()
        adbf.setPointSize(9)
        adb_l.setFont(adbf)
        adb_l.setStyleSheet("color: #666666; border: none; background: transparent;")
        block_lay.addWidget(adb_l, 0, Qt.AlignmentFlag.AlignHCenter)

        sep_row = QHBoxLayout()
        sep_row.setContentsMargins(0, 0, 0, 0)
        sep_row.setSpacing(0)
        sep_row.addStretch(1)
        sep_fr = QFrame()
        sep_fr.setFrameShape(QFrame.Shape.HLine)
        sep_fr.setMaximumWidth(300)
        sep_fr.setFixedHeight(1)
        sep_fr.setStyleSheet("background-color: #3d4654; border: none; max-height: 1px;")
        sep_row.addWidget(sep_fr, 0, Qt.AlignmentFlag.AlignCenter)
        sep_row.addStretch(1)
        block_lay.addLayout(sep_row)

        ver_l = QLabel(f"Version {ARLO_SHELL_VERSION}")
        ver_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vf = QFont()
        vf.setPointSize(9)
        ver_l.setFont(vf)
        ver_l.setStyleSheet("color: #aeb8c4; border: none; background: transparent;")
        block_lay.addWidget(ver_l, 0, Qt.AlignmentFlag.AlignHCenter)

        connect_l = QLabel("Click Connect to get started")
        connect_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cf = QFont()
        cf.setPointSize(10)
        connect_l.setFont(cf)
        connect_l.setStyleSheet("color: #aeb8c4; border: none; background: transparent;")
        block_lay.addWidget(connect_l, 0, Qt.AlignmentFlag.AlignHCenter)

        outer.addWidget(centered_block, 0, Qt.AlignmentFlag.AlignHCenter)

        outer.addStretch(3)

        welcome_log = self._new_log_editor()
        welcome_log.setMinimumHeight(0)
        welcome_log.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        welcome_log.hide()
        outer.addWidget(welcome_log, stretch=0)

        return root, welcome_log

    def _set_status_dot_color(self, color: str) -> None:
        # QWidget + WA_StyledBackground: QFrame default frame style breaks stylesheet parse/fill.
        self._status_dot.setStyleSheet(
            f"#statusDot {{ background-color: {color}; border-radius: 5px; border: none; }}"
        )

    def _sync_status_strip(self) -> None:
        pal = self.palette()
        dark_ui = pal.color(QPalette.ColorRole.Window).lightness() < 128
        muted = "#c5ced9" if dark_ui else "#3d4f5f"
        bright = "#e8eef4" if dark_ui else "#0f1419"

        if self._status_phase == "connecting":
            self._set_status_dot_color(_STATUS_DOT_CONNECTING)
            self._onboarded_badge.setVisible(False)
            self._status_text.setText("Connecting...")
            self._status_text.setStyleSheet(
                f"color: {muted}; font-size: 12px; padding: 2px 0; border: none; background: transparent;"
            )
        elif self._device_connected:
            self._set_status_dot_color(_STATUS_DOT_CONNECTED)
            m = self._status_detail_model or "—"
            fw = self._status_detail_fw or "—"
            t = self._status_detail_transport or "—"
            e = self._status_detail_env or "—"
            self._status_text.setText(f"Connected · {m} · FW: {fw} · {t} · {e}")
            self._status_text.setStyleSheet(
                f"color: {bright}; font-size: 13px; font-weight: 500; padding: 4px 0; "
                "border: none; background: transparent;"
            )
            if getattr(self, "_device_is_onboarded", None) is True:
                self._onboarded_badge.setVisible(True)
            else:
                self._onboarded_badge.setVisible(False)
        else:
            self._set_status_dot_color(_STATUS_DOT_DISCONNECTED)
            self._onboarded_badge.setVisible(False)
            self._status_text.setText("Not connected")
            self._status_text.setStyleSheet(
                f"color: {muted}; font-size: 12px; padding: 2px 0; border: none; background: transparent;"
            )

    def _begin_connection_log_tab(self) -> None:
        """Open a new tab for this connection attempt; worker log lines go here until disconnect or failure."""
        log = self._new_log_editor()
        idx = self._tab_logs.addTab(log, "Connecting…")
        self._tab_logs.setCurrentIndex(idx)
        self._active_session_log = log
        self._status_phase = "connecting"
        self._sync_status_strip()
        self._update_tab_close_buttons()

    def _set_active_session_tab_title(self, title: str) -> None:
        if self._active_session_log is None:
            return
        i = self._tab_logs.indexOf(self._active_session_log)
        if i < 0:
            return
        safe = (title or "Device").strip() or "Device"
        if len(safe) > 36:
            safe = safe[:33] + "…"
        self._tab_logs.setTabText(i, safe.replace("&", "&&"))

    def _finalize_failed_session_tab(self) -> None:
        if self._active_session_log is None:
            return
        i = self._tab_logs.indexOf(self._active_session_log)
        self._active_session_log = None
        # Set tab title after refresh: toggling tabsClosable can reset labels if done earlier.
        self._update_tab_close_buttons()
        if i >= 0:
            self._tab_logs.setTabText(i, "Connection failed")

    def _log_target(self) -> QTextEdit:
        """Live output: session tab while connecting/connected, otherwise Welcome."""
        if self._active_session_log is not None:
            return self._active_session_log
        return self._welcome_log

    @staticmethod
    def _tail_path_key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def _tail_read_chunk(self, state: dict[str, Any], *, final: bool) -> None:
        edit: QTextEdit = state["edit"]
        decoder: Any = state["decoder"]
        path: str = state["path"]
        pos: int = state["pos"]
        raw = b""
        try:
            with open(path, "rb") as f:
                f.seek(pos)
                raw = f.read()
                state["pos"] = f.tell()
        except OSError:
            pass
        if not raw and not final:
            return
        text = decoder.decode(raw, final=final)
        if text:
            edit.moveCursor(QTextCursor.MoveOperation.End)
            edit.insertPlainText(text)
            edit.moveCursor(QTextCursor.MoveOperation.End)

    def _finalize_live_tail_state(self, state: dict[str, Any]) -> None:
        timer = state.get("timer")
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._tail_read_chunk(state, final=True)
        edit: QTextEdit = state["edit"]
        idx = self._tab_logs.indexOf(edit)
        if idx >= 0:
            cur = self._tab_logs.tabText(idx)
            if " (stopped)" not in cur:
                self._tab_logs.setTabText(idx, (cur + " (stopped)")[:44])

    @Slot(str, str)
    def _on_tail_live_start(self, path: str, title: str) -> None:
        key = self._tail_path_key(path)
        if key in self._live_tail_sessions:
            return
        edit = self._new_log_editor()
        tab_title = title if len(title) <= 26 else title[:23] + "…"
        idx = self._tab_logs.addTab(edit, tab_title.replace("&", "&&"))
        self._tab_logs.setCurrentIndex(idx)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        state: dict[str, Any] = {
            "edit": edit,
            "path": path,
            "pos": 0,
            "decoder": decoder,
        }
        timer = QTimer(self)
        state["timer"] = timer
        timer.setInterval(350)

        def tick() -> None:
            st = self._live_tail_sessions.get(key)
            if st:
                self._tail_read_chunk(st, final=False)

        timer.timeout.connect(tick)
        timer.start()
        self._live_tail_sessions[key] = state
        self._update_tab_close_buttons()

    def _update_tab_close_buttons(self) -> None:
        """Hide close (×) on Welcome and on the active session log tab; restore defaults for other tabs."""
        if not self._tab_logs.tabsClosable():
            return
        bar = self._tab_logs.tabBar()
        welcome = getattr(self, "_welcome_tab_root", None)
        # Recreate default close buttons (clears stale setTabButton(None) after session ref is cleared).
        self._tab_logs.setTabsClosable(False)
        self._tab_logs.setTabsClosable(True)
        for i in range(self._tab_logs.count()):
            w = self._tab_logs.widget(i)
            if w is welcome:
                bar.setTabButton(i, QTabBar.ButtonPosition.RightSide, None)
            elif self._active_session_log is not None and w is self._active_session_log:
                bar.setTabButton(i, QTabBar.ButtonPosition.RightSide, None)

    def _focus_session_log_tab(self) -> None:
        """Bring the device session log to the front, or open a new one if connected but the tab was removed."""
        if self._active_session_log is not None:
            i = self._tab_logs.indexOf(self._active_session_log)
            if i >= 0:
                self._tab_logs.setCurrentIndex(i)
                return
        if self._device_connected:
            log = self._new_log_editor()
            title = (self._prompt_model_name or "Device").strip() or "Device"
            if len(title) > 36:
                title = title[:33] + "…"
            idx = self._tab_logs.addTab(log, title.replace("&", "&&"))
            self._tab_logs.setCurrentIndex(idx)
            self._active_session_log = log
            log.setPlainText(
                "[Session log reopened. Output that appeared while this tab was closed may be above on the Welcome tab.]\n\n"
            )
            self._update_tab_close_buttons()
            return
        QMessageBox.information(
            self,
            "Session log",
            "Connect to a device first. The session log tab opens when you start connecting.",
        )

    @Slot(int)
    def _on_tab_close_requested(self, index: int) -> None:
        w = self._tab_logs.widget(index)
        if w is None:
            return
        if self._active_session_log is not None and w is self._active_session_log:
            return
        if w is self._welcome_tab_root:
            return
        if self._e3_reference_widget is not None and w is self._e3_reference_widget:
            self._e3_reference_widget = None
            self._tab_logs.removeTab(index)
            w.deleteLater()
            self._update_tab_close_buttons()
            return
        key_to_remove: str | None = None
        for key, st in self._live_tail_sessions.items():
            if st.get("edit") is w:
                key_to_remove = key
                break
        if key_to_remove is not None:
            state = self._live_tail_sessions.pop(key_to_remove)
            timer = state.get("timer")
            if timer is not None:
                timer.stop()
                timer.deleteLater()
            self._tail_read_chunk(state, final=True)
        self._tab_logs.removeTab(index)
        w.deleteLater()
        self._update_tab_close_buttons()

    @Slot(str)
    def _on_tail_live_stop(self, path: str) -> None:
        key = self._tail_path_key(path)
        state = self._live_tail_sessions.pop(key, None)
        if state is not None:
            self._finalize_live_tail_state(state)

    def _shutdown_all_live_tail_tabs(self) -> None:
        for key in list(self._live_tail_sessions.keys()):
            state = self._live_tail_sessions.pop(key, None)
            if state is not None:
                self._finalize_live_tail_state(state)

    def _make_cmd_panel_separator(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        line.setFixedHeight(1)
        line.setStyleSheet(
            "QFrame { background-color: #3d4654; border: none; margin-top: 8px; margin-bottom: 4px; }"
        )
        return line

    def _clear_cmd_panel_body(self) -> None:
        self._cmd_filter_rows.clear()
        self._cmd_advanced_rows.clear()
        self._cmd_panel_groups.clear()
        self._cmd_tier1_outer = None
        self._cmd_tools_outer = None
        self._cmd_adv_block_ref = None
        self._cmd_sep_t1_tools = None
        self._cmd_sep_tools_adv = None
        while self._cmd_body_layout.count():
            item = self._cmd_body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _cmd_sidebar_run(self, cmd: str) -> None:
        c = (cmd or "").strip()
        if c:
            self._submit_command_line(c)

    def _cmd_sidebar_prefill(self, cmd: str) -> None:
        c = (cmd or "").strip()
        if c:
            self._cmd_input.setText(c)

    def _append_cmd_sidebar_row(
        self,
        layout: QVBoxLayout,
        *,
        cmd_key: str,
        display_label: str | None = None,
        args_hint: str = "",
        meta: dict | None = None,
        tier: int,
        group: _CollapsibleCategoryBlock | _AdvancedTierBlock | None,
        is_advanced: bool = False,
    ) -> None:
        disp = (display_label or _display_command_label(cmd_key)).strip()
        tip = _format_command_hover(cmd_key, meta or {})
        row = _CommandRowFrame(
            cmd_key=cmd_key,
            display_line=disp,
            args_hint=args_hint,
            tooltip=tip,
            tier=tier,
            on_run=self._cmd_sidebar_run,
            on_prefill=self._cmd_sidebar_prefill,
            parent=self._cmd_scroll_content,
        )
        layout.addWidget(row)
        needle = f"{disp} {cmd_key}".lower()
        self._cmd_filter_rows.append((row, needle))
        if group is not None:
            group.register_row(row)
        if is_advanced:
            self._cmd_advanced_rows.append(row)

    def _apply_cmd_panel_filter(self) -> None:
        q = (self._cmd_filter_edit.text() or "").strip().lower()
        if not q:
            for row, _ in self._cmd_filter_rows:
                row.setVisible(True)
            for grp in self._cmd_panel_groups:
                grp.setVisible(True)
            if self._cmd_tier1_outer is not None:
                self._cmd_tier1_outer.setVisible(True)
            if self._cmd_tools_outer is not None:
                self._cmd_tools_outer.setVisible(True)
            if self._cmd_adv_block_ref is not None:
                self._cmd_adv_block_ref.setVisible(True)
            if self._cmd_sep_t1_tools is not None:
                self._cmd_sep_t1_tools.setVisible(self._cmd_tier1_outer is not None)
            if self._cmd_sep_tools_adv is not None:
                self._cmd_sep_tools_adv.setVisible(self._cmd_adv_block_ref is not None)
            return

        for row, hay in self._cmd_filter_rows:
            row.setVisible(q in hay)

        matched_adv = any(r.isVisible() for r in self._cmd_advanced_rows)
        if matched_adv and self._cmd_adv_block_ref is not None:
            self._cmd_adv_block_ref.set_expanded(True)

        for grp in self._cmd_panel_groups:
            vis = any(r.isVisible() for r in grp._row_widgets)
            grp.setVisible(vis)

        if self._cmd_tier1_outer is not None:
            t1_any = any(
                r.isVisible()
                for r, _ in self._cmd_filter_rows
                if self._cmd_tier1_outer.isAncestorOf(r)
            )
            self._cmd_tier1_outer.setVisible(t1_any)

        if self._cmd_tools_outer is not None:
            tt_any = any(
                r.isVisible()
                for r, _ in self._cmd_filter_rows
                if self._cmd_tools_outer.isAncestorOf(r)
            )
            self._cmd_tools_outer.setVisible(tt_any)

        if self._cmd_adv_block_ref is not None:
            self._cmd_adv_block_ref.setVisible(any(r.isVisible() for r in self._cmd_advanced_rows))

        if self._cmd_sep_t1_tools is not None:
            t1v = self._cmd_tier1_outer is not None and self._cmd_tier1_outer.isVisible()
            ttv = self._cmd_tools_outer is not None and self._cmd_tools_outer.isVisible()
            self._cmd_sep_t1_tools.setVisible(t1v and ttv)

        if self._cmd_sep_tools_adv is not None:
            ttv = self._cmd_tools_outer is not None and self._cmd_tools_outer.isVisible()
            advv = self._cmd_adv_block_ref is not None and self._cmd_adv_block_ref.isVisible()
            self._cmd_sep_tools_adv.setVisible(ttv and advv)

    @Slot(str)
    def _on_cmd_filter_text_changed(self, _text: str) -> None:
        self._apply_cmd_panel_filter()

    def _set_command_list_disconnected(self) -> None:
        self._clear_cmd_panel_body()
        self._cmd_filter_edit.blockSignals(True)
        self._cmd_filter_edit.clear()
        self._cmd_filter_edit.blockSignals(False)
        self._cmd_filter_row.setVisible(False)
        empty = QWidget()
        el = QVBoxLayout(empty)
        el.setContentsMargins(12, 20, 12, 20)
        el.addStretch(1)
        head_font = QFont()
        head_font.setPointSize(12)
        head_font.setBold(True)
        h1 = QLabel("No device connected")
        h1.setFont(head_font)
        h1.setWordWrap(True)
        h1.setAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignTop)
        h1.setStyleSheet("color: #666666; border: none; background: transparent;")
        el.addWidget(h1)
        l2 = QLabel("Click Connect to load\ncommands for your device")
        l2.setAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignTop)
        l2.setWordWrap(True)
        l2.setStyleSheet("color: #555555; font-size: 11px; border: none; background: transparent;")
        el.addWidget(l2)
        el.addStretch(2)
        self._cmd_body_layout.addWidget(empty)

    @Slot(str)
    def _on_append_log(self, text: str) -> None:
        w = self._log_target()
        w.moveCursor(QTextCursor.MoveOperation.End)
        w.insertPlainText(text)
        w.moveCursor(QTextCursor.MoveOperation.End)
        if w is self._welcome_log and self._welcome_log.toPlainText():
            self._welcome_log.setMinimumHeight(120)
            self._welcome_log.show()

    def _build_device_info_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("deviceInfoPanel")
        panel.setStyleSheet(
            "#deviceInfoPanel { "
            "background-color: #161a20; border: 1px solid #3d4654; border-radius: 6px; }"
        )
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(10)

        head = QHBoxLayout()
        self._dip_model = QLabel("—")
        mf = QFont()
        mf.setPointSize(15)
        mf.setBold(True)
        self._dip_model.setFont(mf)
        self._dip_model.setStyleSheet("color: #e8eef4; border: none; background: transparent;")
        head.addWidget(self._dip_model, 0, Qt.AlignmentFlag.AlignVCenter)
        self._dip_badge = QLabel("Onboarded")
        self._dip_badge.setVisible(False)
        self._dip_badge.setStyleSheet(
            "QLabel { background-color: #3949ab; color: #e8eaf6; border-radius: 10px; "
            "padding: 3px 10px; font-size: 11px; font-weight: 600; }"
        )
        head.addWidget(self._dip_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        outer.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        def _mk_lbl(txt: str) -> QLabel:
            z = QLabel(txt)
            z.setStyleSheet("color: #8b95a5; font-size: 11px; border: none; background: transparent;")
            return z

        r = 0
        grid.addWidget(_mk_lbl("Firmware"), r, 0, Qt.AlignmentFlag.AlignTop)
        self._dip_fw = QLabel("—")
        self._dip_fw.setStyleSheet("color: #c5ced9; font-size: 13px; border: none; background: transparent;")
        self._dip_fw.setWordWrap(True)
        grid.addWidget(self._dip_fw, r, 1)
        r += 1

        grid.addWidget(_mk_lbl("Connection"), r, 0, Qt.AlignmentFlag.AlignTop)
        self._dip_conn = QLabel("—")
        self._dip_conn.setStyleSheet("color: #c5ced9; font-size: 13px; border: none; background: transparent;")
        self._dip_conn.setWordWrap(True)
        grid.addWidget(self._dip_conn, r, 1)
        r += 1

        grid.addWidget(_mk_lbl("Device ID"), r, 0, Qt.AlignmentFlag.AlignTop)
        self._dip_did = QLabel("—")
        mono = QFont("Menlo", 10) if sys.platform == "darwin" else QFont("Consolas", 10)
        self._dip_did.setFont(mono)
        self._dip_did.setStyleSheet(
            "color: #aeb8c4; font-size: 12px; border: none; background: transparent; "
            "font-family: Consolas, Menlo, monospace;"
        )
        self._dip_did.setWordWrap(True)
        self._dip_did.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(self._dip_did, r, 1)
        r += 1

        grid.addWidget(_mk_lbl("Stage / URL env"), r, 0, Qt.AlignmentFlag.AlignTop)
        self._dip_env = QLabel("—")
        self._dip_env.setStyleSheet("color: #c5ced9; font-size: 13px; border: none; background: transparent;")
        self._dip_env.setWordWrap(True)
        grid.addWidget(self._dip_env, r, 1)
        r += 1

        grid.addWidget(_mk_lbl("Command profile"), r, 0, Qt.AlignmentFlag.AlignTop)
        self._dip_profile = QLabel("—")
        self._dip_profile.setStyleSheet("color: #7a8494; font-size: 12px; border: none; background: transparent;")
        grid.addWidget(self._dip_profile, r, 1)
        outer.addLayout(grid)

        raw_l = QLabel("Build info (raw)")
        raw_l.setStyleSheet("color: #8b95a5; font-size: 11px; border: none; background: transparent;")
        outer.addWidget(raw_l)
        self._dip_raw = QTextEdit()
        self._dip_raw.setReadOnly(True)
        self._dip_raw.setMaximumHeight(140)
        self._dip_raw.setFont(mono)
        self._dip_raw.setStyleSheet(
            "QTextEdit { background-color: #0d1117; color: #aeb8c4; border: 1px solid #2a313a; "
            "border-radius: 4px; padding: 6px; }"
        )
        outer.addWidget(self._dip_raw)

        panel.hide()
        return panel

    def _refresh_device_info_panel(self, info: dict) -> None:
        if not info.get("connected"):
            self._device_info_panel.hide()
            return
        self._device_info_panel.show()
        model = str(info.get("model") or "—")
        self._dip_model.setText(model)
        self._dip_fw.setText(str(info.get("fw") or "—"))
        ct = str(info.get("conn_type") or "—")
        did = (info.get("device_id") or "").strip()
        self._dip_conn.setText(ct)
        self._dip_did.setText(did if did else "—")
        self._dip_env.setText(str(info.get("env") or "—"))
        self._dip_profile.setText(str(info.get("command_profile") or "none"))
        ob = info.get("is_onboarded")
        self._dip_badge.setVisible(ob is True)
        raw = (info.get("raw_build_info") or "").strip()
        self._dip_raw.setPlainText(raw if raw else "(no build_info captured)")
        self._dip_raw.verticalScrollBar().setValue(0)

    @Slot(dict)
    def _on_state_changed(self, info: dict) -> None:
        if info.get("connected"):
            self._device_connected = True
            self._command_profile = str(info.get("command_profile") or "none")
            self._conn_type = str(info.get("conn_type") or "")
            self._action_fw_setup.setEnabled(self._command_profile == "e3_wired")
            self._btn_disconnect.setEnabled(True)
            self._cmd_input.setEnabled(True)
            model = info.get("model") or "—"
            ct = info.get("conn_type") or "—"
            did = (info.get("device_id") or "").strip()
            env = info.get("env") or "—"
            transport = f"{ct} {did}".strip() if did else str(ct)
            self._status_detail_model = str(model)
            self._status_detail_fw = str(info.get("fw") or "—")
            self._status_detail_transport = transport
            self._status_detail_env = str(env)
            raw_ob = info.get("is_onboarded")
            self._device_is_onboarded = raw_ob if isinstance(raw_ob, bool) else None
            self._status_phase = "connected"
            self._prompt_model_name = str(model).strip() or "Device"
            self._set_active_session_tab_title(self._prompt_model_name)
            self._sync_status_strip()
            self._refresh_device_info_panel(info)
            self._update_tab_close_buttons()
        else:
            self._device_connected = False
            self._command_profile = "none"
            self._conn_type = ""
            self._action_fw_setup.setEnabled(False)
            self._btn_disconnect.setEnabled(False)
            self._cmd_input.setEnabled(False)
            self._status_phase = "disconnected"
            self._prompt_model_name = "Device"
            self._device_is_onboarded = None
            self._status_detail_fw = "—"
            self._active_session_log = None
            self._sync_status_strip()
            self._refresh_device_info_panel(info)
            self._set_command_list_disconnected()
            self._update_tab_close_buttons()

    @Slot(list)
    def _merge_command_list(self, device_cmds: list) -> None:
        prof = getattr(self, "_command_profile", "none") or "none"
        conn = getattr(self, "_conn_type", "") or ""
        self._clear_cmd_panel_body()
        self._cmd_filter_row.setVisible(True)
        _, advanced = get_visible_commands(list(device_cmds))

        help_lines = get_abstract_command_help_lines()
        abs_with_names = [
            d
            for d in ABSTRACT_DEFINITIONS
            if isinstance(d, dict) and (d.get("name") or "").strip()
        ]
        help_by_name: dict[str, str] = {}
        for d, hl in zip(abs_with_names, help_lines):
            help_by_name[(d.get("name") or "").strip()] = hl

        if prof == "e3_wired":
            tier1 = QWidget(self._cmd_scroll_content)
            t1 = QVBoxLayout(tier1)
            t1.setContentsMargins(0, 0, 0, 0)
            t1.setSpacing(0)
            for cat_title, name_set in _ABSTRACT_CATEGORY_ORDER:
                block_cmds = [
                    d for d in abs_with_names if (d.get("name") or "").strip() in name_set
                ]
                if not block_cmds:
                    continue
                blk = _CollapsibleCategoryBlock(cat_title, expanded_default=True, parent=tier1)
                self._cmd_panel_groups.append(blk)
                bl = blk.body_layout()
                for d in block_cmds:
                    nm = (d.get("name") or "").strip()
                    hl = help_by_name.get(nm, "")
                    self._append_cmd_sidebar_row(
                        bl,
                        cmd_key=nm,
                        args_hint=_abstract_args_hint(nm),
                        meta={"description": hl, "category": "abstract"},
                        tier=1,
                        group=blk,
                    )
                t1.addWidget(blk)
            if t1.count() > 0:
                self._cmd_tier1_outer = tier1
                self._cmd_body_layout.addWidget(tier1)
                self._cmd_sep_t1_tools = self._make_cmd_panel_separator()
                self._cmd_body_layout.addWidget(self._cmd_sep_t1_tools)
            else:
                tier1.deleteLater()

        self._cmd_tools_outer = QWidget(self._cmd_scroll_content)
        to_lay = QVBoxLayout(self._cmd_tools_outer)
        to_lay.setContentsMargins(0, 4, 0, 0)
        to_lay.setSpacing(0)
        tools_hdr = QLabel("⚙  Tools")
        tools_hdr.setStyleSheet(
            "color: #8b95a5; font-size: 11px; font-weight: 600; padding: 8px 6px 4px 6px;"
        )
        to_lay.addWidget(tools_hdr)

        tool_names = {t.get("name", "").strip() for t in get_tools_for_profile(prof, conn)}
        buckets: defaultdict[str, list[dict]] = defaultdict(list)
        for c in get_system_commands_for_profile(prof):
            if not isinstance(c, dict):
                continue
            n = (c.get("name") or "").strip()
            if n in tool_names:
                sg = _tool_subgroup_for_system_name(n)
                if sg:
                    buckets[sg].append(c)
            elif _tool_subgroup_for_system_name(n) == "CONFIG":
                buckets["CONFIG"].append(c)
            else:
                buckets["SESSION"].append(c)
        for key in list(buckets.keys()):
            buckets[key].sort(key=lambda x: _display_command_label(str(x.get("name", ""))).lower())

        for sub in _TOOL_SUBGROUP_ORDER:
            cmds = buckets.get(sub)
            if not cmds:
                continue
            blk = _CollapsibleCategoryBlock(sub, expanded_default=True, parent=self._cmd_tools_outer)
            self._cmd_panel_groups.append(blk)
            bl = blk.body_layout()
            for c in cmds:
                nk = str(c.get("name") or "").strip()
                if not nk:
                    continue
                self._append_cmd_sidebar_row(
                    bl,
                    cmd_key=nk,
                    display_label=_display_command_label(nk),
                    args_hint="",
                    meta=c,
                    tier=2,
                    group=blk,
                )
            to_lay.addWidget(blk)
        self._cmd_body_layout.addWidget(self._cmd_tools_outer)

        if advanced:
            self._cmd_sep_tools_adv = self._make_cmd_panel_separator()
            self._cmd_body_layout.addWidget(self._cmd_sep_tools_adv)
            adv = _AdvancedTierBlock(self._cmd_scroll_content)
            self._cmd_adv_block_ref = adv
            adv_body = adv.body_layout()
            by_cat: defaultdict[str, list[dict]] = defaultdict(list)
            for c in advanced:
                if not isinstance(c, dict):
                    continue
                cat = (str(c.get("category") or "other")).strip().lower() or "other"
                by_cat[cat].append(c)
            ordered_cats: list[str] = []
            seen_cat: set[str] = set()
            for k in _ADV_DEVICE_CATEGORY_ORDER:
                if k in by_cat:
                    ordered_cats.append(k)
                    seen_cat.add(k)
            for k in sorted(by_cat.keys()):
                if k not in seen_cat:
                    ordered_cats.append(k)
            for cat_key in ordered_cats:
                cmds = sorted(by_cat[cat_key], key=lambda x: str(x.get("name", "")).lower())
                if not cmds:
                    continue
                cblk = _CollapsibleCategoryBlock(
                    cat_key.upper(), expanded_default=True, parent=self._cmd_scroll_content
                )
                self._cmd_panel_groups.append(cblk)
                cbl = cblk.body_layout()
                for c in cmds:
                    nk = str(c.get("name") or "").strip()
                    if not nk:
                        continue
                    self._append_cmd_sidebar_row(
                        cbl,
                        cmd_key=nk,
                        args_hint=_device_command_args_hint(c),
                        meta=c,
                        tier=3,
                        group=cblk,
                        is_advanced=True,
                    )
                adv_body.addWidget(cblk)
            self._cmd_body_layout.addWidget(adv)

        self._apply_cmd_panel_filter()

    @Slot(str, object)
    def _on_command_finished(self, action: str, _message: object) -> None:
        if action == "exit":
            self.close()
        elif action == "back":
            self._disconnect()
        elif action == "disconnected":
            QMessageBox.warning(
                self,
                "Disconnected",
                "The camera disconnected from the PC. Use Connect to try again.",
            )
            self._disconnect()

    @Slot(str)
    def _on_connect_failed(self, msg: str) -> None:
        self._finalize_failed_session_tab()
        self._status_phase = "disconnected"
        self._sync_status_strip()
        QMessageBox.critical(self, "Connection failed", msg)

    def _disconnect(self) -> None:
        self._worker.disconnect_session.emit()

    def _clear_log(self) -> None:
        w = self._tab_logs.currentWidget()
        if w is self._welcome_tab_root:
            self._welcome_log.clear()
            self._welcome_log.setMinimumHeight(0)
            self._welcome_log.hide()
        elif isinstance(w, QTextEdit):
            w.clear()

    def _submit_command_line(self, line: str) -> None:
        line = (line or "").strip()
        if not line:
            return
        model = getattr(self, "_prompt_model_name", "Device") or "Device"
        w = self._log_target()
        w.append(f"\n{model}> {line}\n")
        self._worker.submit_command.emit(line)

    def _send_command(self) -> None:
        line = self._cmd_input.text().strip()
        if not line:
            return
        self._cmd_input.clear()
        self._submit_command_line(line)

    def _run_help(self) -> None:
        self._submit_command_line("help")

    def _open_connect_dialog(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Connect to camera")
        layout = QVBoxLayout(dlg)

        models = get_models()
        device_combo = QComboBox()
        for m in models:
            conns = format_supported_connections(m.get("supported_connections"))
            device_combo.addItem(f"{m['name']}  ({conns})", m)

        method = QComboBox()

        uart_box = QGroupBox("UART")
        uart_form = QFormLayout(uart_box)
        uart_port = QComboBox()
        uart_refresh = QPushButton("Refresh ports")
        ports = list_uart_ports()
        for port, desc in ports:
            uart_port.addItem(f"{desc} ({port})", port)
        uart_baud = QSpinBox()
        uart_baud.setRange(1200, 921600)
        uart_baud.setValue(DEFAULT_UART_BAUD)
        uart_row = QHBoxLayout()
        uart_row.addWidget(uart_port)
        uart_row.addWidget(uart_refresh)
        w_uart_row = QWidget()
        w_uart_row.setLayout(uart_row)
        uart_form.addRow("Port:", w_uart_row)
        uart_form.addRow("Baud:", uart_baud)

        def refresh_ports() -> None:
            uart_port.clear()
            for p, desc in list_uart_ports():
                uart_port.addItem(f"{desc} ({p})", p)

        uart_refresh.clicked.connect(refresh_ports)

        adb_box = QGroupBox("ADB")
        adb_form = QFormLayout(adb_box)
        adb_pwd = QLineEdit()
        adb_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        adb_form.addRow("Shell password:", adb_pwd)

        ssh_box = QGroupBox("SSH")
        ssh_form = QFormLayout(ssh_box)
        ssh_ip = QLineEdit()
        ssh_port = QSpinBox()
        ssh_port.setRange(1, 65535)
        ssh_port.setValue(DEFAULT_SSH_PORT)

        def _ssh_default_for_model(m: dict | None) -> int:
            if not m:
                return DEFAULT_SSH_PORT
            ds = m.get("default_settings") or {}
            return int((ds.get("ssh") or {}).get("port") or DEFAULT_SSH_PORT)

        def _refill_methods() -> None:
            method.clear()
            m = device_combo.currentData()
            if not isinstance(m, dict):
                return
            allowed = {str(x).upper() for x in (m.get("supported_connections") or [])}
            opts = [
                ("UART (serial)", "UART"),
                ("ADB (USB)", "ADB"),
                ("SSH", "SSH"),
            ]
            for label, key in opts:
                if not allowed or key in allowed:
                    method.addItem(label, key)
            if method.count() == 0:
                for label, key in opts:
                    method.addItem(label, key)
            ssh_port.setValue(_ssh_default_for_model(m))

        device_combo.currentIndexChanged.connect(lambda _i: _refill_methods())
        _refill_methods()
        ssh_user = QLineEdit("root")
        ssh_pwd = QLineEdit()
        ssh_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        ssh_form.addRow("IP:", ssh_ip)
        ssh_form.addRow("Port:", ssh_port)
        ssh_form.addRow("Username:", ssh_user)
        ssh_form.addRow("Password:", ssh_pwd)

        stack = QWidget()
        stack_layout = QVBoxLayout(stack)
        stack_layout.setContentsMargins(0, 0, 0, 0)
        stack_layout.addWidget(uart_box)
        stack_layout.addWidget(adb_box)
        stack_layout.addWidget(ssh_box)

        def update_visible(_idx: int) -> None:
            key = method.currentData()
            uart_box.setVisible(key == "UART")
            adb_box.setVisible(key == "ADB")
            ssh_box.setVisible(key == "SSH")

        method.currentIndexChanged.connect(update_visible)
        update_visible(method.currentIndex())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)

        layout.addWidget(QLabel("Device (model and supported connections):"))
        layout.addWidget(device_combo)
        layout.addWidget(QLabel("Connection method:"))
        layout.addWidget(method)
        layout.addWidget(stack)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        key = method.currentData()
        if key == "UART":
            if uart_port.count() == 0:
                QMessageBox.warning(self, "UART", "No serial ports found.")
                return
            self._begin_connection_log_tab()
            port = uart_port.currentData()
            baud = uart_baud.value()
            self._worker.connect_uart.emit(port, baud)
        elif key == "ADB":
            self._begin_connection_log_tab()
            self._worker.connect_adb.emit(adb_pwd.text())
        elif key == "SSH":
            ip = ssh_ip.text().strip()
            if not ip:
                QMessageBox.warning(self, "SSH", "Enter an IP address.")
                return
            self._begin_connection_log_tab()
            self._worker.connect_ssh.emit(
                ip,
                ssh_port.value(),
                ssh_user.text().strip() or "root",
                ssh_pwd.text(),
            )
        else:
            QMessageBox.warning(self, "Connect", "Select a connection method.")

    # --- Blocking prompt slots (invoked from worker thread via GuiBridge) ---

    @Slot()
    def guiBlockingAskText(self) -> None:
        text, ok = QInputDialog.getText(
            self,
            "Arlo",
            self._bridge._text_prompt,
            QLineEdit.EchoMode.Normal,
            self._bridge._text_default,
        )
        self._bridge._text_result = text if ok else None

    @Slot()
    def guiBlockingAskPassword(self) -> None:
        text, ok = QInputDialog.getText(
            self,
            "Arlo",
            self._bridge._pwd_prompt,
            QLineEdit.EchoMode.Password,
        )
        self._bridge._pwd_result = text if ok else None

    @Slot()
    def guiBlockingAskConfirm(self) -> None:
        r = QMessageBox.question(
            self,
            "Arlo",
            self._bridge._confirm_message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes if self._bridge._confirm_default else QMessageBox.StandardButton.No,
        )
        self._bridge._confirm_result = r == QMessageBox.StandardButton.Yes

    @Slot()
    def guiBlockingAskSelect(self) -> None:
        item, ok = QInputDialog.getItem(
            self,
            "Arlo",
            self._bridge._select_title,
            self._bridge._select_labels,
            0,
            False,
        )
        if ok and item is not None:
            try:
                i = self._bridge._select_labels.index(item)
                self._bridge._select_result = self._bridge._select_values[i]
            except ValueError:
                self._bridge._select_result = _SELECT_CANCELLED
        else:
            self._bridge._select_result = _SELECT_CANCELLED

    def closeEvent(self, event: Any) -> None:
        self._shutdown_all_live_tail_tabs()
        set_tail_live_view_handlers(None, None)
        self._worker.disconnect_session.emit()
        self._thread.quit()
        self._thread.wait(5000)
        import interface.menus as menus
        import interface.prompts as prompts

        menus.set_gui_menu_bridge(None)
        menus.console = Console()
        prompts.set_gui_prompt_bridge(None)
        super().closeEvent(event)
