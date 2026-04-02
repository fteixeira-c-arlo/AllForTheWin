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

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QRect, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QFont,
    QFontMetrics,
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
    QDockWidget,
    QFormLayout,
    QFrame,
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
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from rich.console import Console

from core.device_connection import detect_after_connect, ensure_adb_allowed_for_selection
from core.device_credentials import get_adb_password_for_model
from core.device_errors import UnknownDeviceError, UnsupportedConnectionError
from core.device_registry import lookup_registry_by_model_id
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


def _safe_set_point_size(font: QFont, size: int, *, context: str = "") -> None:
    if size > 0:
        font.setPointSize(size)
        return
    print(f"DEBUG: font size was {size}, clamped to minimum ({context})")
    font.setPointSize(10)


def _ensure_explicit_font_size(font: QFont, *, context: str = "") -> None:
    """Avoid propagating Qt's 'unset' point size (-1), which can trigger QFont::setPointSize warnings."""
    if font.pointSize() > 0:
        return
    if font.pixelSize() > 0:
        return
    app = QApplication.instance()
    if app is not None:
        af = app.font()
        if af.pointSize() > 0:
            font.setPointSize(af.pointSize())
            print(f"DEBUG: font size was -1, using application point size ({context})")
            return
        if af.pixelSize() > 0:
            font.setPixelSize(af.pixelSize())
            print(f"DEBUG: font size was -1, using application pixel size ({context})")
            return
    font.setPointSize(10)
    print(f"DEBUG: font size was -1, clamped to 10 ({context})")


def _env_stage_badge_qss(env_raw: str) -> str:
    """Distinct pill colors for common FW stages (dark UI). Uses internal env value for matching."""
    e = (env_raw or "").lower().strip()
    if not e or e == "—":
        bg, fg = "#455a64", "#eceff1"
    elif "staging" in e or "stage" in e:
        bg, fg = "#e65100", "#fff3e0"
    elif "qa" in e or e == "dev" or "ftrial" in e:
        bg, fg = "#1565c0", "#e3f2fd"
    elif "prod" in e:
        bg, fg = "#2e7d32", "#e8f5e9"
    else:
        bg, fg = "#455a64", "#eceff1"
    return (
        f"QLabel {{ background-color: {bg}; color: {fg}; border-radius: 10px; "
        "padding: 4px 12px; font-size: 12px; font-weight: 700; }"
    )


def _env_stage_display_label(env_raw: str) -> str:
    """User-facing env badge text; colors still come from _env_stage_badge_qss(env_raw)."""
    e = (env_raw or "").strip()
    if not e or e == "—":
        return "—"
    el = e.lower().replace("-", "_")
    if el == "qa":
        return "GQA"
    if el == "dev":
        return "GDev"
    if el == "prod" or el.startswith("prod_"):
        return "Production"
    return e


def _elide_status_value(text: str, max_px: int, fm: QFontMetrics) -> str:
    t = text or ""
    if not t or t == "—":
        return t or "—"
    if fm.horizontalAdvance(t) <= max_px:
        return t
    return fm.elidedText(t, Qt.TextElideMode.ElideMiddle, max_px)


class _CopyableValueLabel(QLabel):
    """Click to copy; optional elided display with full text in tooltip."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._copy_text: str = ""
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

    def set_copy_value(self, full_text: str, display_text: str | None = None) -> None:
        self._copy_text = (full_text or "").strip()
        disp = display_text if display_text is not None else (full_text or "—")
        self.setText(disp if disp else "—")
        if self._copy_text and self._copy_text != "—":
            self.setToolTip(f"{self._copy_text}\n\nClick to copy")
        else:
            self.setToolTip("Click to copy")

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            t = self._copy_text
            if t and t != "—":
                QApplication.clipboard().setText(t)
                QToolTip.showText(
                    event.globalPosition().toPoint(),
                    "Copied!",
                    self,
                    QRect(),
                    1500,
                )
        super().mousePressEvent(event)


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
    if disp in ("fw local", "fw wizard", "server stop", "server status"):
        return "FIRMWARE"
    if disp in ("log tail", "log tail stop", "log parse", "log parse stop", "log export"):
        return "LOGS"
    if raw.startswith("config_"):
        return "CONFIG"
    return None


_TOOL_SUBGROUP_ORDER = ("FIRMWARE", "LOGS", "CONFIG", "SESSION")


class _AdbPickerDeviceCard(QFrame):
    """One selectable row: ADB USB serial only (no device queries)."""

    def __init__(
        self,
        dialog: "_AdbDevicePickerDialog",
        serial: str,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._dialog = dialog
        self._serial = serial
        self._selected = False
        self._hover = False
        self.setObjectName("adbDeviceCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        mono = QFont("Menlo" if sys.platform == "darwin" else "Consolas")
        mono.setPixelSize(14)

        inlay = QVBoxLayout(self)
        inlay.setContentsMargins(14, 12, 14, 12)
        inlay.setSpacing(0)

        lab = QLabel(serial)
        lab.setObjectName("adbPickerSerial")
        lab.setFont(mono)
        lab.setStyleSheet("color: #e8eef4; background: transparent; border: none;")
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        inlay.addWidget(lab)

        self._apply_frame_style()

    def serial(self) -> str:
        return self._serial

    def set_card_selected(self, selected: bool) -> None:
        self._selected = selected
        self._apply_frame_style()

    def _apply_frame_style(self) -> None:
        accent = ARLO_ACCENT_COLOR
        if self._selected:
            self.setStyleSheet(
                f"QFrame#adbDeviceCard {{ background-color: rgba(0, 137, 123, 0.22); "
                f"border: 2px solid {accent}; border-radius: 8px; }}"
            )
        elif self._hover:
            self.setStyleSheet(
                "QFrame#adbDeviceCard { background-color: #2a3038; border: 1px solid #5a6570; border-radius: 8px; }"
            )
        else:
            self.setStyleSheet(
                "QFrame#adbDeviceCard { background-color: #181c22; border: 1px solid #3d4650; border-radius: 8px; }"
            )

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dialog._on_card_clicked(self)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dialog._accept_serial(self._serial)
        super().mouseDoubleClickEvent(event)

    def enterEvent(self, event: Any) -> None:
        self._hover = True
        self._apply_frame_style()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self._hover = False
        self._apply_frame_style()
        super().leaveEvent(event)


class _AdbDevicePickerDialog(QDialog):
    """Dark-themed ADB USB device chooser: serial IDs only (from ``adb devices``)."""

    def __init__(self, parent: QWidget | None, serials: list[str]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select device")
        self._chosen: str | None = None
        self._cards: list[_AdbPickerDeviceCard] = []
        self._selected: _AdbPickerDeviceCard | None = None
        accent = ARLO_ACCENT_COLOR
        self.setStyleSheet(
            f"""
            QDialog {{ background-color: #1a1a1a; color: #e8eef4; }}
            QLabel {{ color: #b8c0cc; }}
            QScrollArea {{ border: none; background-color: transparent; }}
            QPushButton {{
                background-color: #2d333b;
                color: #e8eef4;
                border: 1px solid #3d4650;
                border-radius: 4px;
                padding: 6px 16px;
                min-width: 72px;
            }}
            QPushButton:hover {{ background-color: #3a424d; }}
            QPushButton:default {{
                background-color: #1a5c54;
                border-color: {accent};
            }}
            QPushButton:default:hover {{ background-color: #156f66; }}
            """
        )
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        hint = QLabel("Multiple devices found. Choose one:")
        hint.setStyleSheet("color: #b8c0cc; font-size: 12px;")
        lay.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(440)
        scroll.setMinimumHeight(220)
        inner = QWidget()
        inner.setStyleSheet("background-color: transparent;")
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(2, 2, 2, 2)
        inner_lay.setSpacing(9)

        for s in serials:
            card = _AdbPickerDeviceCard(self, s, inner)
            self._cards.append(card)
            inner_lay.addWidget(card)
        inner_lay.addStretch(1)

        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)

        if self._cards:
            self._select_card(self._cards[0])

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_b = QPushButton("Cancel")
        connect_b = QPushButton("Connect")
        connect_b.setDefault(True)
        cancel_b.clicked.connect(self.reject)
        connect_b.clicked.connect(self._on_connect_clicked)
        btn_row.addWidget(cancel_b)
        btn_row.addWidget(connect_b)
        lay.addLayout(btn_row)

    def _on_card_clicked(self, card: _AdbPickerDeviceCard) -> None:
        self._select_card(card)

    def _select_card(self, card: _AdbPickerDeviceCard) -> None:
        self._selected = card
        for c in self._cards:
            c.set_card_selected(c is card)

    def _accept_serial(self, serial: str) -> None:
        s = (serial or "").strip()
        if s:
            self._chosen = s
            self.accept()

    def _on_connect_clicked(self) -> None:
        card = self._selected or (self._cards[0] if self._cards else None)
        if card is None:
            return
        self._accept_serial(card.serial())

    def selected_serial(self) -> str | None:
        return self._chosen


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
            f = QFont(self.font())
            _ensure_explicit_font_size(f, context="_CommandRowFrame tier3 row")
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
                hf = QFont(h.font())
                _ensure_explicit_font_size(hf, context="_CommandRowFrame tier3 hint")
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


_HEARTBEAT_INTERVAL_MS = 4000


def _uart_ports_equivalent(port_a: str, port_b: str) -> bool:
    """True if two serial port names are the same device (e.g. COM3 vs \\\\.\\COM3)."""
    from transports.uart_handler import _port_key_for_match

    return _port_key_for_match(port_a) == _port_key_for_match(port_b)


class SessionWorker(QObject):
    """Owns connection handle; all I/O runs on this object's thread."""

    append_log = Signal(str)
    state_changed = Signal(dict)
    commands_updated = Signal(list)
    command_finished = Signal(str, object)
    connect_failed = Signal(str)

    connect_uart = Signal(str, int, object, str, int)
    connect_adb = Signal(str, object, str)
    connect_ssh = Signal(str, int, str, str, object)
    submit_command = Signal(str)
    disconnect_session = Signal()
    fw_shell_request = Signal(str, object)
    fw_shell_response = Signal(bool, str)

    def __init__(self, bridge: GuiBridge) -> None:
        super().__init__()
        self._bridge = bridge
        self._cfg: ConnectionConfig | None = None
        self._handle: Any = None
        self._mcu_handle: Any = None
        self._device_commands: list[dict] = []
        self._detected: dict[str, Any] = {}
        self._selected_model: dict[str, Any] = {}
        self._command_profile: str = "none"
        self._io_busy = False
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(_HEARTBEAT_INTERVAL_MS)
        self._heartbeat_timer.timeout.connect(self._on_heartbeat_tick)

        self.connect_uart.connect(self._on_connect_uart)  # type: ignore[arg-type]
        self.connect_adb.connect(self._on_connect_adb)  # type: ignore[arg-type]
        self.connect_ssh.connect(self._on_connect_ssh)  # type: ignore[arg-type]
        self.submit_command.connect(self._on_command)
        self.disconnect_session.connect(self._on_disconnect)
        self.fw_shell_request.connect(self._on_fw_shell_request)

    def _emit_state(self) -> None:
        if self._cfg and self._handle:
            name = (self._detected.get("model") or "Device").strip() or "Device"
            m_info = get_model_by_name(name)
            fw_search = (
                list(m_info.get("fw_search_models") or [m_info["name"]])
                if m_info
                else [name]
            )
            self.state_changed.emit(
                {
                    "connected": True,
                    "model": name,
                    "fw": self._detected.get("fw_version") or "—",
                    "serial": self._detected.get("serial") or "",
                    "env": (self._detected.get("env") or "—"),
                    "update_url_raw": (self._detected.get("update_url_raw") or "").strip(),
                    "conn_type": self._cfg.type,
                    "device_id": self._cfg.device_identifier or "",
                    "commands_count": len(self._device_commands),
                    "command_profile": self._command_profile,
                    "is_onboarded": self._detected.get("is_onboarded"),
                    "raw_build_info": self._detected.get("raw_build_info") or "",
                    "fw_search_models": fw_search,
                    "mcu_uart": (
                        (self._mcu_handle.device_identifier() or "")
                        if self._mcu_handle and getattr(self._mcu_handle, "is_connected", lambda: False)()
                        else ""
                    ),
                }
            )
        else:
            self.state_changed.emit({"connected": False})

    @Slot(str, int, object, str, int)
    def _on_connect_uart(
        self,
        port: str,
        baud: int,
        selected: object = None,
        mcu_port: str = "",
        mcu_baud: int = 0,
    ) -> None:
        self._selected_model = selected if isinstance(selected, dict) else {}
        self.append_log.emit("Connecting via UART...\n")
        handler = UARTHandler()
        m = self._selected_model
        plat = (m.get("platform") or "").strip().lower()
        console_style = "amebapro2" if plat == "amebapro2" else "linux_shell"
        legacy = m.get("uart_baudrate_legacy")
        ok, msg, settings = handler.connect(
            port=port,
            baud_rate=baud,
            console_style=console_style,
            legacy_baud=int(legacy) if legacy is not None else None,
            device_display_name=str(m.get("display_name") or m.get("name") or "device"),
        )
        if ok and settings:
            cfg = _make_config(
                "UART",
                settings,
                handler.device_identifier() or f"{port}@{baud}",
            )
            self._cfg = cfg
            self._handle = handler
            self._mcu_handle = None
            self.append_log.emit(f"Connected via UART ({cfg.device_identifier})\n")
            mcu_p = (mcu_port or "").strip()
            if mcu_p and int(mcu_baud or 0) > 0:
                if _uart_ports_equivalent(mcu_p, port):
                    self._handle.disconnect()
                    self._handle = None
                    self._cfg = None
                    err_m = "MCU UART port cannot be the same as the ISP/main UART port."
                    self.append_log.emit(f"\n{err_m}\n\n")
                    self.connect_failed.emit(err_m)
                    return
                mcu_h = UARTHandler()
                m_ok, m_msg, _ = mcu_h.connect(
                    port=mcu_p,
                    baud_rate=int(mcu_baud),
                    console_style="mcu",
                    device_display_name="MCU",
                )
                if not m_ok:
                    self._handle.disconnect()
                    self._handle = None
                    self._cfg = None
                    em = (m_msg or "MCU UART connection failed.").strip()
                    self.append_log.emit(f"\n{em}\n\n")
                    self.connect_failed.emit(em)
                    return
                self._mcu_handle = mcu_h
                self.append_log.emit(
                    f"MCU UART: {mcu_h.device_identifier() or mcu_p} (Gen5 MCU CLI)\n"
                )
            self._run_detect_and_load()
            return
        self._cfg = None
        self._handle = None
        self._mcu_handle = None
        err = (msg or "UART connection failed.").strip()
        self.append_log.emit(f"\nConnection failed: {err}\n\n")
        self.connect_failed.emit(err)

    @Slot(str, object, str)
    def _on_connect_adb(self, password: str, selected: object = None, adb_serial: str = "") -> None:
        self._selected_model = selected if isinstance(selected, dict) else {}
        self.append_log.emit("Connecting via ADB...\n")
        handler = ADBHandler()
        serial_arg = (adb_serial or "").strip() or None
        ok, msg, settings = handler.connect(password=password, device_serial=serial_arg)
        if ok and settings:
            self._mcu_handle = None
            device_id = settings.get("device_serial") or "USB"
            cfg = _make_config("ADB", settings, device_id)
            self._cfg = cfg
            self._handle = handler
            self.append_log.emit(f"Connected via USB ({device_id})\n")
            self._run_detect_and_load()
            return
        self._cfg = None
        self._handle = None
        self._mcu_handle = None
        err = (msg or "ADB connection failed.").strip()
        self.append_log.emit(f"\nConnection failed: {err}\n\n")
        self.connect_failed.emit(err)

    @Slot(str, int, str, str, object)
    def _on_connect_ssh(self, ip: str, port: int, username: str, password: str, selected: object = None) -> None:
        self._selected_model = selected if isinstance(selected, dict) else {}
        self.append_log.emit("Connecting via SSH...\n")
        handler = SSHHandler()
        ok, msg, settings = handler.connect(
            ip_address=ip,
            port=port,
            username=username,
            password=password,
        )
        if ok and settings:
            self._mcu_handle = None
            device_id = f"{settings['ip_address']}:{settings['port']}"
            cfg = _make_config("SSH", settings, device_id)
            self._cfg = cfg
            self._handle = handler
            self.append_log.emit(f"Connected at {device_id}\n")
            self._run_detect_and_load()
            return
        self._cfg = None
        self._handle = None
        self._mcu_handle = None
        err = (msg or "SSH connection failed.").strip()
        self.append_log.emit(f"\nConnection failed: {err}\n\n")
        self.connect_failed.emit(err)

    def _run_detect_and_load(self) -> None:
        if not self._handle:
            return
        self.append_log.emit("Detecting device...\n")
        ct = (self._cfg.type if self._cfg else "") or ""
        ct_l = ct.strip().lower()
        if ct_l == "adb":
            conn = "adb"
        elif ct_l == "ssh":
            conn = "ssh"
        else:
            conn = "uart"
        used_legacy = bool((self._cfg.settings or {}).get("used_legacy_uart_baud")) if self._cfg else False
        try:
            self._detected, _dc = detect_after_connect(
                self._handle.execute,
                conn,
                selected_model=self._selected_model,
                used_legacy_uart_baud=used_legacy,
            )
        except UnknownDeviceError as e:
            msg = str(e)
            self.append_log.emit(f"\n{msg}\n\n")
            self._do_disconnect(msg)
            self.connect_failed.emit(msg)
            return
        except UnsupportedConnectionError as e:
            msg = str(e)
            self.append_log.emit(f"\n{msg}\n\n")
            self._do_disconnect(msg)
            self.connect_failed.emit(msg)
            return
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
        self._heartbeat_timer.start()

    def _do_disconnect(self, log_message: str | None) -> None:
        self._heartbeat_timer.stop()
        if self._mcu_handle:
            try:
                self._mcu_handle.disconnect()
            except Exception:
                pass
        self._mcu_handle = None
        if self._handle:
            try:
                self._handle.disconnect()
            except Exception:
                pass
        self._handle = None
        self._cfg = None
        self._detected = {}
        self._selected_model = {}
        self._device_commands = []
        self._command_profile = "none"
        # Log before state_changed so the UI still routes this line to the session tab.
        self.append_log.emit((log_message or "Disconnected.") + "\n\n")
        self._emit_state()

    @Slot()
    def _on_disconnect(self) -> None:
        self._do_disconnect(None)

    def _on_heartbeat_tick(self) -> None:
        if self._io_busy or not self._cfg or not self._handle:
            return
        alive_fn = getattr(self._handle, "transport_heartbeat", None)
        if callable(alive_fn):
            alive = alive_fn()
        else:
            alive = getattr(self._handle, "is_connected", lambda: True)()
        if not alive:
            self._do_disconnect("Connection lost — device disconnected. Use Connect to reconnect.")
            return
        if self._mcu_handle:
            mcu_alive_fn = getattr(self._mcu_handle, "transport_heartbeat", None)
            if callable(mcu_alive_fn) and not mcu_alive_fn():
                try:
                    self._mcu_handle.disconnect()
                except Exception:
                    pass
                self._mcu_handle = None
                self.append_log.emit("MCU UART disconnected (port closed or unplugged).\n")
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

        self._io_busy = True
        try:
            prompt_name = (self._detected.get("model") or "Device").strip() or "Device"
            m_info = get_model_by_name(prompt_name)
            reg = lookup_registry_by_model_id(prompt_name)
            if m_info:
                fw_search = list(m_info.get("fw_search_models") or [m_info["name"]])
            else:
                fw_search = [prompt_name] if self._detected.get("model") else []
            current_model_dict: dict[str, Any] = {
                "name": prompt_name,
                "fw_search_models": fw_search,
                "command_profile": self._command_profile,
                "is_onboarded": self._detected.get("is_onboarded"),
            }
            if m_info:
                current_model_dict.setdefault("display_name", m_info.get("display_name"))
                current_model_dict.setdefault("codename", m_info.get("codename"))
                current_model_dict.setdefault("platform", m_info.get("platform"))
            if reg:
                current_model_dict["codename"] = reg.get("codename")
                current_model_dict["platform"] = reg.get("platform")
                current_model_dict["display_name"] = reg.get("display_name", current_model_dict.get("display_name"))
            mcu_ex = getattr(self._mcu_handle, "execute", None) if self._mcu_handle else None
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
                mcu_connection_execute=mcu_ex if callable(mcu_ex) else None,
            )
            if message:
                self.append_log.emit(_strip_rich_markup(message) + "\n")
            if action == "exit":
                self._do_disconnect(None)
            elif action == "disconnected":
                self._do_disconnect("The camera disconnected from the PC.")
            elif (
                self._handle
                and hasattr(self._handle, "is_connected")
                and not self._handle.is_connected()
            ):
                self._do_disconnect("Connection lost — device disconnected. Use Connect to reconnect.")
            self.command_finished.emit(action, message)
        finally:
            self._io_busy = False

    @Slot(str, object)
    def _on_fw_shell_request(self, command: str, args_obj: object) -> None:
        args = list(args_obj) if isinstance(args_obj, (list, tuple)) else []
        if not self._handle:
            self.fw_shell_response.emit(False, "Not connected.")
            return
        self._io_busy = True
        try:
            ok, text = self._handle.execute(command, args)
            if (not ok and text and "Device disconnected" in text) or (
                self._handle
                and hasattr(self._handle, "is_connected")
                and not self._handle.is_connected()
            ):
                self._do_disconnect("Connection lost — device disconnected. Use Connect to reconnect.")
            self.fw_shell_response.emit(ok, text or "")
        finally:
            self._io_busy = False


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
        self._status_detail_env: str = "—"
        self._status_detail_serial: str = ""
        self._status_update_url_raw: str = ""
        self._device_is_onboarded: bool | None = None
        self._preferred_adb_serial: str | None = None

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
        _safe_set_point_size(title_font, 14, context="sidebar title")
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {ARLO_ACCENT_COLOR};")

        intro = QLabel("Camera developer tool  ·  ADB  ·  SSH  ·  UART")
        intro.setWordWrap(True)

        self._status_strip = QWidget()
        strip_outer = QVBoxLayout(self._status_strip)
        strip_outer.setContentsMargins(0, 6, 0, 4)
        strip_outer.setSpacing(0)

        status_main = QHBoxLayout()
        status_main.setContentsMargins(0, 0, 0, 0)
        status_main.setSpacing(10)

        self._status_dot = QWidget(self._status_strip)
        self._status_dot.setObjectName("statusDot")
        self._status_dot.setFixedSize(10, 10)
        self._status_dot.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._status_dot.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._set_status_dot_color(_STATUS_DOT_DISCONNECTED)
        status_main.addWidget(self._status_dot, 0, Qt.AlignmentFlag.AlignVCenter)

        self._onboarded_badge = QLabel("Onboarded")
        self._onboarded_badge.setVisible(False)
        self._onboarded_badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._onboarded_badge.setStyleSheet(
            "QLabel { background-color: #1b5e20; color: #e8f5e9; border-radius: 10px; "
            "padding: 3px 10px; font-size: 11px; font-weight: 600; }"
        )

        right_stack = QWidget()
        rsv = QVBoxLayout(right_stack)
        rsv.setContentsMargins(0, 0, 0, 0)
        rsv.setSpacing(4)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self._status_text = QLabel("Not connected")
        self._status_text.setWordWrap(True)
        self._status_text.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        )
        self._status_model = QLabel("—")
        self._status_model.setStyleSheet(
            "font-size: 16px; font-weight: 700; color: #e8eef4; border: none; background: transparent;"
        )
        self._status_sep1 = QLabel("·")
        self._status_sep1.setStyleSheet("color: #5c6570; font-size: 13px; background: transparent;")
        self._status_fw = _CopyableValueLabel()
        self._status_fw.setStyleSheet(
            "font-size: 11px; font-weight: 500; color: #8b95a5; border: none; background: transparent;"
        )
        self._status_sep2 = QLabel("·")
        self._status_sep2.setStyleSheet("color: #5c6570; font-size: 13px; background: transparent;")
        self._status_env_pill = QLabel("—")
        self._status_env_pill.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        row1.addWidget(self._onboarded_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        row1.addWidget(self._status_text, 0, Qt.AlignmentFlag.AlignVCenter)
        row1.addWidget(self._status_model, 0, Qt.AlignmentFlag.AlignVCenter)
        row1.addWidget(self._status_sep1, 0, Qt.AlignmentFlag.AlignVCenter)
        row1.addWidget(self._status_fw, 0, Qt.AlignmentFlag.AlignVCenter)
        row1.addWidget(self._status_sep2, 0, Qt.AlignmentFlag.AlignVCenter)
        row1.addWidget(self._status_env_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        row1.addStretch(1)

        self._status_row2 = QWidget()
        row2 = QHBoxLayout(self._status_row2)
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(8)
        self._status_serial_caption = QLabel("Serial")
        self._status_serial_caption.setStyleSheet("color: #7a8494; font-size: 11px; background: transparent;")
        self._status_serial_val = _CopyableValueLabel()
        _mono_sm = QFont("Menlo", 10) if sys.platform == "darwin" else QFont("Consolas", 10)
        self._status_serial_val.setFont(_mono_sm)
        self._status_serial_val.setStyleSheet(
            "color: #9aa5b4; font-size: 11px; font-family: Consolas, Menlo, monospace; "
            "border: none; background: transparent;"
        )
        row2.addWidget(self._status_serial_caption)
        row2.addWidget(self._status_serial_val)
        row2.addStretch(1)

        rsv.addLayout(row1)
        rsv.addWidget(self._status_row2)

        status_main.addWidget(right_stack, 1)
        strip_outer.addLayout(status_main)

        self._sync_status_strip()

        btn_row = QHBoxLayout()
        self._btn_connect = QPushButton("Connect…")
        self._btn_connect.clicked.connect(self._open_connect_dialog)
        self._btn_disconnect = QPushButton("Disconnect")
        self._btn_disconnect.clicked.connect(self._disconnect)
        self._btn_disconnect.setEnabled(False)
        self._btn_help = QPushButton("Help")
        self._btn_help.clicked.connect(self._run_help)
        self._btn_clear = QPushButton("Clear log")
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
        _safe_set_point_size(title_cmd_font, 11, context="cmd panel title")
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
        header_layout.addLayout(btn_row)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(header_bar)
        layout.addWidget(splitter, stretch=1)
        layout.addWidget(cmd_input_area)
        self.setCentralWidget(central)

        self._setup_menu_bar()
        self._init_fw_folder_switcher_dock()
        self._init_local_server_dock()

        self._set_command_list_disconnected()
        self._prompt_model_name = "Device"

    def _setup_menu_bar(self) -> None:
        menubar = self.menuBar()
        menu_view = menubar.addMenu("&View")
        self._menu_view: object = menu_view
        act_session_log = QAction("&Session log", self)
        act_session_log.setShortcut(QKeySequence("Ctrl+Shift+L"))
        act_session_log.setStatusTip("Show the live connection and command output tab")
        act_session_log.triggered.connect(self._focus_session_log_tab)
        menu_view.addAction(act_session_log)

        menu_tools = menubar.addMenu("&Tools")
        self._menu_tools = menu_tools
        self._action_fw_wizard = QAction("FW &Wizard…", self)
        self._action_fw_wizard.setStatusTip(
            "Open the FW Wizard (Artifactory, local server, update URL). "
            "The fw_wizard command runs the same steps as text prompts in the session log."
        )
        self._action_fw_wizard.triggered.connect(self._menu_fw_wizard)
        self._action_fw_wizard.setEnabled(False)
        menu_tools.addAction(self._action_fw_wizard)

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

    def _init_fw_folder_switcher_dock(self) -> None:
        from interface.fw_quick_switch_panel import FwQuickSwitchPanel

        self._fw_switch_dock = QDockWidget("Firmware folders", self)
        self._fw_switch_dock.setObjectName("FwFolderSwitcherDock")
        self._fw_switch_panel = FwQuickSwitchPanel(self)
        self._fw_switch_panel.set_shell_async(self._fw_shell_async)
        self._fw_switch_dock.setWidget(self._fw_switch_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._fw_switch_dock)
        self._fw_switch_dock.hide()

        act = QAction("FW &folder switcher", self)
        act.setCheckable(True)
        act.setStatusTip(
            "Show folders on the local firmware server and switch the camera update_url in one click."
        )
        act.toggled.connect(self._fw_switch_dock.setVisible)
        self._fw_switch_dock.visibilityChanged.connect(act.setChecked)
        mv = getattr(self, "_menu_view", None)
        if mv is not None:
            mv.addSeparator()
            mv.addAction(act)

    def _init_local_server_dock(self) -> None:
        from interface.local_server_tool import LocalServerTool

        self._local_server_dock = QDockWidget("Local Server", self)
        self._local_server_dock.setObjectName("LocalServerDock")
        self._local_server_panel = LocalServerTool(self)
        self._local_server_panel.set_shell_async(self._fw_shell_async)
        self._local_server_dock.setWidget(self._local_server_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._local_server_dock)
        self._local_server_dock.hide()
        self._local_server_dock.visibilityChanged.connect(self._on_local_server_dock_visibility)

        self._action_local_server = QAction("Local &Server", self)
        self._action_local_server.setCheckable(True)
        self._action_local_server.setStatusTip(
            "Firmware HTTP server status, folders, and switching update_url on the camera."
        )
        self._action_local_server.toggled.connect(self._local_server_dock.setVisible)
        mt = getattr(self, "_menu_tools", None)
        if mt is not None:
            mt.addAction(self._action_local_server)

    @Slot(bool)
    def _on_local_server_dock_visibility(self, visible: bool) -> None:
        act = getattr(self, "_action_local_server", None)
        if act is not None and act.isCheckable():
            act.blockSignals(True)
            act.setChecked(visible)
            act.blockSignals(False)
        if visible:
            panel = getattr(self, "_local_server_panel", None)
            if panel is not None:
                panel.refresh_if_visible()

    @Slot()
    def _on_open_local_server_tool(self) -> None:
        dock = getattr(self, "_local_server_dock", None)
        panel = getattr(self, "_local_server_panel", None)
        act = getattr(self, "_action_local_server", None)
        if dock is None:
            return
        if act is not None:
            act.blockSignals(True)
            act.setChecked(True)
            act.blockSignals(False)
        dock.show()
        dock.setVisible(True)
        dock.raise_()
        if panel is not None:
            panel.refresh_if_visible()
        self.raise_()
        self.activateWindow()

    def _menu_fw_wizard(self) -> None:
        if not self._device_connected:
            QMessageBox.information(
                self,
                "FW Wizard",
                "Connect to a camera first (use Connect… on the toolbar), then choose Tools → FW Wizard.",
            )
            return
        QMessageBox.information(
            self,
            "FW Wizard — company VPN",
            "Firmware is downloaded from the company Artifactory server.\n\n"
            "Before continuing, make sure you are connected to the company VPN "
            "(GlobalProtect).\n\n"
            "Click OK to open the FW Wizard.",
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
        from interface.fw_wizard import FwWizard

        wiz = FwWizard(self, model_dict, self._fw_shell_async)
        wiz.server_started.connect(self._on_fw_wizard_server_started)
        wiz.update_sent.connect(self._on_fw_wizard_update_sent)
        wiz.wizard_closed.connect(self._on_fw_wizard_closed)
        wiz.open_local_server_tool.connect(self._on_open_local_server_tool)
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
        self._on_append_log(f"FW Wizard: camera update URL (local server): {url}\n")

    def _on_fw_wizard_update_sent(self, ok: bool) -> None:
        if ok:
            self._on_append_log("FW Wizard: update_url command succeeded.\n")
        else:
            self._on_append_log("FW Wizard: update_url command failed (see wizard).\n")

    def _on_fw_wizard_closed(self) -> None:
        self._fw_wizard = None

    def _menu_about(self) -> None:
        QMessageBox.about(
            self,
            "About ArloShell",
            "<h3>ArloShell</h3>"
            "<p>Connect to cameras over UART, ADB (USB), or SSH. "
            "Commands are loaded after the device is detected.</p>"
            "<p><b>Tools → FW Wizard</b> opens the firmware wizard "
            "(Artifactory, local server, camera <code>update_url</code>). "
            "Typing <code>fw_wizard</code> in the command line runs the same flow with text prompts.</p>",
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
        _safe_set_point_size(tf, 22, context="welcome title")
        tf.setBold(True)
        title_w.setFont(tf)
        title_w.setStyleSheet(f"color: {ARLO_ACCENT_COLOR}; border: none; background: transparent;")
        block_lay.addWidget(title_w, 0, Qt.AlignmentFlag.AlignHCenter)

        adb_l = QLabel("ADB · SSH · UART")
        adb_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
        adbf = QFont()
        _safe_set_point_size(adbf, 9, context="welcome adb subtitle")
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
        _safe_set_point_size(vf, 9, context="welcome version")
        ver_l.setFont(vf)
        ver_l.setStyleSheet("color: #aeb8c4; border: none; background: transparent;")
        block_lay.addWidget(ver_l, 0, Qt.AlignmentFlag.AlignHCenter)

        connect_l = QLabel("Click Connect to get started")
        connect_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cf = QFont()
        _safe_set_point_size(cf, 10, context="welcome connect hint")
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

        detail_widgets = (
            self._status_model,
            self._status_sep1,
            self._status_fw,
            self._status_sep2,
            self._status_env_pill,
        )

        if self._status_phase == "connecting":
            self._set_status_dot_color(_STATUS_DOT_CONNECTING)
            self._onboarded_badge.setVisible(False)
            for w in detail_widgets:
                w.setVisible(False)
            self._status_row2.setVisible(False)
            self._status_text.setText("Connecting…")
            self._status_text.setStyleSheet(
                f"color: {_STATUS_DOT_CONNECTING}; font-size: 12px; font-weight: 600; "
                "padding: 2px 0; border: none; background: transparent;"
            )
        elif self._device_connected:
            self._set_status_dot_color(_STATUS_DOT_CONNECTED)
            for w in detail_widgets:
                w.setVisible(True)
            self._status_row2.setVisible(True)
            self._status_text.setText("Connected")
            self._status_text.setStyleSheet(
                f"color: {bright}; font-size: 12px; font-weight: 600; padding: 2px 0; "
                "border: none; background: transparent;"
            )
            if getattr(self, "_device_is_onboarded", None) is True:
                self._onboarded_badge.setVisible(True)
            else:
                self._onboarded_badge.setVisible(False)

            m = self._status_detail_model or "—"
            self._status_model.setText(m)
            self._status_model.setToolTip(m if m != "—" else "")

            fw_full = self._status_detail_fw or "—"
            fm_fw = QFontMetrics(self._status_fw.font())
            fw_disp = _elide_status_value(fw_full, 260, fm_fw)
            self._status_fw.set_copy_value(fw_full, fw_disp)

            env_internal = (self._status_detail_env or "—").strip() or "—"
            env_display = _env_stage_display_label(env_internal)
            self._status_env_pill.setText(env_display)
            self._status_env_pill.setStyleSheet(_env_stage_badge_qss(env_internal))
            url_raw = (getattr(self, "_status_update_url_raw", "") or "").strip()
            if url_raw:
                self._status_env_pill.setToolTip(f"Stage / env: {env_display}\nUpdate URL: {url_raw}")
            else:
                self._status_env_pill.setToolTip(f"Stage / env: {env_display}")

            ser = (getattr(self, "_status_detail_serial", "") or "").strip() or "—"
            fm_ser = QFontMetrics(self._status_serial_val.font())
            ser_disp = _elide_status_value(ser, 220, fm_ser)
            self._status_serial_val.set_copy_value(ser, ser_disp)
        else:
            self._set_status_dot_color(_STATUS_DOT_DISCONNECTED)
            self._onboarded_badge.setVisible(False)
            for w in detail_widgets:
                w.setVisible(False)
            self._status_row2.setVisible(False)
            self._status_text.setText("Not connected")
            self._status_text.setStyleSheet(
                f"color: {_STATUS_DOT_DISCONNECTED}; font-size: 12px; font-weight: 600; "
                "padding: 2px 0; border: none; background: transparent;"
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
        _safe_set_point_size(head_font, 12, context="disconnected cmd panel head")
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

    @Slot(dict)
    def _on_state_changed(self, info: dict) -> None:
        if info.get("connected"):
            self._device_connected = True
            self._command_profile = str(info.get("command_profile") or "none")
            self._conn_type = str(info.get("conn_type") or "")
            self._action_fw_wizard.setEnabled(self._command_profile == "e3_wired")
            self._btn_disconnect.setEnabled(True)
            self._cmd_input.setEnabled(True)
            model = info.get("model") or "—"
            env = info.get("env") or "—"
            self._status_detail_model = str(model)
            self._status_detail_fw = str(info.get("fw") or "—")
            self._status_detail_env = str(env)
            self._status_detail_serial = str(info.get("serial") or "").strip()
            self._status_update_url_raw = str(info.get("update_url_raw") or "").strip()
            raw_ob = info.get("is_onboarded")
            self._device_is_onboarded = raw_ob if isinstance(raw_ob, bool) else None
            self._status_phase = "connected"
            self._prompt_model_name = str(model).strip() or "Device"
            if str(info.get("conn_type") or "").strip().upper() == "ADB":
                did = str(info.get("device_id") or "").strip()
                if did:
                    self._preferred_adb_serial = did
            self._set_active_session_tab_title(self._prompt_model_name)
            self._sync_status_strip()
            fwp = getattr(self, "_fw_switch_panel", None)
            if fwp is not None:
                fwp.apply_state(info)
            lsp = getattr(self, "_local_server_panel", None)
            if lsp is not None:
                lsp.apply_state(info)
            wiz = getattr(self, "_fw_wizard", None)
            if wiz is not None:
                wiz.apply_shell_connection(True)
            self._update_tab_close_buttons()
        else:
            self._device_connected = False
            self._command_profile = "none"
            self._conn_type = ""
            self._action_fw_wizard.setEnabled(False)
            self._btn_disconnect.setEnabled(False)
            self._cmd_input.setEnabled(False)
            self._status_phase = "disconnected"
            self._prompt_model_name = "Device"
            self._device_is_onboarded = None
            self._status_detail_fw = "—"
            self._status_detail_serial = ""
            self._status_update_url_raw = ""
            self._active_session_log = None
            self._sync_status_strip()
            fwp = getattr(self, "_fw_switch_panel", None)
            if fwp is not None:
                fwp.apply_state({"connected": False})
            lsp = getattr(self, "_local_server_panel", None)
            if lsp is not None:
                lsp.apply_state({"connected": False})
            wiz = getattr(self, "_fw_wizard", None)
            if wiz is not None:
                wiz.apply_shell_connection(False)
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

        mcu_uart_inner = QWidget()
        mcu_uart_layout = QVBoxLayout(mcu_uart_inner)
        mcu_uart_layout.setContentsMargins(0, 0, 0, 0)
        mcu_hint = QLabel(
            "Optional: second USB–UART for the Gen5 MCU console (adc, pir, regr, …). "
            "Must be a different COM port than the ISP UART above."
        )
        mcu_hint.setWordWrap(True)
        mcu_hint.setStyleSheet("color: #aeb8c4; font-size: 11px;")
        mcu_uart_layout.addWidget(mcu_hint)
        mcu_form = QFormLayout()
        mcu_port = QComboBox()
        mcu_refresh = QPushButton("Refresh")
        mcu_baud = QSpinBox()
        mcu_baud.setRange(1200, 921600)
        mcu_baud.setValue(115200)
        mcu_row = QHBoxLayout()
        mcu_row.addWidget(mcu_port)
        mcu_row.addWidget(mcu_refresh)
        mcu_w_row = QWidget()
        mcu_w_row.setLayout(mcu_row)
        mcu_form.addRow("MCU port:", mcu_w_row)
        mcu_form.addRow("MCU baud:", mcu_baud)
        mcu_uart_layout.addLayout(mcu_form)

        def refresh_mcu_ports() -> None:
            saved = mcu_port.currentData()
            mcu_port.clear()
            mcu_port.addItem("(none)", "")
            for p, desc in list_uart_ports():
                mcu_port.addItem(f"{desc} ({p})", p)
            if saved:
                for i in range(mcu_port.count()):
                    if mcu_port.itemData(i) == saved:
                        mcu_port.setCurrentIndex(i)
                        break

        mcu_refresh.clicked.connect(refresh_mcu_ports)
        uart_form.addRow(mcu_uart_inner)

        def update_visible(_idx: int) -> None:
            key = method.currentData()
            m = device_combo.currentData()
            is_g5 = isinstance(m, dict) and (
                str(m.get("command_profile") or "") == "gen5"
                or str(m.get("platform") or "").lower() == "gen5"
            )
            uart_box.setVisible(key == "UART")
            mcu_uart_inner.setVisible(key == "UART" and is_g5)
            adb_box.setVisible(key == "ADB")
            ssh_box.setVisible(key == "SSH")

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
            ap = get_adb_password_for_model(m.get("name"))
            if ap and not adb_pwd.text().strip():
                adb_pwd.setText(ap)

        def _sync_uart_baud_from_model() -> None:
            m = device_combo.currentData()
            if isinstance(m, dict) and m.get("default_uart_baud"):
                uart_baud.setValue(int(m["default_uart_baud"]))

        def _on_device_changed(_i: int) -> None:
            _refill_methods()
            _sync_uart_baud_from_model()
            refresh_mcu_ports()
            update_visible(method.currentIndex())

        device_combo.currentIndexChanged.connect(_on_device_changed)
        _on_device_changed(device_combo.currentIndex())
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
        m_sel = device_combo.currentData()
        if not isinstance(m_sel, dict):
            m_sel = {}
        if key == "UART":
            if uart_port.count() == 0:
                QMessageBox.warning(self, "UART", "No serial ports found.")
                return
            self._begin_connection_log_tab()
            port = uart_port.currentData()
            baud = uart_baud.value()
            mcu_raw = mcu_port.currentData()
            mcu_p = str(mcu_raw).strip() if mcu_raw else ""
            mcu_br = int(mcu_baud.value()) if mcu_p else 0
            if mcu_p and _uart_ports_equivalent(mcu_p, str(port)):
                QMessageBox.warning(
                    self,
                    "UART",
                    "MCU UART cannot use the same COM port as the ISP/main UART.",
                )
                return
            self._worker.connect_uart.emit(port, baud, m_sel, mcu_p, mcu_br)
        elif key == "ADB":
            try:
                ensure_adb_allowed_for_selection(m_sel)
            except UnsupportedConnectionError as e:
                QMessageBox.critical(self, "ADB not supported", str(e))
                return
            serials = ADBHandler.list_attached_usb_serials()
            if not serials:
                QMessageBox.warning(
                    self,
                    "ADB",
                    "No ADB device found. Connect the camera via USB and ensure USB debugging is enabled.",
                )
                return
            adb_serial = ""
            if len(serials) > 1:
                pref = (self._preferred_adb_serial or "").strip()
                if pref and pref in serials:
                    adb_serial = pref
                else:
                    pick = _AdbDevicePickerDialog(self, serials)
                    if pick.exec() != QDialog.DialogCode.Accepted:
                        return
                    adb_serial = (pick.selected_serial() or "").strip()
                    if not adb_serial:
                        return
            self._begin_connection_log_tab()
            self._worker.connect_adb.emit(adb_pwd.text(), m_sel, adb_serial)
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
                m_sel,
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
