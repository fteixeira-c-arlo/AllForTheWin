"""PySide6 main window: connect flow, command execution on a worker thread, prompt bridge."""
from __future__ import annotations

import codecs
import os
import re
import sys
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, Q_ARG, QMetaObject, QThread, QTimer, QRect, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QIntValidator,
    QKeySequence,
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
    QGraphicsOpacityEffect,
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
    QStackedWidget,
    QTabBar,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from rich.console import Console

from core.app_metadata import APP_NAME, APP_VERSION
from core.build_info import UPDATE_URL_SHELL, parse_env_from_update_url
from core.device_connection import detect_after_connect, ensure_adb_allowed_for_selection
from core.device_credentials import get_adb_password_for_model, resolve_production_adb_password
from core.device_errors import UnknownDeviceError, UnsupportedConnectionError
from core.device_registry import lookup_registry_by_model_id
from core.user_paths import get_arlo_logs_dir
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
    connection_methods_upper,
    default_uart_baud_for_model_group,
    format_connect_dialog_device_label,
    format_supported_connections,
    get_command_profile_for_model_name,
    get_model_by_name,
    get_models,
    model_supports_adb,
)
from transports.adb_handler import ADBHandler
from transports.ssh_handler import SSHHandler
from transports.uart_handler import UARTHandler, list_uart_ports
from transports.connection_config import ConnectionConfig
from interface.gui_bridge import GuiBridge, _SELECT_CANCELLED
from interface.log_viewer_widget import LogViewerWidget
from interface.app_styles import (
    polish_dynamic_properties,
    prepare_qframe_for_qss,
    qcombobox_dark_stylesheet,
)
from styles.tokens import (
    CMD_ROW_HEIGHT_PX,
    PANEL_FIXED_WIDTH,
    STATUS_DOT_CONNECTED,
    STATUS_DOT_CONNECTING,
    STATUS_DOT_DISCONNECTED,
)


DEFAULT_UART_BAUD = 115200
DEFAULT_SSH_PORT = 22

# Match QSpinBox int range for UART baud validators (practical upper bound).
_UART_BAUD_INT_MAX = 2_147_483_647


def _parse_connect_baud_text(s: str) -> int | None:
    """Positive integer baud from user text, or None if empty/invalid."""
    t = (s or "").strip().replace("_", "").replace(",", "")
    if not t:
        return None
    try:
        v = int(t, 10)
    except ValueError:
        return None
    return v if v >= 1 else None


def _configure_connect_dialog_baud_lineedit(le: QLineEdit) -> None:
    """Fully typeable baud field; min 1, no artificial max below int32."""
    le.setValidator(QIntValidator(1, _UART_BAUD_INT_MAX, le))


def _style_connect_dialog_comboboxes(*boxes: QComboBox) -> None:
    """Dark combo + visible chevron (Windows); add new connect-dialog QComboBoxes here."""
    qss = qcombobox_dark_stylesheet(include_dropdown_chevron=True)
    for cb in boxes:
        cb.setStyleSheet(qss)
        cb.setCursor(Qt.CursorShape.PointingHandCursor)


# Command input bottom border and shared UI accents (refined teal for dark UI).
ARLO_ACCENT_COLOR = "#00897B"

# Compact main header bar (~44px): action buttons (hex only, no CSS variables).
_HEADER_QSS_CONNECT = """
QPushButton {
    background-color: #00897B;
    color: #ffffff;
    border: 1px solid #00897B;
    border-radius: 6px;
    padding: 5px 12px;
    font-size: 11px;
    font-weight: 600;
}
QPushButton:hover { background-color: #009688; border-color: #009688; }
QPushButton:pressed { background-color: #00796b; border-color: #00796b; }
QPushButton:disabled { background-color: #2a313a; color: #5c6570; border-color: #3d4654; }
"""
_HEADER_QSS_DISCONNECT = """
QPushButton {
    background-color: transparent;
    color: #e05555;
    border: 1px solid #8b3a3a;
    border-radius: 6px;
    padding: 5px 12px;
    font-size: 11px;
    font-weight: 600;
}
QPushButton:hover { background-color: #2a1f1f; }
QPushButton:pressed { background-color: #1a1515; }
QPushButton:disabled { color: #6b7280; border-color: #3d4654; }
"""
_HEADER_QSS_OUTLINE = """
QPushButton {
    background-color: transparent;
    color: #8b95a5;
    border: 1px solid #3d4654;
    border-radius: 6px;
    padding: 5px 12px;
    font-size: 11px;
}
QPushButton:hover { background-color: #222326; color: #c5ced9; border-color: #5a6270; }
QPushButton:pressed { background-color: #1a1c20; }
"""

_CLAIMED_BADGE_ONBOARDED_QSS = (
    "QLabel { background-color: #1e3a5f; color: #64b5f6; border-radius: 10px; "
    "padding: 2px 8px; font-size: 10px; font-weight: 600; }"
)
_CLAIMED_BADGE_NOT_CLAIMED_QSS = (
    "QLabel { background-color: #4a3d16; color: #e6c229; border-radius: 10px; "
    "padding: 2px 8px; font-size: 10px; font-weight: 600; }"
)


def _header_vertical_divider(parent: QWidget | None = None) -> QFrame:
    f = QFrame(parent)
    f.setFixedSize(1, 20)
    prepare_qframe_for_qss(f)
    f.setStyleSheet("background-color: #333333; border: none;")
    return f


def _firmware_listen_port_for_header() -> int | None:
    """Port for firmware HTTP server if this or another ArloHub session is listening."""
    from urllib.parse import urlparse

    from core.local_server import (
        get_running_server_url,
        is_firmware_port_accepting_connections,
        read_fw_server_state,
    )

    ok, url = get_running_server_url()
    if ok and url:
        pr = urlparse(url).port
        if pr:
            return int(pr)
    st = read_fw_server_state()
    if st and is_firmware_port_accepting_connections(int(st["port"])):
        return int(st["port"])
    return None


def _safe_set_point_size(font: QFont, size: int, *, context: str = "") -> None:
    _ = context
    if size > 0:
        font.setPointSize(size)
        return
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
        ps = af.pointSize()
        if ps > 0:
            font.setPointSize(ps)
            return
        px = af.pixelSize()
        if px > 0:
            font.setPixelSize(px)
            return
    font.setPointSize(10)


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


# Welcome launcher (disconnected state)
_WELCOME_PAGE_BG = "#1a1a1a"
_WELCOME_CARD_BG = "#222326"
_WELCOME_CARD_BORDER = "#333333"
_WELCOME_CARD_BORDER_HOVER = "#5a5a5a"
_WELCOME_SECTION_LABEL_COLOR = "#5F5E5A"
_WELCOME_CARD_SUBTITLE_COLOR = "#5F5E5A"
_WELCOME_DIM_OPACITY = 0.4
_WELCOME_BLUE = "#4a90d9"
_WELCOME_AMBER = "#c9a227"


def _supported_connection_methods_union() -> set[str]:
    """ADB / SSH / UART keys supported by at least one model in the catalog."""
    out: set[str] = set()
    for m in get_models():
        out.update(connection_methods_upper(m))
    return out


class WelcomeConnectionCard(QWidget):
    """Rounded welcome card; interactive cards get hover border and emit ``clicked``."""

    clicked = Signal()

    def __init__(
        self,
        icon_char: str,
        icon_color: str,
        title: str,
        subtitle: str,
        *,
        interactive: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._interactive = interactive
        self.setObjectName("welcomeConnectionCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._qss_normal = (
            f"#welcomeConnectionCard {{ background-color: {_WELCOME_CARD_BG}; "
            f"border: 1px solid {_WELCOME_CARD_BORDER}; border-radius: 10px; }}"
        )
        self._qss_hover = (
            f"#welcomeConnectionCard {{ background-color: #2a2d32; "
            f"border: 1px solid {_WELCOME_CARD_BORDER_HOVER}; border-radius: 10px; }}"
        )
        self.setStyleSheet(self._qss_normal)
        if interactive:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            dim = QGraphicsOpacityEffect(self)
            dim.setOpacity(_WELCOME_DIM_OPACITY)
            self.setGraphicsEffect(dim)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(10)
        ic = QLabel(icon_char)
        ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ic.setStyleSheet(
            f"color: {icon_color}; font-size: 26px; border: none; background: transparent;"
        )
        lay.addWidget(ic)
        tl = QLabel(title)
        tf = QFont()
        _safe_set_point_size(tf, 13, context="welcome card title")
        tf.setBold(True)
        tl.setFont(tf)
        tl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tl.setStyleSheet("color: #e8eef4; border: none; background: transparent;")
        lay.addWidget(tl)
        sl = QLabel(subtitle)
        sf = QFont()
        _safe_set_point_size(sf, 10, context="welcome card subtitle")
        sl.setFont(sf)
        sl.setWordWrap(True)
        sl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        sl.setStyleSheet(
            f"color: {_WELCOME_CARD_SUBTITLE_COLOR}; border: none; background: transparent;"
        )
        lay.addWidget(sl)
        self.setMinimumHeight(148)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)

    def enterEvent(self, event):  # type: ignore[override]
        if self._interactive:
            self.setStyleSheet(self._qss_hover)
        super().enterEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        if self._interactive:
            self.setStyleSheet(self._qss_normal)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):  # type: ignore[override]
        if self._interactive and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def _welcome_section_label(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
    f = QFont()
    _safe_set_point_size(f, 11, context="welcome section label")
    f.setBold(True)
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
    lb.setFont(f)
    lb.setStyleSheet(
        f"color: {_WELCOME_SECTION_LABEL_COLOR}; border: none; background: transparent;"
    )
    return lb


def _content_tabs_stylesheet(*, welcome_solo_pane: bool) -> str:
    """Tab widget chrome: full border when the bar is visible; flat pane for welcome-only layout."""
    if welcome_solo_pane:
        pane = (
            f"QTabWidget#contentTabs::pane {{ border: none; margin: 0; padding: 0; "
            f"background-color: {_WELCOME_PAGE_BG}; }}"
        )
    else:
        pane = (
            "QTabWidget#contentTabs::pane { border: 1px solid #3d4654; top: -1px; "
            "background-color: #111111; }"
        )
    return f"""
            {pane}
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


def _load_png_pixmap(path: str) -> QPixmap:
    """
    Load a PNG into a pixmap with explicit ARGB32 alpha.

    ``QPixmap(path)`` alone can composite incorrectly on some platforms (opaque black
    behind transparency). Window icons and welcome branding use this path.
    """
    img = QImage(path)
    if img.isNull():
        return QPixmap()
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    return QPixmap.fromImage(img)


def _load_icon(path: str) -> QIcon:
    """Load window icon preserving alpha (ARGB32). Intended for PNG assets."""
    return QIcon(_load_png_pixmap(path))


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
    "kv",
    "filesystem",
    "arlogw",
    "profile",
    "ble",
    "feedback",
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
    "isp",
)


def _display_command_label(name: str) -> str:
    return (name or "").replace("_", " ").strip()


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


def _sidebar_title_to_section(title_upper: str) -> str:
    t = (title_upper or "").strip().upper()
    if t in ("FIRMWARE", "MANUFACTURING"):
        return "firmware"
    if t == "DEVICE":
        return "device"
    if t == "LOGS":
        return "logs"
    if t == "NETWORK":
        return "network"
    if t == "DEV":
        return "device"
    return "neutral"


def _adv_catalog_to_section(cat_key: str) -> str:
    k = (cat_key or "").strip().lower()
    if k in ("firmware", "ota"):
        return "firmware"
    if k == "logs":
        return "logs"
    if k == "network":
        return "network"
    if k in (
        "device",
        "kv",
        "filesystem",
        "arlogw",
        "profile",
        "ble",
        "feedback",
        "camera",
        "ptz",
        "mfg",
        "arlocmd",
        "cali",
        "kvcmd",
        "sv",
        "debug",
        "arlod",
        "isp",
    ):
        return "device"
    return "neutral"


def _section_header_glyph(section: str, title_upper: str) -> str:
    tu = (title_upper or "").strip().upper()
    if section == "firmware" and tu == "MANUFACTURING":
        return "M"
    if section == "firmware":
        return "F"
    if section == "device":
        return "D"
    if section == "logs":
        return "L"
    if section == "network":
        return "N"
    return tu[:1] if tu else "?"


def _row_section_glyph(section: str) -> str:
    if section == "firmware":
        return "F"
    if section == "device":
        return "D"
    if section == "logs":
        return "L"
    if section == "network":
        return "N"
    return "·"


def _cmd_risk_badge_id(cmd_key: str) -> str | None:
    k = (cmd_key or "").strip().lower()
    if k in ("flash", "factory reset"):
        return "badgeDanger"
    return None


def _row_section_from_group(
    group: _CollapsibleCategoryBlock | _AdvancedTierBlock | None,
) -> str:
    if group is None:
        return "neutral"
    if isinstance(group, _AdvancedTierBlock):
        return "neutral"
    return group.section_kind()


class _AdbPickerDeviceCard(QWidget):
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
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
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
                f"#adbDeviceCard {{ background-color: rgba(0, 137, 123, 0.22); "
                f"border: 2px solid {accent}; border-radius: 8px; }}"
            )
        elif self._hover:
            self.setStyleSheet(
                "#adbDeviceCard { background-color: #2a3038; border: 1px solid #5a6570; border-radius: 8px; }"
            )
        else:
            self.setStyleSheet(
                "#adbDeviceCard { background-color: #181c22; border: 1px solid #3d4650; border-radius: 8px; }"
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

    def __init__(
        self,
        title: str,
        *,
        expanded_default: bool = True,
        section: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._expanded = expanded_default
        self._title = title.upper()
        self._section = section if section is not None else _sidebar_title_to_section(self._title)

        header_block = QWidget(self)
        header_block.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header_block.setProperty("section", self._section)
        hrow = QHBoxLayout(header_block)
        hrow.setContentsMargins(0, 0, 0, 0)
        hrow.setSpacing(0)

        icon = QLabel(_section_header_glyph(self._section, self._title))
        icon.setObjectName("sectionHeaderIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._header = QPushButton(self)
        self._header.setObjectName("sectionHeaderBtn")
        self._header.setFlat(True)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.clicked.connect(self._toggle)

        hrow.addWidget(icon, 0)
        hrow.addWidget(self._header, 1)
        polish_dynamic_properties(header_block)

        divider = QFrame(self)
        divider.setObjectName("sectionHeaderDivider")
        divider.setFrameShape(QFrame.Shape.NoFrame)
        divider.setFixedHeight(1)
        prepare_qframe_for_qss(divider)

        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(header_block)
        outer.addWidget(divider)
        outer.addWidget(self._body)
        self._sync_header()
        self._body.setVisible(self._expanded)
        self._row_widgets: list[QWidget] = []

    def section_kind(self) -> str:
        return self._section

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
        self.setObjectName("advancedBlock")
        self._expanded = False

        header_block = QWidget(self)
        header_block.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header_block.setProperty("section", "neutral")
        hrow = QHBoxLayout(header_block)
        hrow.setContentsMargins(0, 0, 0, 0)
        hrow.setSpacing(0)

        icon = QLabel("A")
        icon.setObjectName("sectionHeaderIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._header = QPushButton(self)
        self._header.setObjectName("advancedHeaderBtn")
        self._header.setFlat(True)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.clicked.connect(self._toggle)

        hrow.addWidget(icon, 0)
        hrow.addWidget(self._header, 1)
        polish_dynamic_properties(header_block)

        divider = QFrame(self)
        divider.setObjectName("sectionHeaderDivider")
        divider.setFrameShape(QFrame.Shape.NoFrame)
        divider.setFixedHeight(1)
        prepare_qframe_for_qss(divider)

        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(header_block)
        outer.addWidget(divider)
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
        section: str,
        risk_badge: str | None,
        on_run: Any,
        on_prefill: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("cmdRow")
        prepare_qframe_for_qss(self)
        self.setProperty("tier", str(int(tier)))
        self.setProperty("section", section)
        self._cmd_key = cmd_key
        self._on_run = on_run
        self._on_prefill = on_prefill
        self._pending_timer: QTimer | None = None
        self._suppress_next_release = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(CMD_ROW_HEIGHT_PX)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(tooltip)
        self.setToolTipDuration(60000)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 0, 8, 0)
        row.setSpacing(6)

        glyph = QLabel(_row_section_glyph(section))
        glyph.setObjectName("cmdRowSectionIcon")
        glyph.setFixedSize(18, 18)
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        glyph.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row.addWidget(glyph, 0)

        self._name_lbl = QLabel(display_line)
        self._name_lbl.setObjectName("cmdRowLabel")
        row.addWidget(self._name_lbl, stretch=1)

        if args_hint:
            h = QLabel(args_hint)
            h.setObjectName("cmdRowArgHint")
            row.addWidget(h, stretch=0)

        if risk_badge:
            badge = QLabel("risk")
            badge.setObjectName(risk_badge)
            badge.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            row.addWidget(badge, 0)

        polish_dynamic_properties(self)

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
_TRANSPORT_WATCHDOG_INTERVAL_MS = 2500
_TRANSPORT_WATCHDOG_FAIL_STREAK = 2


def _fw_shell_timeout_sec(command: str) -> float | None:
    """ADB/SSH subprocess timeout; UART uses this to extend total wait for sparse output."""
    c = (command or "").strip().lower()
    if "update_refresh" in c:
        return 300.0
    if "update_url" in c:
        return 120.0
    if c == "arlocmd reboot" or c.startswith("arlocmd reboot "):
        return 90.0
    return None


def _posix_single_quoted(s: str) -> str:
    """Quote for POSIX sh single-quoted string (device shell, not Windows cmd)."""
    return "'" + (s or "").replace("'", "'\"'\"'") + "'"


def _extract_camera_update_url_line(raw: str) -> str:
    """Best-effort URL from arlocmd update_url output when logs precede the URL."""
    text = (raw or "").strip()
    if not text:
        return ""
    for ln in text.splitlines():
        s = ln.strip()
        if "http://" not in s and "https://" not in s:
            continue
        for part in s.split():
            low = part.lower()
            if low.startswith("http://") or low.startswith("https://"):
                return part[:500]
        if s.lower().startswith("http"):
            return s[:500]
    return text[:500]


def _normalize_fw_shell_command(command: str, args: list[str]) -> tuple[str, list[str]]:
    """
    Build a shell-safe command line for the camera. Unquoted URLs break sh when they contain
    &, ;, (), etc. ADB/SSH/UART all send one line interpreted by the device shell.
    """
    c = (command or "").strip()
    a = [str(x) for x in (args or [])]
    if c == "arlocmd update_url" and len(a) == 1:
        return f"arlocmd update_url {_posix_single_quoted(a[0])}", []
    return command, a


def _uart_ports_equivalent(port_a: str, port_b: str) -> bool:
    """True if two serial port names are the same device (e.g. COM3 vs \\\\.\\COM3)."""
    from transports.uart_handler import _port_key_for_match

    return _port_key_for_match(port_a) == _port_key_for_match(port_b)


class SessionWorker(QObject):
    """Owns connection handle; all I/O runs on this object's thread."""

    append_log = Signal(str)
    state_changed = Signal(dict)
    commands_updated = Signal(list, str)
    command_finished = Signal(str, object)
    connect_failed = Signal(str)

    connect_uart = Signal(str, int, object, str, int)
    connect_adb = Signal(str, object, str)
    connect_adb_production = Signal(object, str)
    connect_ssh = Signal(str, int, str, str, object)
    submit_command = Signal(str)
    disconnect_session = Signal()
    fw_shell_request = Signal(str, object)
    fw_shell_response = Signal(bool, str)
    refresh_update_url_readback = Signal()
    forced_disconnect = Signal(str)

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
        self._detect_busy = False
        self._command_queue: list[str] = []
        self._detect_timer = QTimer(self)
        self._detect_timer.setSingleShot(True)
        self._detect_timer.timeout.connect(self._try_run_detect_and_load)
        self._url_readback_timer = QTimer(self)
        self._url_readback_timer.setSingleShot(True)
        self._url_readback_timer.timeout.connect(self._try_update_url_readback)
        self._url_readback_attempt = 0

        self.connect_uart.connect(self._on_connect_uart)  # type: ignore[arg-type]
        self.connect_adb.connect(self._on_connect_adb)  # type: ignore[arg-type]
        self.connect_adb_production.connect(self._on_connect_adb_production)  # type: ignore[arg-type]
        self.connect_ssh.connect(self._on_connect_ssh)  # type: ignore[arg-type]
        self.submit_command.connect(self._on_command)
        self.disconnect_session.connect(self._on_disconnect)
        self.forced_disconnect.connect(self._on_forced_disconnect)
        self.fw_shell_request.connect(self._on_fw_shell_request)
        self.refresh_update_url_readback.connect(self._on_refresh_update_url_readback)

    @Slot(str)
    def _on_forced_disconnect(self, reason: str) -> None:
        if not self._cfg or not self._handle:
            return
        self._do_disconnect((reason or "").strip() or "Disconnected.")

    @Slot()
    def _on_refresh_update_url_readback(self) -> None:
        """After pushing update_url, refresh UI from device without full detect (no build_info spam)."""
        self._url_readback_attempt = 0
        self._try_update_url_readback()

    def _try_update_url_readback(self) -> None:
        if not self._handle or not self._cfg:
            return
        if self._io_busy:
            self._url_readback_attempt += 1
            if self._url_readback_attempt <= 40:
                self._url_readback_timer.start(100)
            else:
                self.append_log.emit("Update URL read-back skipped (session busy).\n")
            return
        self._url_readback_attempt = 0
        self._io_busy = True
        try:
            ok, text = self._handle.execute(UPDATE_URL_SHELL, [], timeout_sec=90.0)
            if ok and (text or "").strip():
                u = _extract_camera_update_url_line(text)
                if not u:
                    u = (text or "").strip()[:500]
                self._detected["update_url_raw"] = u[:500]
                env = parse_env_from_update_url(u)
                if env:
                    self._detected["env"] = env
                self._emit_state()
            if ok:
                self.append_log.emit("Camera update_url read back (UI updated; no full device scan).\n")
            else:
                t = (text or "").strip().replace("\r\n", "\n")
                self.append_log.emit(
                    f"Could not read update_url from camera: {t[:300]}{'…' if len(t) > 300 else ''}\n"
                )
        finally:
            self._io_busy = False

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

    def _bootstrap_pre_detect_state(self) -> None:
        """
        Enable main-window UI (command input, command list) from the Connect dialog selection
        before the blocking device scan. Detection still runs next on the worker thread, but
        the worker can interleave user commands until the scan starts; during the scan, commands
        are queued and run when the scan finishes.
        """
        sel = self._selected_model if isinstance(self._selected_model, dict) else {}
        pick = str(sel.get("name") or "").strip()
        self._detected = {
            "model": pick or None,
            "fw_version": None,
            "serial": None,
            "env": None,
            "update_url_raw": "",
            "raw_build_info": "",
            "is_onboarded": None,
        }
        model_for_cmds = pick or "Device"
        self._command_profile = get_command_profile_for_model_name(pick or None)
        self._device_commands = load_device_commands(model_for_cmds)
        self._emit_state()
        self.commands_updated.emit(list(self._device_commands), self._command_profile or "none")
        self._heartbeat_timer.start()

    @Slot()
    def _try_run_detect_and_load(self) -> None:
        """Run detection when the worker is idle so UART/ADB is not shared with a user command."""
        if not self._handle or not self._cfg:
            return
        if self._io_busy:
            self._detect_timer.start(50)
            return
        self._run_detect_and_load()

    def _flush_command_queue_after_detect(self) -> None:
        if not self._command_queue:
            return
        pending = self._command_queue[:]
        self._command_queue.clear()
        for ln in pending:
            self._on_command(ln)

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
        ok, msg, settings = handler.connect(
            port=port,
            baud_rate=baud,
            console_style=console_style,
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
            self._bootstrap_pre_detect_state()
            self._detect_timer.start(0)
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

        def _log(m: str) -> None:
            self.append_log.emit(m)

        ok, msg, settings = handler.connect(
            password=password, device_serial=serial_arg, log_callback=_log
        )
        if ok and settings:
            self._mcu_handle = None
            device_id = settings.get("device_serial") or "USB"
            cfg = _make_config("ADB", settings, device_id)
            self._cfg = cfg
            self._handle = handler
            self.append_log.emit(f"Connected via USB ({device_id})\n")
            self._bootstrap_pre_detect_state()
            self._detect_timer.start(0)
            return
        self._cfg = None
        self._handle = None
        self._mcu_handle = None
        err = (msg or "ADB connection failed.").strip()
        self.append_log.emit(f"\nConnection failed: {err}\n\n")
        self.connect_failed.emit(err)

    @Slot(object, str)
    def _on_connect_adb_production(self, selected: object = None, adb_serial: str = "") -> None:
        self._selected_model = selected if isinstance(selected, dict) else {}
        self.append_log.emit("Connecting via ADB (Production)…\n")
        pwd = resolve_production_adb_password(self._selected_model)
        if not pwd:
            err = (
                "No production ADB credentials are configured for this device. "
                "Use the Dev / QA tab, or follow internal documentation for secured credentials."
            )
            self.append_log.emit(f"\n{err}\n\n")
            self.connect_failed.emit(err)
            return
        handler = ADBHandler()
        serial_arg = (adb_serial or "").strip() or None

        def _log(m: str) -> None:
            self.append_log.emit(m)

        ok, msg, settings = handler.connect(
            password=pwd, device_serial=serial_arg, log_callback=_log
        )
        if ok and settings:
            self._mcu_handle = None
            device_id = settings.get("device_serial") or "USB"
            cfg = _make_config("ADB", settings, device_id)
            self._cfg = cfg
            self._handle = handler
            self.append_log.emit(f"Connected via USB ({device_id})\n")
            self._bootstrap_pre_detect_state()
            self._detect_timer.start(0)
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
            self._bootstrap_pre_detect_state()
            self._detect_timer.start(0)
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
        self._detect_busy = True
        try:
            ct = (self._cfg.type if self._cfg else "") or ""
            ct_l = ct.strip().lower()
            if ct_l == "adb":
                conn = "adb"
            elif ct_l == "ssh":
                conn = "ssh"
            else:
                conn = "uart"
            try:
                self._detected, _dc = detect_after_connect(
                    self._handle.execute,
                    conn,
                    selected_model=self._selected_model,
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
            sel = self._selected_model if isinstance(self._selected_model, dict) else {}
            picked = str(sel.get("name") or "").strip().upper()
            det_model = str(self._detected.get("model") or "").strip()
            if not det_model and picked:
                self._detected = {**self._detected, "model": picked}
                self.append_log.emit(
                    "Using the model you selected in Connect (UART did not return build_info this session).\n"
                )
            model_for_commands = self._detected.get("model") or "Device"
            self._command_profile = get_command_profile_for_model_name(self._detected.get("model"))
            self._device_commands = load_device_commands(model_for_commands)
            self._emit_state()
            self.commands_updated.emit(list(self._device_commands), self._command_profile or "none")
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
            for line in self._detected.get("post_connect_messages") or []:
                if line:
                    self.append_log.emit(f"{str(line).strip()}\n")
            self.append_log.emit(
                "Type a command below, or click a command in the list to run it "
                "(double-click to put the name in the input for editing).\n\n"
            )
        finally:
            self._detect_busy = False
            if self._handle and self._cfg:
                self._flush_command_queue_after_detect()
            else:
                self._command_queue.clear()

    def _do_disconnect(self, log_message: str | None) -> None:
        self._heartbeat_timer.stop()
        self._detect_timer.stop()
        self._command_queue.clear()
        self._detect_busy = False
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
        if self._io_busy or self._detect_busy or not self._cfg or not self._handle:
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
        if self._detect_busy:
            self._command_queue.append(line)
            self.append_log.emit(
                "(Device scan in progress — your command will run when it finishes.)\n"
            )
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
                pull_logs_local_dir=get_arlo_logs_dir(),
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
            elif message and (
                "Session expired" in _strip_rich_markup(str(message))
                or "Login incorrect" in _strip_rich_markup(str(message))
            ):
                self._do_disconnect("Connection lost — session ended. Use Connect to reconnect.")
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
        n_cmd, n_args = _normalize_fw_shell_command(command, args)
        line = n_cmd if not n_args else (n_cmd + " " + " ".join(n_args))
        self.append_log.emit(f"[fw_shell] {line}\n")
        self._io_busy = True
        try:
            tmo = _fw_shell_timeout_sec(command)
            ok, text = self._handle.execute(n_cmd, n_args, timeout_sec=tmo)
            if (
                not ok
                and text
                and (
                    "Device disconnected" in text
                    or "Session expired" in text
                    or "Login incorrect" in text
                )
            ) or (
                self._handle
                and hasattr(self._handle, "is_connected")
                and not self._handle.is_connected()
            ):
                self._do_disconnect("Connection lost — device disconnected. Use Connect to reconnect.")
            snippet = (text or "").strip().replace("\r\n", "\n")
            # Set update_url often prints HAL/partition noise to stdout; keep session log usable.
            if (
                ok
                and n_cmd.startswith("arlocmd update_url 'http")
                and len(snippet) > 240
            ):
                first = snippet.splitlines()[0].strip() if snippet else ""
                snippet = (
                    (first[:220] + " …")
                    if first
                    else "OK (long device log omitted; URL was sent.)"
                )
            elif len(snippet) > 1200:
                snippet = snippet[:1200] + "…"
            self.append_log.emit(f"[fw_shell] {'OK' if ok else 'FAIL'}: {snippet or '(no output)'}\n")
            self.fw_shell_response.emit(ok, text or "")
        finally:
            self._io_busy = False


class MainWindow(QMainWindow):
    """Main application window; ``transport_watchdog_poll_done`` is emitted from a background thread."""

    transport_watchdog_poll_done = Signal(bool, int)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  v{APP_VERSION}")
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
        self._production_connect_resume: dict[str, Any] | None = None
        self._fw_shell_redetect_after: bool = False
        self._transport_watchdog_timer = QTimer(self)
        self._transport_watchdog_timer.setInterval(_TRANSPORT_WATCHDOG_INTERVAL_MS)
        self._transport_watchdog_timer.timeout.connect(self._on_transport_watchdog_tick)
        self._watchdog_poll_busy = False
        self._watchdog_fail_streak = 0
        self._watchdog_uart_port = ""
        # Bumped on disconnect so late poll results from a prior session cannot fail-streak a new one.
        self._watchdog_transport_epoch = 0

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
        self.transport_watchdog_poll_done.connect(self._on_transport_watchdog_poll_done)

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
        self._cmd_device_catalog_outer: QWidget | None = None
        self._cmd_sep_t1_tools: QFrame | None = None
        self._cmd_sep_tools_adv: QFrame | None = None
        self._live_tail_sessions: dict[str, dict[str, Any]] = {}

        def _marshal_tail_open(path: str, title: str) -> None:
            # command_parser runs on SessionWorker thread; tab/UI must run on the GUI thread.
            if QThread.currentThread() is self.thread():
                self.tail_live_open_tab(path, title)
                return
            QMetaObject.invokeMethod(
                self,
                "tail_live_open_tab",
                Qt.ConnectionType.BlockingQueuedConnection,
                Q_ARG(str, path),
                Q_ARG(str, title),
            )

        def _marshal_tail_stop(path: str) -> None:
            if QThread.currentThread() is self.thread():
                self.tail_live_stop_tab(path)
                return
            QMetaObject.invokeMethod(
                self,
                "tail_live_stop_tab",
                Qt.ConnectionType.BlockingQueuedConnection,
                Q_ARG(str, path),
            )

        set_tail_live_view_handlers(_marshal_tail_open, _marshal_tail_stop)

        self._header_bar = QWidget()
        self._header_bar.setObjectName("mainHeaderBar")
        self._header_bar.setFixedHeight(44)
        self._header_bar.setStyleSheet(
            "#mainHeaderBar { background-color: #0d0d0d; border-bottom: 1px solid #1e1e1e; }"
        )
        h_header = QHBoxLayout(self._header_bar)
        h_header.setContentsMargins(10, 0, 10, 0)
        h_header.setSpacing(0)

        header_left = QWidget()
        h_left = QHBoxLayout(header_left)
        h_left.setContentsMargins(0, 0, 0, 0)
        h_left.setSpacing(10)

        self._header_icon_lbl = QLabel()
        self._header_icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._header_icon_lbl.setStyleSheet("border: none; background: transparent;")
        self._header_icon_lbl.setFixedSize(20, 20)
        _hip = _main_window_icon_path()
        if _hip:
            _hpm = _load_png_pixmap(_hip)
            if not _hpm.isNull():
                self._header_icon_lbl.setPixmap(
                    _hpm.scaled(
                        20,
                        20,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
        h_left.addWidget(self._header_icon_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        h_left.addWidget(_header_vertical_divider(self._header_bar), 0, Qt.AlignmentFlag.AlignVCenter)

        self._status_dot = QWidget(self._header_bar)
        self._status_dot.setObjectName("statusDot")
        self._status_dot.setFixedSize(8, 8)
        self._status_dot.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._status_dot.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._set_status_dot_color(STATUS_DOT_DISCONNECTED)
        h_left.addWidget(self._status_dot, 0, Qt.AlignmentFlag.AlignVCenter)

        self._status_text = QLabel("Not connected")
        self._status_text.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        )
        self._status_text.setStyleSheet(
            "color: #8b95a5; font-size: 12px; font-weight: 500; border: none; background: transparent;"
        )
        h_left.addWidget(self._status_text, 0, Qt.AlignmentFlag.AlignVCenter)
        h_header.addWidget(header_left, 0)

        h_header.addStretch(1)

        self._header_center = QWidget()
        h_center = QHBoxLayout(self._header_center)
        h_center.setContentsMargins(0, 0, 0, 0)
        h_center.setSpacing(12)

        self._status_model = QLabel("—")
        self._status_model.setStyleSheet(
            "font-size: 12px; font-weight: 500; color: #ffffff; border: none; background: transparent;"
        )

        self._status_env_badge = QLabel()
        self._status_env_badge.setVisible(False)
        self._status_env_badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._status_serial_val = _CopyableValueLabel()
        _mono_sm = QFont("Menlo", 10) if sys.platform == "darwin" else QFont("Consolas", 10)
        _safe_set_point_size(_mono_sm, 11, context="header serial")
        self._status_serial_val.setFont(_mono_sm)
        self._status_serial_val.setStyleSheet(
            "color: #8b95a5; font-size: 11px; font-family: Consolas, Menlo, monospace; "
            "border: none; background: transparent;"
        )

        self._status_fw = _CopyableValueLabel()
        self._status_fw.setStyleSheet(
            "font-size: 11px; font-weight: 500; color: #8b95a5; border: none; background: transparent;"
        )

        self._claimed_badge = QLabel()
        self._claimed_badge.setVisible(False)
        self._claimed_badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._header_server_block = QWidget()
        h_srv = QHBoxLayout(self._header_server_block)
        h_srv.setContentsMargins(0, 0, 0, 0)
        h_srv.setSpacing(8)
        self._header_server_div = _header_vertical_divider(self._header_bar)
        self._header_server_div.setVisible(False)
        h_srv.addWidget(self._header_server_div, 0, Qt.AlignmentFlag.AlignVCenter)
        self._header_server_dot = QWidget(self._header_bar)
        self._header_server_dot.setObjectName("headerServerDot")
        self._header_server_dot.setFixedSize(6, 6)
        self._header_server_dot.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._header_server_dot.setStyleSheet(
            "#headerServerDot { background-color: #4caf7d; border-radius: 3px; border: none; }"
        )
        self._header_server_dot.setVisible(False)
        h_srv.addWidget(self._header_server_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self._header_server_port = QLabel("")
        self._header_server_port.setStyleSheet(
            "font-size: 11px; font-family: Consolas, Menlo, monospace; color: #8b95a5; "
            "border: none; background: transparent;"
        )
        self._header_server_port.setVisible(False)
        h_srv.addWidget(self._header_server_port, 0, Qt.AlignmentFlag.AlignVCenter)

        h_center.addWidget(self._status_model, 0, Qt.AlignmentFlag.AlignVCenter)
        h_center.addWidget(self._status_env_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        h_center.addWidget(self._status_serial_val, 0, Qt.AlignmentFlag.AlignVCenter)
        h_center.addWidget(self._status_fw, 0, Qt.AlignmentFlag.AlignVCenter)
        h_center.addWidget(self._claimed_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        h_center.addWidget(self._header_server_block, 0, Qt.AlignmentFlag.AlignVCenter)

        self._header_center.setVisible(False)
        h_header.addWidget(self._header_center, 0)

        h_header.addStretch(1)

        header_actions = QWidget()
        h_act = QHBoxLayout(header_actions)
        h_act.setContentsMargins(0, 0, 0, 0)
        h_act.setSpacing(8)

        self._btn_connect = QPushButton("Connect")
        self._btn_connect.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_connect.setStyleSheet(_HEADER_QSS_CONNECT)
        self._btn_connect.clicked.connect(self._open_connect_dialog)

        self._btn_disconnect = QPushButton("Disconnect")
        self._btn_disconnect.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_disconnect.setStyleSheet(_HEADER_QSS_DISCONNECT)
        self._btn_disconnect.clicked.connect(self._disconnect)
        self._btn_disconnect.setVisible(False)

        self._btn_help = QPushButton("Help")
        self._btn_help.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_help.setStyleSheet(_HEADER_QSS_OUTLINE)
        self._btn_help.clicked.connect(self._run_help)

        self._btn_clear = QPushButton("Clear log")
        self._btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear.setStyleSheet(_HEADER_QSS_OUTLINE)
        self._btn_clear.clicked.connect(self._clear_log)

        h_act.addWidget(self._btn_connect)
        h_act.addWidget(self._btn_disconnect)
        h_act.addWidget(_header_vertical_divider(self._header_bar), 0, Qt.AlignmentFlag.AlignVCenter)
        h_act.addWidget(self._btn_help)
        h_act.addWidget(self._btn_clear)
        h_header.addWidget(header_actions, 0)

        self._sync_status_strip()

        self._header_server_timer = QTimer(self)
        self._header_server_timer.setInterval(2000)
        self._header_server_timer.timeout.connect(self._sync_status_strip)
        self._header_server_timer.start()

        self._cmd_sidebar = QWidget()
        self._cmd_sidebar.setObjectName("cmdSidebar")
        self._cmd_sidebar.setFixedWidth(PANEL_FIXED_WIDTH)
        side_outer = QVBoxLayout(self._cmd_sidebar)
        side_outer.setContentsMargins(0, 0, 0, 0)
        side_outer.setSpacing(0)

        self._cmd_filter_row = QWidget()
        filter_lay = QHBoxLayout(self._cmd_filter_row)
        filter_lay.setContentsMargins(10, 4, 10, 4)
        filter_lay.setSpacing(0)
        self._filter_mag = QLabel("\u2315")
        self._filter_mag.setObjectName("filterSearchIcon")
        self._filter_mag.setFixedWidth(20)
        self._filter_mag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        filter_lay.addWidget(self._filter_mag, 0)

        self._cmd_filter_edit = QLineEdit()
        self._cmd_filter_edit.setObjectName("filterInput")
        self._cmd_filter_edit.setPlaceholderText("Filter commands...")
        self._cmd_filter_edit.setClearButtonEnabled(True)
        self._cmd_filter_edit.textChanged.connect(self._on_cmd_filter_text_changed)
        filter_lay.addWidget(self._cmd_filter_edit, stretch=1)

        self._cmd_scroll = QScrollArea()
        self._cmd_scroll.setObjectName("cmdScroll")
        self._cmd_scroll.setWidgetResizable(True)
        self._cmd_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._cmd_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._cmd_scroll_content = QWidget()
        self._cmd_scroll_content.setObjectName("cmdScrollContent")
        self._cmd_body_layout = QVBoxLayout(self._cmd_scroll_content)
        self._cmd_body_layout.setContentsMargins(0, 0, 0, 0)
        self._cmd_body_layout.setSpacing(0)
        self._cmd_scroll.setWidget(self._cmd_scroll_content)

        side_outer.addWidget(self._cmd_filter_row)
        side_outer.addWidget(self._cmd_scroll, stretch=1)

        self._tab_logs = QTabWidget()
        self._tab_logs.setObjectName("contentTabs")
        self._tab_logs.setDocumentMode(True)
        self._tab_logs.setMovable(True)
        self._tab_logs.setTabsClosable(True)
        self._tab_logs.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tab_logs.tabBar().tabMoved.connect(self._update_tab_close_buttons)
        self._tab_logs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._tab_logs.setStyleSheet(_content_tabs_stylesheet(welcome_solo_pane=False))
        self._welcome_tab_root, self._welcome_log = self._build_welcome_tab_widget()
        self._tab_logs.addTab(self._welcome_tab_root, "Welcome")
        self._e3_reference_widget: QWidget | None = None
        self._active_session_log: QTextEdit | None = None
        self._update_tab_close_buttons()

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.addWidget(self._cmd_sidebar)
        self._main_splitter.addWidget(self._tab_logs)
        self._main_splitter.setStretchFactor(0, 0)
        self._main_splitter.setStretchFactor(1, 1)

        self._cmd_input = QLineEdit()
        self._cmd_input.setObjectName("cmdFooterInput")
        self._cmd_input.setEnabled(False)
        self._cmd_input.setPlaceholderText("Enter command (e.g. help, status, reboot)…")
        self._cmd_input.returnPressed.connect(self._send_command)
        send_btn = QPushButton("Send")
        send_btn.clicked.connect(self._send_command)
        self._cmd_input_area = QWidget()
        self._cmd_input_area.setObjectName("cmdFooter")
        cmd_row = QHBoxLayout(self._cmd_input_area)
        cmd_row.setContentsMargins(10, 8, 10, 8)
        cmd_row.setSpacing(8)
        self._cmd_prompt_glyph = QLabel("›_")
        self._cmd_prompt_glyph.setObjectName("cmdPromptGlyph")
        cmd_row.addWidget(self._cmd_prompt_glyph, 0)
        cmd_row.addWidget(self._cmd_input, stretch=1)
        cmd_row.addWidget(send_btn, 0)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self._header_bar)
        layout.addWidget(self._main_splitter, stretch=1)
        layout.addWidget(self._cmd_input_area)
        self.setCentralWidget(central)

        self._setup_menu_bar()
        self._init_fw_folder_switcher_dock()
        self._init_local_server_dock()

        self._set_command_list_disconnected()
        self._prompt_model_name = "Device"
        self._refresh_shell_chrome_visibility()

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

    def _fw_wizard_model_dict_for_standalone(self) -> dict[str, Any]:
        """Default model context when opening FW Wizard from the welcome page (no session)."""
        models = get_models()
        m_info = next(
            (m for m in models if str(m.get("command_profile") or "") == "e3_wired"),
            None,
        )
        if m_info is None and models:
            m_info = models[0]
        if not m_info:
            return {
                "name": "VMC3070",
                "fw_search_models": ["VMC3070", "VMC2070"],
                "command_profile": "e3_wired",
                "is_onboarded": None,
            }
        primary = (m_info.get("name") or "Camera").strip() or "Camera"
        fw_search = list(m_info.get("fw_search_models") or [primary])
        return {
            "name": primary,
            "fw_search_models": fw_search,
            "command_profile": str(m_info.get("command_profile") or "e3_wired"),
            "is_onboarded": None,
        }

    def _open_fw_wizard_dialog(self, model_dict: dict[str, Any]) -> None:
        existing = getattr(self, "_fw_wizard", None)
        if existing is not None:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return
        from interface.fw_wizard import FwWizard

        wiz = FwWizard(self, model_dict, self._fw_shell_async)
        wiz.server_started.connect(self._on_fw_wizard_server_started)
        wiz.update_sent.connect(self._on_fw_wizard_update_sent)
        wiz.wizard_closed.connect(self._on_fw_wizard_closed)
        wiz.open_local_server_tool.connect(self._on_open_local_server_tool)
        self._fw_wizard = wiz
        wiz.apply_shell_connection(bool(self._device_connected))
        wiz.show()

    def _welcome_open_fw_wizard(self) -> None:
        QMessageBox.information(
            self,
            "FW Wizard — company VPN",
            "Firmware is downloaded from the company Artifactory server.\n\n"
            "Before continuing, make sure you are connected to the company VPN "
            "(GlobalProtect).\n\n"
            "Click OK to open the FW Wizard.",
        )
        self._open_fw_wizard_dialog(self._fw_wizard_model_dict_for_standalone())

    def _menu_fw_wizard(self) -> None:
        if not self._device_connected:
            QMessageBox.information(
                self,
                "FW Wizard",
                "Connect to a camera first (use Connect on the toolbar), then choose Tools → FW Wizard.",
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
        self._open_fw_wizard_dialog(model_dict)

    def _fw_shell_async(
        self, cmd: str, args: list[str], on_done: Callable[[bool, str], None]
    ) -> None:
        self._fw_shell_pending = on_done
        cs = (cmd or "").strip()
        self._fw_shell_redetect_after = cs == "arlocmd update_url" and bool(args)
        self._worker.fw_shell_request.emit(cmd, args)

    @Slot(bool, str)
    def _on_fw_shell_response(self, ok: bool, text: str) -> None:
        cb = self._fw_shell_pending
        want_redetect = self._fw_shell_redetect_after
        self._fw_shell_pending = None
        self._fw_shell_redetect_after = False
        if cb:
            cb(ok, text or "")
        if ok and want_redetect:
            self._worker.refresh_update_url_readback.emit()

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
            f"About {APP_NAME}",
            f"<h3>{APP_NAME}</h3>"
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

    def _new_log_editor(self, *, tail: bool = False) -> QTextEdit | LogViewerWidget:
        """Plain terminal log by default; structured viewer only for live tail / parse tabs."""
        if tail:
            log = LogViewerWidget(show_transport_badge=True, tail_mode=True, parent=self)
            log.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            return log
        log = QTextEdit()
        log.setReadOnly(True)
        f = QFont("Menlo", 11) if sys.platform == "darwin" else QFont("Consolas", 10)
        log.setFont(f)
        return log

    def _build_welcome_tab_widget(self) -> tuple[QWidget, QTextEdit]:
        """Launcher-style welcome: branding, connection cards, tools, hidden log target."""
        root = QWidget()
        root.setStyleSheet(f"QWidget#welcomeTabRoot {{ background-color: {_WELCOME_PAGE_BG}; }}")
        root.setObjectName("welcomeTabRoot")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addStretch(1)

        band = QWidget()
        band_lay = QHBoxLayout(band)
        band_lay.setContentsMargins(16, 16, 16, 16)
        band_lay.addStretch(1)

        col = QWidget()
        col.setMaximumWidth(540)
        inner = QVBoxLayout(col)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)

        icon_lbl = QLabel()
        icon_lbl.setAutoFillBackground(False)
        icon_lbl.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        icon_lbl.setStyleSheet("border: none; background: transparent;")
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _ip = _main_window_icon_path()
        if _ip:
            _px = _load_png_pixmap(_ip)
            if not _px.isNull():
                icon_lbl.setPixmap(
                    _px.scaled(
                        48,
                        48,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
        _pm_brand = icon_lbl.pixmap()
        if _pm_brand is None or _pm_brand.isNull():
            icon_lbl.setText("◉")
            icon_lbl.setStyleSheet(
                f"color: {ARLO_ACCENT_COLOR}; font-size: 34px; border: none; background: transparent;"
            )
        inner.addWidget(icon_lbl)

        title_w = QLabel(APP_NAME)
        tf = QFont()
        _safe_set_point_size(tf, 22, context="welcome title")
        tf.setBold(True)
        title_w.setFont(tf)
        title_w.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_w.setStyleSheet(f"color: {ARLO_ACCENT_COLOR}; border: none; background: transparent;")
        inner.addWidget(title_w)

        proto_l = QLabel("QE Tool")
        proto_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ptf = QFont()
        _safe_set_point_size(ptf, 10, context="welcome tagline")
        ptf.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
        proto_l.setFont(ptf)
        proto_l.setStyleSheet(
            f"color: {_WELCOME_SECTION_LABEL_COLOR}; border: none; background: transparent;"
        )
        inner.addWidget(proto_l)
        inner.addSpacing(28)

        methods = _supported_connection_methods_union()
        cards_row = QHBoxLayout()
        cards_row.setSpacing(16)
        if "ADB" in methods:
            c_adb = WelcomeConnectionCard(
                "⎆",
                ARLO_ACCENT_COLOR,
                "USB / ADB",
                "Wired connection via USB.",
                parent=root,
            )
            c_adb.clicked.connect(lambda: self._open_connect_dialog(preferred_method="ADB"))
            cards_row.addWidget(c_adb, 1)
        if "SSH" in methods:
            c_ssh = WelcomeConnectionCard(
                "⊕",
                _WELCOME_BLUE,
                "SSH",
                "Network connection.",
                parent=root,
            )
            c_ssh.clicked.connect(lambda: self._open_connect_dialog(preferred_method="SSH"))
            cards_row.addWidget(c_ssh, 1)
        if "UART" in methods:
            c_uart = WelcomeConnectionCard(
                "∞",
                _WELCOME_AMBER,
                "UART",
                "Serial connection.",
                parent=root,
            )
            c_uart.clicked.connect(lambda: self._open_connect_dialog(preferred_method="UART"))
            cards_row.addWidget(c_uart, 1)
        if cards_row.count() < 1:
            hint = QLabel("No connection methods are configured for any device model.")
            hint.setWordWrap(True)
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet("color: #8b95a5; font-size: 12px;")
            cards_row.addWidget(hint, 1)
        inner.addLayout(cards_row)

        inner.addSpacing(28)
        inner.addWidget(_welcome_section_label("TOOLS"))
        inner.addSpacing(10)

        tools_row = QHBoxLayout()
        tools_row.setSpacing(16)
        fw_card = WelcomeConnectionCard(
            "+",
            ARLO_ACCENT_COLOR,
            "FW Wizard",
            "Download and set up firmware from Artifactory.",
            parent=root,
        )
        fw_card.clicked.connect(self._welcome_open_fw_wizard)
        tools_row.addWidget(fw_card, 1)
        ls_card = WelcomeConnectionCard(
            "▣",
            ARLO_ACCENT_COLOR,
            "Local Server",
            "Manage firmware folders and serve to cameras.",
            parent=root,
        )
        ls_card.clicked.connect(self._on_open_local_server_tool)
        tools_row.addWidget(ls_card, 1)
        inner.addLayout(tools_row)

        inner.addSpacing(20)

        ver_l = QLabel(f"Version {APP_VERSION}")
        ver_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vf = QFont()
        _safe_set_point_size(vf, 9, context="welcome version")
        ver_l.setFont(vf)
        ver_l.setStyleSheet(
            f"color: {_WELCOME_SECTION_LABEL_COLOR}; border: none; background: transparent;"
        )
        inner.addWidget(ver_l)

        band_lay.addWidget(col, 0, Qt.AlignmentFlag.AlignTop)
        band_lay.addStretch(1)
        outer.addWidget(band, 0)
        outer.addStretch(1)

        welcome_log = self._new_log_editor()
        welcome_log.setMinimumHeight(0)
        welcome_log.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        welcome_log.hide()
        outer.addWidget(welcome_log, stretch=0)

        return root, welcome_log

    def _restore_main_splitter_sizes(self) -> None:
        """Give the commands panel a sane width when it becomes visible after connect."""
        if not getattr(self, "_main_splitter", None):
            return
        side_w = PANEL_FIXED_WIDTH
        self._main_splitter.setSizes([side_w, max(200, self.width() - side_w - 32)])

    def _refresh_shell_chrome_visibility(self) -> None:
        """Full-width welcome when disconnected; restore sidebar, tabs bar, and command row when connected."""
        if not hasattr(self, "_cmd_input_area"):
            return
        conn = self._device_connected
        self._cmd_sidebar.setVisible(conn)
        self._cmd_input_area.setVisible(conn)
        welcome_only = (
            not conn
            and self._status_phase != "connecting"
            and self._tab_logs.count() == 1
            and self._tab_logs.widget(0) is self._welcome_tab_root
        )
        self._tab_logs.tabBar().setVisible(not welcome_only)
        self._tab_logs.setStyleSheet(_content_tabs_stylesheet(welcome_solo_pane=welcome_only))
        if conn:
            QTimer.singleShot(0, self._restore_main_splitter_sizes)

    def _set_status_dot_color(self, color: str) -> None:
        # QWidget + WA_StyledBackground: QFrame default frame style breaks stylesheet parse/fill.
        self._status_dot.setStyleSheet(
            f"#statusDot {{ background-color: {color}; border-radius: 4px; border: none; }}"
        )

    def _sync_status_strip(self) -> None:
        center_widgets = (
            self._status_model,
            self._status_env_badge,
            self._status_serial_val,
            self._status_fw,
            self._claimed_badge,
        )

        if self._status_phase == "connecting":
            self._set_status_dot_color(STATUS_DOT_CONNECTING)
            self._header_center.setVisible(False)
            for w in center_widgets:
                w.setVisible(False)
            self._header_server_block.setVisible(False)
            self._claimed_badge.setVisible(False)
            self._status_text.setText("Connecting…")
            self._status_text.setStyleSheet(
                f"color: {STATUS_DOT_CONNECTING}; font-size: 12px; font-weight: 600; "
                "border: none; background: transparent;"
            )
            self._btn_connect.setVisible(False)
            self._btn_disconnect.setVisible(True)
            self._btn_disconnect.setEnabled(True)
        elif self._device_connected:
            self._set_status_dot_color(STATUS_DOT_CONNECTED)
            self._header_center.setVisible(True)
            for w in center_widgets:
                w.setVisible(True)
            self._status_text.setText("Connected")
            self._status_text.setStyleSheet(
                f"color: {STATUS_DOT_CONNECTED}; font-size: 12px; font-weight: 600; "
                "border: none; background: transparent;"
            )

            m = self._status_detail_model or "—"
            self._status_model.setText(m)
            env_internal = (self._status_detail_env or "—").strip() or "—"
            env_display = _env_stage_display_label(env_internal)
            if env_internal and env_internal != "—":
                self._status_env_badge.setText(env_display)
                self._status_env_badge.setStyleSheet(_env_stage_badge_qss(env_internal))
                self._status_env_badge.setVisible(True)
            else:
                self._status_env_badge.clear()
                self._status_env_badge.setVisible(False)
            url_raw = (getattr(self, "_status_update_url_raw", "") or "").strip()
            tip_lines: list[str] = []
            if m != "—":
                tip_lines.append(m)
            if env_internal and env_internal != "—":
                tip_lines.append(f"Environment: {env_display}")
            if url_raw:
                tip_lines.append(f"Update URL: {url_raw}")
            self._status_model.setToolTip("\n".join(tip_lines) if tip_lines else "")

            fw_raw = (self._status_detail_fw or "").strip()
            if not fw_raw or fw_raw == "—":
                fw_line = "—"
                fw_copy = "—"
            else:
                fw_line = fw_raw if fw_raw.upper().startswith("FW") else f"FW {fw_raw}"
                fw_copy = fw_line
            fm_fw = QFontMetrics(self._status_fw.font())
            fw_disp = _elide_status_value(fw_line, 160, fm_fw)
            self._status_fw.set_copy_value(fw_copy, fw_disp)

            ser = (getattr(self, "_status_detail_serial", "") or "").strip() or "—"
            fm_ser = QFontMetrics(self._status_serial_val.font())
            ser_disp = _elide_status_value(ser, 200, fm_ser)
            self._status_serial_val.set_copy_value(ser, ser_disp)

            ob = getattr(self, "_device_is_onboarded", None)
            if ob is True:
                self._claimed_badge.setText("Onboarded")
                self._claimed_badge.setStyleSheet(_CLAIMED_BADGE_ONBOARDED_QSS)
                self._claimed_badge.setVisible(True)
            elif ob is False:
                self._claimed_badge.setText("Not claimed")
                self._claimed_badge.setStyleSheet(_CLAIMED_BADGE_NOT_CLAIMED_QSS)
                self._claimed_badge.setVisible(True)
            else:
                self._claimed_badge.setVisible(False)

            port = _firmware_listen_port_for_header()
            if port is not None:
                self._header_server_block.setVisible(True)
                self._header_server_div.setVisible(True)
                self._header_server_dot.setVisible(True)
                self._header_server_port.setVisible(True)
                self._header_server_port.setText(f":{port}")
            else:
                self._header_server_block.setVisible(False)

            self._btn_connect.setVisible(False)
            self._btn_disconnect.setVisible(True)
            self._btn_disconnect.setEnabled(True)
        else:
            self._set_status_dot_color(STATUS_DOT_DISCONNECTED)
            self._header_center.setVisible(False)
            for w in center_widgets:
                w.setVisible(False)
            self._header_server_block.setVisible(False)
            self._claimed_badge.setVisible(False)
            self._status_text.setText("Not connected")
            self._status_text.setStyleSheet(
                "color: #8b95a5; font-size: 12px; font-weight: 500; "
                "border: none; background: transparent;"
            )
            self._btn_connect.setVisible(True)
            self._btn_disconnect.setVisible(False)
            self._btn_disconnect.setEnabled(False)

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
        edit: LogViewerWidget = state["edit"]
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
            edit.append_plain(text)
        if final:
            edit.flush_partial_line()

    def _finalize_live_tail_state(self, state: dict[str, Any]) -> None:
        timer = state.get("timer")
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._tail_read_chunk(state, final=True)
        edit = state["edit"]
        if isinstance(edit, LogViewerWidget):
            edit.set_tail_streaming(False)
        idx = self._tab_logs.indexOf(edit)
        if idx >= 0:
            cur = self._tab_logs.tabText(idx)
            if " (stopped)" not in cur:
                self._tab_logs.setTabText(idx, (cur + " (stopped)")[:44])

    @Slot(str, str)
    def tail_live_open_tab(self, path: str, title: str) -> None:
        """Marshaled from worker thread via QMetaObject.invokeMethod (log tail / log parse)."""
        self._on_tail_live_start(path, title)

    @Slot(str)
    def tail_live_stop_tab(self, path: str) -> None:
        self._on_tail_live_stop(path)

    @Slot(str, str)
    def _on_tail_live_start(self, path: str, title: str) -> None:
        key = self._tail_path_key(path)
        if key in self._live_tail_sessions:
            return
        edit = self._new_log_editor(tail=True)
        edit.set_tail_streaming(True)
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
        if self._tab_logs.tabsClosable():
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
        if hasattr(self, "_cmd_input_area"):
            self._refresh_shell_chrome_visibility()

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
            ed = state.get("edit")
            if isinstance(ed, LogViewerWidget):
                ed.set_tail_streaming(False)
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
        line.setObjectName("cmdPanelSeparator")
        line.setFixedHeight(1)
        prepare_qframe_for_qss(line)
        return line

    def _clear_cmd_panel_body(self) -> None:
        self._cmd_filter_rows.clear()
        self._cmd_advanced_rows.clear()
        self._cmd_panel_groups.clear()
        self._cmd_tier1_outer = None
        self._cmd_tools_outer = None
        self._cmd_adv_block_ref = None
        self._cmd_device_catalog_outer = None
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
        sec = _row_section_from_group(group)
        risk = _cmd_risk_badge_id(cmd_key)
        row = _CommandRowFrame(
            cmd_key=cmd_key,
            display_line=disp,
            args_hint=args_hint,
            tooltip=tip,
            tier=tier,
            section=sec,
            risk_badge=risk,
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
            if self._cmd_device_catalog_outer is not None:
                self._cmd_device_catalog_outer.setVisible(True)
            if self._cmd_sep_t1_tools is not None:
                self._cmd_sep_t1_tools.setVisible(self._cmd_tier1_outer is not None)
            if self._cmd_sep_tools_adv is not None:
                self._cmd_sep_tools_adv.setVisible(
                    self._cmd_adv_block_ref is not None or self._cmd_device_catalog_outer is not None
                )
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

        if self._cmd_device_catalog_outer is not None:
            d_any = any(
                r.isVisible()
                for r, _ in self._cmd_filter_rows
                if self._cmd_device_catalog_outer.isAncestorOf(r)
            )
            self._cmd_device_catalog_outer.setVisible(d_any)

        if self._cmd_sep_t1_tools is not None:
            t1v = self._cmd_tier1_outer is not None and self._cmd_tier1_outer.isVisible()
            ttv = self._cmd_tools_outer is not None and self._cmd_tools_outer.isVisible()
            self._cmd_sep_t1_tools.setVisible(t1v and ttv)

        if self._cmd_sep_tools_adv is not None:
            ttv = self._cmd_tools_outer is not None and self._cmd_tools_outer.isVisible()
            advv = self._cmd_adv_block_ref is not None and self._cmd_adv_block_ref.isVisible()
            devv = self._cmd_device_catalog_outer is not None and self._cmd_device_catalog_outer.isVisible()
            self._cmd_sep_tools_adv.setVisible(ttv and (advv or devv))

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

    def _reset_main_window_to_disconnected(self, *, clear_session_log: bool = True) -> None:
        """
        Clear status strip and panels so we do not show a stale model/serial as connected.

        When ``clear_session_log`` is False, the active session log tab is kept so the worker can
        still append the disconnect line before ``state_changed`` runs (watchdog path).
        """
        self._watchdog_transport_epoch += 1
        self._transport_watchdog_timer.stop()
        self._watchdog_fail_streak = 0
        self._watchdog_poll_busy = False
        self._watchdog_uart_port = ""
        self._preferred_adb_serial = None
        self._device_connected = False
        self._command_profile = "none"
        self._conn_type = ""
        self._action_fw_wizard.setEnabled(False)
        self._cmd_input.setEnabled(False)
        self._status_phase = "disconnected"
        self._prompt_model_name = "Device"
        self._device_is_onboarded = None
        self._status_detail_model = "—"
        self._status_detail_env = "—"
        self._status_detail_fw = "—"
        self._status_detail_serial = ""
        self._status_update_url_raw = ""
        if clear_session_log:
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

    @Slot(dict)
    def _on_state_changed(self, info: dict) -> None:
        if info.get("connected"):
            self._production_connect_resume = None
            self._device_connected = True
            self._command_profile = str(info.get("command_profile") or "none")
            self._conn_type = str(info.get("conn_type") or "")
            self._action_fw_wizard.setEnabled(self._command_profile == "e3_wired")
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
                did_adb = str(info.get("device_id") or "").strip()
                self._preferred_adb_serial = did_adb or None
            else:
                self._preferred_adb_serial = None
            ct_u = str(info.get("conn_type") or "").strip().upper()
            if ct_u == "UART":
                did_uart = str(info.get("device_id") or "").strip()
                self._watchdog_uart_port = (did_uart.split("@", 1)[0].strip() if did_uart else "")
            else:
                self._watchdog_uart_port = ""
            self._watchdog_fail_streak = 0
            self._watchdog_poll_busy = False
            self._transport_watchdog_timer.start()
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
            self._reset_main_window_to_disconnected()

    @Slot(list, str)
    def _merge_command_list(self, device_cmds: list, command_profile: str = "none") -> None:
        prof = ((command_profile or "").strip() or getattr(self, "_command_profile", "none") or "none")
        self._command_profile = prof
        conn = getattr(self, "_conn_type", "") or ""
        self._clear_cmd_panel_body()
        self._cmd_filter_row.setVisible(True)
        _, advanced = get_visible_commands(list(device_cmds), command_profile=prof)

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
                        args_hint="",
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
        tools_hdr = QLabel("Tools")
        tools_hdr.setObjectName("cmdToolsBanner")
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

        has_device_catalog = bool(advanced)
        by_cat: defaultdict[str, list[dict]] = defaultdict(list)
        ordered_cats: list[str] = []
        if has_device_catalog:
            for c in advanced:
                if not isinstance(c, dict):
                    continue
                cat = (str(c.get("category") or "other")).strip().lower() or "other"
                by_cat[cat].append(c)
            seen_cat: set[str] = set()
            for k in _ADV_DEVICE_CATEGORY_ORDER:
                if k in by_cat:
                    ordered_cats.append(k)
                    seen_cat.add(k)
            for k in sorted(by_cat.keys()):
                if k not in seen_cat:
                    ordered_cats.append(k)

        def _append_device_category_blocks(target_layout: QVBoxLayout, *, is_advanced_tier: bool) -> None:
            for cat_key in ordered_cats:
                cmds = sorted(by_cat[cat_key], key=lambda x: str(x.get("name", "")).lower())
                if not cmds:
                    continue
                cblk = _CollapsibleCategoryBlock(
                    cat_key.upper(),
                    expanded_default=True,
                    parent=self._cmd_scroll_content,
                    section=_adv_catalog_to_section(cat_key),
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
                        args_hint="",
                        meta=c,
                        tier=3,
                        group=cblk,
                        is_advanced=is_advanced_tier,
                    )
                target_layout.addWidget(cblk)

        # E3: tier-1 abstracts, then Tools, then raw catalog under collapsed ADVANCED.
        # Other profiles: device shell catalog first, then Tools (matches UART-first workflow).
        if prof == "e3_wired":
            self._cmd_body_layout.addWidget(self._cmd_tools_outer)
            if has_device_catalog:
                self._cmd_sep_tools_adv = self._make_cmd_panel_separator()
                self._cmd_body_layout.addWidget(self._cmd_sep_tools_adv)
                adv = _AdvancedTierBlock(self._cmd_scroll_content)
                self._cmd_adv_block_ref = adv
                _append_device_category_blocks(adv.body_layout(), is_advanced_tier=True)
                self._cmd_body_layout.addWidget(adv)
        else:
            self._cmd_adv_block_ref = None
            if has_device_catalog:
                device_outer = QWidget(self._cmd_scroll_content)
                dev_lay = QVBoxLayout(device_outer)
                dev_lay.setContentsMargins(0, 0, 0, 0)
                dev_lay.setSpacing(0)
                hdr = QLabel("Commands")
                hdr.setObjectName("cmdDeviceCatalogBanner")
                dev_lay.addWidget(hdr)
                _append_device_category_blocks(dev_lay, is_advanced_tier=False)
                self._cmd_device_catalog_outer = device_outer
                self._cmd_sep_tools_adv = self._make_cmd_panel_separator()
                self._cmd_body_layout.addWidget(device_outer)
                self._cmd_body_layout.addWidget(self._cmd_sep_tools_adv)
            self._cmd_body_layout.addWidget(self._cmd_tools_outer)

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
        resume = self._production_connect_resume
        self._production_connect_resume = None
        if resume:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("Connection failed")
            box.setText(msg)
            box.setInformativeText("Return to the Production instructions and try again, or close this dialog.")
            try_btn = box.addButton("Try again", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() == try_btn:
                self._open_connect_dialog(initial_tab="production", production_resume=resume)
            return
        QMessageBox.critical(self, "Connection failed", msg)

    def _disconnect(self) -> None:
        self._worker.disconnect_session.emit()

    def _on_transport_watchdog_tick(self) -> None:
        if not self._device_connected or self._watchdog_poll_busy:
            return
        ct = (self._conn_type or "").strip().upper()
        adb_serial = ""
        uart_port = ""
        if ct == "ADB":
            adb_serial = (self._preferred_adb_serial or "").strip()
            if not adb_serial:
                return
        elif ct == "UART":
            uart_port = (self._watchdog_uart_port or "").strip()
            if not uart_port:
                return
        else:
            return

        self._watchdog_poll_busy = True
        epoch_snapshot = self._watchdog_transport_epoch
        adb_copy = adb_serial
        uart_copy = uart_port
        ct_copy = ct

        def _run_poll() -> None:
            try:
                if ct_copy == "ADB":
                    ok = adb_serial_transport_alive(adb_copy)
                else:
                    ok = uart_port_transport_alive_for_watchdog(uart_copy)
                self.transport_watchdog_poll_done.emit(ok, epoch_snapshot)
            except Exception:
                self.transport_watchdog_poll_done.emit(True, epoch_snapshot)

        threading.Thread(target=_run_poll, daemon=True).start()

    @Slot(bool, int)
    def _on_transport_watchdog_poll_done(self, ok: bool, epoch: int) -> None:
        self._watchdog_poll_busy = False
        if epoch != self._watchdog_transport_epoch:
            return
        if not self._device_connected:
            return
        if ok:
            self._watchdog_fail_streak = 0
            return
        self._watchdog_fail_streak += 1
        if self._watchdog_fail_streak >= _TRANSPORT_WATCHDOG_FAIL_STREAK:
            self._watchdog_fail_streak = 0
            self._reset_main_window_to_disconnected(clear_session_log=False)
            self._worker.forced_disconnect.emit(
                "Connection lost — device disconnected. Use Connect to reconnect."
            )

    def _clear_log(self) -> None:
        w = self._tab_logs.currentWidget()
        if w is self._welcome_tab_root:
            self._welcome_log.clear()
            self._welcome_log.setMinimumHeight(0)
            self._welcome_log.hide()
        elif isinstance(w, (QTextEdit, LogViewerWidget)):
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

    def _open_connect_dialog(
        self,
        *,
        initial_tab: str = "dev",
        production_resume: dict[str, Any] | None = None,
        preferred_method: str | None = None,
    ) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Connect to camera")
        outer = QVBoxLayout(dlg)
        connect_result: dict[str, Any] = {}

        pref_raw = (preferred_method or "").strip().upper()
        pref_method = pref_raw if pref_raw in ("ADB", "SSH", "UART") else ""

        models = get_models()
        dev_tab = QWidget()
        layout = QVBoxLayout(dev_tab)
        layout.setContentsMargins(0, 0, 0, 0)

        device_combo = QComboBox()
        device_combo.setPlaceholderText("Select a camera…")
        for m in models:
            device_combo.addItem(format_connect_dialog_device_label(m), m)

        dev_connect = QPushButton("Connect")
        dev_connect.setEnabled(False)

        def _sync_dev_connect_enabled() -> None:
            dev_connect.setEnabled(isinstance(device_combo.currentData(), dict))

        method = QComboBox()

        uart_box = QGroupBox("UART")
        uart_form = QFormLayout(uart_box)
        uart_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        uart_form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        uart_form.setHorizontalSpacing(10)
        uart_form.setVerticalSpacing(8)
        uart_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        uart_port = QComboBox()
        uart_port.setMinimumContentsLength(18)
        uart_port.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        uart_port.setMinimumWidth(140)
        uart_refresh = QPushButton("Refresh ports")

        def _repop_uart_ports() -> None:
            uart_port.blockSignals(True)
            uart_port.clear()
            ports = list_uart_ports()
            if not ports:
                uart_port.addItem(
                    "No serial ports found — connect USB-UART, then Refresh",
                    None,
                )
            else:
                for p, desc in ports:
                    uart_port.addItem(f"{desc} ({p})", p)
            uart_port.blockSignals(False)

        _repop_uart_ports()

        uart_baud = QLineEdit(str(DEFAULT_UART_BAUD))
        _configure_connect_dialog_baud_lineedit(uart_baud)

        uart_port_row = QHBoxLayout()
        uart_port_row.setContentsMargins(0, 0, 0, 0)
        uart_port_row.setSpacing(8)
        uart_port_row.addWidget(uart_port, 1)
        uart_port_row.addWidget(uart_refresh, 0)
        w_uart_port = QWidget()
        w_uart_port.setLayout(uart_port_row)
        uart_form.addRow("Port:", w_uart_port)
        uart_form.addRow("Baud:", uart_baud)

        uart_refresh.clicked.connect(_repop_uart_ports)

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
        mcu_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        mcu_form.setHorizontalSpacing(10)
        mcu_form.setVerticalSpacing(8)
        mcu_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        mcu_port = QComboBox()
        mcu_port.setMinimumContentsLength(14)
        mcu_port.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        mcu_port.setMinimumWidth(120)
        mcu_refresh = QPushButton("Refresh")
        mcu_baud = QLineEdit("115200")
        _configure_connect_dialog_baud_lineedit(mcu_baud)
        mcu_row = QHBoxLayout()
        mcu_row.setContentsMargins(0, 0, 0, 0)
        mcu_row.setSpacing(8)
        mcu_row.addWidget(mcu_port, 1)
        mcu_row.addWidget(mcu_refresh, 0)
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
            if not key:
                uart_box.setVisible(False)
                mcu_uart_inner.setVisible(False)
                adb_box.setVisible(False)
                ssh_box.setVisible(False)
                return
            is_g5 = isinstance(m, dict) and (
                str(m.get("command_profile") or "") == "gen5"
                or str(m.get("platform") or "").lower() == "gen5"
            )
            uart_box.setVisible(key == "UART")
            mcu_uart_inner.setVisible(key == "UART" and is_g5)
            adb_box.setVisible(key == "ADB")
            ssh_box.setVisible(key == "SSH")
            if key == "UART":
                _sync_uart_baud_from_model()

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
                method.addItem("Select a camera above", "")
                method.setEnabled(False)
                return
            method.setEnabled(True)
            allowed = connection_methods_upper(m)
            opts = [
                ("UART (serial)", "UART"),
                ("ADB (USB)", "ADB"),
                ("SSH", "SSH"),
            ]
            for label, key in opts:
                if key in allowed:
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
            b = default_uart_baud_for_model_group(m if isinstance(m, dict) else None)
            if b is not None:
                uart_baud.setText(str(b))

        def _apply_preferred_connection_method() -> None:
            """Welcome-card shortcut (USB / SSH / UART): pre-pick method after user selects a model."""
            if not pref_method or not method.isEnabled():
                return
            for j in range(method.count()):
                if method.itemData(j) == pref_method:
                    method.blockSignals(True)
                    method.setCurrentIndex(j)
                    method.blockSignals(False)
                    break

        def _on_device_changed(_i: int) -> None:
            _refill_methods()
            _apply_preferred_connection_method()
            _sync_uart_baud_from_model()
            refresh_mcu_ports()
            update_visible(method.currentIndex())
            _sync_dev_connect_enabled()

        device_combo.currentIndexChanged.connect(_on_device_changed)
        device_combo.setCurrentIndex(-1)
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

        device_lbl = QLabel("Camera model")
        device_lbl.setStyleSheet("color: #e8eef4; font-size: 12px; font-weight: 600;")
        layout.addWidget(device_lbl)
        device_pick_hint = QLabel(
            "Choose your camera from the list below. Connection options (UART, ADB, SSH) appear "
            "after you select a model."
        )
        device_pick_hint.setWordWrap(True)
        device_pick_hint.setStyleSheet(
            "color: #aeb8c4; font-size: 11px; padding: 0 0 4px 0; border: none; background: transparent;"
        )
        layout.addWidget(device_pick_hint)
        layout.addWidget(device_combo)
        layout.addWidget(QLabel("Connection method:"))
        layout.addWidget(method)
        layout.addWidget(stack)

        dev_btn_row = QHBoxLayout()
        dev_btn_row.addStretch()
        dev_btn_row.addWidget(dev_connect)
        layout.addLayout(dev_btn_row)

        prod_tab = QWidget()
        prod_outer = QVBoxLayout(prod_tab)
        prod_outer.setContentsMargins(0, 0, 0, 0)
        prod_stack = QStackedWidget()

        prod_pick = QWidget()
        pl = QVBoxLayout(prod_pick)
        prod_device_combo = QComboBox()
        for m in models:
            if model_supports_adb(m):
                prod_device_combo.addItem(format_connect_dialog_device_label(m), m)
        pl.addWidget(QLabel("Select device (USB / ADB only):"))
        pl.addWidget(prod_device_combo)
        btn_pc = QPushButton("Continue")
        pl.addWidget(btn_pc)
        pl.addStretch()

        prod_instr = QWidget()
        pil = QVBoxLayout(prod_instr)
        prod_instr_text = QLabel("")
        prod_instr_text.setWordWrap(True)
        prod_instr_text.setStyleSheet("color: #e8eef4; font-size: 13px;")
        pil.addWidget(prod_instr_text)
        btn_go = QPushButton("I've done this — Connect")
        pil.addWidget(btn_go)
        btn_pb = QPushButton("Back")
        pil.addWidget(btn_pb)
        pil.addStretch()

        prod_stack.addWidget(prod_pick)
        prod_stack.addWidget(prod_instr)
        prod_outer.addWidget(prod_stack)

        _style_connect_dialog_comboboxes(
            device_combo, method, uart_port, mcu_port, prod_device_combo
        )

        def _production_device_title(m: dict) -> str:
            return format_connect_dialog_device_label(m)

        def _refresh_prod_instructions() -> None:
            m = prod_device_combo.currentData()
            if not isinstance(m, dict):
                prod_instr_text.setText("")
                return
            title = _production_device_title(m)
            prod_instr_text.setText(
                f"Put your {title} into debug mode\n\n"
                "Your device is running Production firmware. To connect, you need to enable debug mode first.\n\n"
                "1. Make sure the device is powered on and the USB cable is connected to your computer.\n"
                "2. Press the sync button 6 times rapidly (within about 3 seconds).\n"
                "3. Wait for the LED to indicate the device has entered debug mode.\n"
                "4. Click the \"I've done this — Connect\" button below."
            )

        def _prod_adb_pick_serial() -> str | None:
            serials = ADBHandler.list_attached_usb_serials()
            adb_serial = ""
            if len(serials) > 1:
                pref = (self._preferred_adb_serial or "").strip()
                if pref and pref in serials:
                    adb_serial = pref
                else:
                    pick = _AdbDevicePickerDialog(self, serials)
                    if pick.exec() != QDialog.DialogCode.Accepted:
                        return None
                    adb_serial = (pick.selected_serial() or "").strip()
                    if not adb_serial:
                        return None
            elif len(serials) == 1:
                adb_serial = serials[0]
            return adb_serial

        def _run_prod_connect() -> None:
            m_sel = prod_device_combo.currentData()
            if not isinstance(m_sel, dict):
                return
            if not resolve_production_adb_password(m_sel):
                QMessageBox.warning(
                    self,
                    "Production",
                    "No production ADB credentials are configured for this device.",
                )
                return
            try:
                ensure_adb_allowed_for_selection(m_sel)
            except UnsupportedConnectionError as e:
                QMessageBox.critical(self, "ADB not supported", str(e))
                return
            picked = _prod_adb_pick_serial()
            if picked is None:
                return
            connect_result.clear()
            connect_result["mode"] = "prod_adb"
            connect_result["model"] = m_sel
            connect_result["adb_serial"] = picked
            dlg.accept()

        def _prod_continue() -> None:
            if prod_device_combo.count() == 0:
                QMessageBox.warning(self, "Production", "No ADB-capable devices are available.")
                return
            _refresh_prod_instructions()
            prod_stack.setCurrentIndex(1)

        btn_pc.clicked.connect(_prod_continue)
        btn_go.clicked.connect(_run_prod_connect)
        btn_pb.clicked.connect(lambda: prod_stack.setCurrentIndex(0))

        tab_w = QTabWidget()
        tab_w.addTab(dev_tab, "Dev / QA")
        tab_w.addTab(prod_tab, "Production")
        outer.addWidget(tab_w)

        cancel_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        cancel_box.rejected.connect(dlg.reject)
        outer.addWidget(cancel_box)

        if (production_resume or initial_tab == "production") and tab_w.count() > 1:
            tab_w.setCurrentIndex(1)
            res_model = (production_resume or {}).get("model") if production_resume else None
            if isinstance(res_model, dict):
                want = str(res_model.get("name") or "").strip().upper()
                for i in range(prod_device_combo.count()):
                    md = prod_device_combo.itemData(i)
                    if isinstance(md, dict) and str(md.get("name") or "").strip().upper() == want:
                        prod_device_combo.setCurrentIndex(i)
                        break
            _refresh_prod_instructions()
            prod_stack.setCurrentIndex(1)

        def _attempt_dev_connect() -> None:
            key = method.currentData()
            m_sel = device_combo.currentData()
            if not isinstance(m_sel, dict):
                QMessageBox.warning(
                    self,
                    "Connect",
                    "Select a camera model from the dropdown first.",
                )
                return
            if key == "UART":
                port = uart_port.currentData()
                if port is None or str(port).strip() == "":
                    QMessageBox.warning(
                        self,
                        "UART",
                        "No serial port selected. Connect a USB-UART adapter and click "
                        "Refresh ports, then choose your COM or tty device.",
                    )
                    return
                baud = _parse_connect_baud_text(uart_baud.text())
                if baud is None:
                    QMessageBox.warning(
                        self,
                        "UART",
                        "Enter a valid baud rate (whole number ≥ 1), e.g. 115200 or 1500000.",
                    )
                    return
                mcu_raw = mcu_port.currentData()
                mcu_p = str(mcu_raw).strip() if mcu_raw else ""
                mcu_br = 0
                if mcu_p:
                    mcu_br = _parse_connect_baud_text(mcu_baud.text()) or 0
                    if mcu_br < 1:
                        QMessageBox.warning(
                            self,
                            "UART",
                            "Enter a valid MCU baud rate (whole number ≥ 1).",
                        )
                        return
                if mcu_p and _uart_ports_equivalent(mcu_p, str(port)):
                    QMessageBox.warning(
                        self,
                        "UART",
                        "MCU UART cannot use the same COM port as the ISP/main UART.",
                    )
                    return
                connect_result.clear()
                connect_result.update(
                    {
                        "mode": "dev_uart",
                        "port": port,
                        "baud": baud,
                        "model": m_sel,
                        "mcu_p": mcu_p,
                        "mcu_br": mcu_br,
                    }
                )
                dlg.accept()
                return
            if key == "ADB":
                try:
                    ensure_adb_allowed_for_selection(m_sel)
                except UnsupportedConnectionError as e:
                    QMessageBox.critical(self, "ADB not supported", str(e))
                    return
                serials = ADBHandler.list_attached_usb_serials()
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
                elif len(serials) == 1:
                    adb_serial = serials[0]
                connect_result.clear()
                connect_result["mode"] = "dev_adb"
                connect_result["model"] = m_sel
                connect_result["adb_serial"] = adb_serial
                connect_result["password"] = adb_pwd.text()
                dlg.accept()
                return
            if key == "SSH":
                ip = ssh_ip.text().strip()
                if not ip:
                    QMessageBox.warning(self, "SSH", "Enter an IP address.")
                    return
                connect_result.clear()
                connect_result.update(
                    {
                        "mode": "dev_ssh",
                        "ip": ip,
                        "port": ssh_port.value(),
                        "user": ssh_user.text().strip() or "root",
                        "password": ssh_pwd.text(),
                        "model": m_sel,
                    }
                )
                dlg.accept()
                return
            QMessageBox.warning(self, "Connect", "Select a connection method.")

        dev_connect.clicked.connect(_attempt_dev_connect)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        mode = connect_result.get("mode")
        if mode == "dev_uart":
            self._production_connect_resume = None
            self._begin_connection_log_tab()
            self._worker.connect_uart.emit(
                connect_result["port"],
                connect_result["baud"],
                connect_result["model"],
                connect_result["mcu_p"],
                connect_result["mcu_br"],
            )
            return
        if mode == "dev_adb":
            self._production_connect_resume = None
            self._begin_connection_log_tab()
            self._worker.connect_adb.emit(
                connect_result["password"],
                connect_result["model"],
                connect_result["adb_serial"],
            )
            return
        if mode == "dev_ssh":
            self._production_connect_resume = None
            self._begin_connection_log_tab()
            self._worker.connect_ssh.emit(
                connect_result["ip"],
                connect_result["port"],
                connect_result["user"],
                connect_result["password"],
                connect_result["model"],
            )
            return
        if mode == "prod_adb":
            self._production_connect_resume = {"model": connect_result["model"]}
            self._begin_connection_log_tab()
            self._worker.connect_adb_production.emit(
                connect_result["model"],
                connect_result["adb_serial"],
            )
            return

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
