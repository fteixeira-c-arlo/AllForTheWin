"""Structured log viewer (Qt): table, filters, raw toggle, capped buffer. No device I/O."""
from __future__ import annotations

import os
import re
from collections import deque
from datetime import datetime
from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QModelIndex, QSize
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPen,
    QShortcut,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollBar,
    QStackedWidget,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.user_paths import get_arlo_logs_dir
from interface.device_log_parser import (
    entry_matches_level,
    entry_matches_search,
    json_display_message,
    parse_device_log_line,
    tokenize_message_for_paint,
)

_MAX_ENTRIES = 2000
_SEARCH_DEBOUNCE_MS = 160

# Level tab counts / raw-line filters (embedded `user.*` in raw)
_RE_USER_INFO = re.compile(r"user\.info\b", re.I)
_RE_USER_NOTICE = re.compile(r"user\.notice\b", re.I)
_RE_USER_WARN = re.compile(r"user\.warn", re.I)
_RE_USER_ERR = re.compile(r"user\.err", re.I)

_TAB_KEYS = ("all", "info", "notice", "warn", "error")
_TAB_ACCENT: dict[str, str] = {
    "all": "#3db88a",
    "info": "#5b8fc9",
    "notice": "#3db88a",
    "warn": "#c9954a",
    "error": "#c95a5a",
}
_PILL_QSS = {
    "info": "QLabel { background-color: #1a2e44; color: #5b8fc9; border-radius: 10px; padding: 2px 8px; font-size: 11px; font-weight: 600; }",
    "warn": "QLabel { background-color: #2e2214; color: #c9954a; border-radius: 10px; padding: 2px 8px; font-size: 11px; font-weight: 600; }",
    "error": "QLabel { background-color: #2e1414; color: #c95a5a; border-radius: 10px; padding: 2px 8px; font-size: 11px; font-weight: 600; }",
}

_ROW2_ACTION_BTN_QSS = """
QPushButton {
  background-color: #252a32;
  color: #c5ced9;
  border: 1px solid rgba(255,255,255,0.12);
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 12px;
}
QPushButton:hover { background-color: #2f3640; }
QPushButton:checked { background-color: #2d3d4d; border-color: #00897B; color: #e8eef5; }
"""
# Tail auto-scroll: pause when distance-from-bottom (px) exceeds this (web scrollHeight - scrollTop - clientHeight).
_TAIL_SCROLL_PAUSE_DISTANCE_PX = 60

# Embedded camera / agw lines (delegate paint). Variant A: path …func() body; B: no func() body.
_ARLO_LINE_HEAD = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}\.\d+)\s+(\d+)\s+(user\.\w+)\s+(\S+:)\s*"
)
_ARLO_MODULE_TO_BODY = re.compile(r"^((?:[a-z]\:|[^:\s]+:)+[^\s]+\([^)]*\))\s+(.*)$")
_ARLO_BODY_LABEL = re.compile(
    r"\b(?:curr buffer|delta pts|fill_ms|egress_id|ingress_id|"
    r"size|ptype|seq|ssrc|pts|frames|pool|prop|psize|codec|fill|prev)\b",
    re.I,
)
_ARLO_BODY_0X = re.compile(r"0x[0-9a-fA-F]+", re.I)
_ARLO_BODY_NUM = re.compile(r"(?<=[\s,])(\d+(?:\.\d+)?)s?\b")
_ARLO_BODY_DASH = re.compile(r"(^|\s)(--)(\s|$)")
_ARLO_BODY_SSRC_HEX = re.compile(r"(?i)ssrc\s+([a-f0-9]{6,8})\b")
_ARLO_BODY_BARE_HEX_AFTER_DASH = re.compile(r"\b[a-f0-9]{6,8}\b")
# Standalone word or `mqtt_` / `cloud_` prefix inside filenames (underscore is not a \b boundary).
_RE_SUBSYS_MQTT = re.compile(r"\bmqtt\b|\bmqtt_(?=\w)", re.I)
_RE_SUBSYS_CLOUD = re.compile(r"\bcloud\b|\bcloud_(?=\w)", re.I)

_C_EMB_DATE = "#6b8cad"
_C_EMB_TIME = "#7eb3e8"
_C_EMB_TID = "#7a9ab8"
_C_EMB_PROC = "#a8b8d0"
_C_EMB_MODPATH = "#60a888"
_C_EMB_FUNC = "#80c8e8"
_C_EMB_DASH = "#506070"
_C_EMB_LABEL = "#a8b8d0"
_C_EMB_HEX = "#e8b060"
_C_EMB_NUM = "#90d8a8"
_C_EMB_MSG = "#c0c8d8"
_C_EMB_MQTT = "#c4a7f5"
_C_EMB_CLOUD = "#7ec8f0"


def _emb_level_color(level_tok: str) -> str:
    low = level_tok.lower()
    if low.startswith("user.info"):
        return "#79bfff"
    if low.startswith("user.debug"):
        return "#a0b8d0"
    if low.startswith("user.warn"):
        return "#e8b060"
    if low.startswith("user.err"):
        return "#e87070"
    return "#c0c8d8"


def _tokenize_arlo_embedded_log_line(s: str) -> list[tuple[str, str | None]]:
    """
    Per-segment colors for embedded log lines (variants A and B). Message-body rules
    (hex, numbers, labels, dashes) apply only in the message span on default-colored
    positions. The module path is otherwise left as modpath/func paint only, except
    for a narrow set of subsystem tokens (mqtt, cloud) which may override within path
    or message for readability.
    """
    if not s:
        return []
    m = _ARLO_LINE_HEAD.match(s)
    if not m:
        return tokenize_message_for_paint(s)

    n = len(s)
    date, time_s, tid, level_tok, proc = m.groups()
    body0 = m.end()
    body = s[body0:]

    mod = ""
    mm = _ARLO_MODULE_TO_BODY.match(body)
    msg_off = body0
    mod_lo: int | None = None
    mod_hi: int | None = None

    col = [_C_EMB_MSG] * n

    def fill(a: int, b: int, c: str) -> None:
        for i in range(max(0, a), min(b, n)):
            col[i] = c

    fill(m.start(1), m.end(1), _C_EMB_DATE)
    fill(m.start(2), m.end(2), _C_EMB_TIME)
    fill(m.start(3), m.end(3), _C_EMB_TID)
    fill(m.start(4), m.end(4), _emb_level_color(level_tok))
    fill(m.start(5), m.end(5), _C_EMB_PROC)

    if mm:
        mod = mm.group(1)
        msg_off = body0 + mm.start(2)
        mod_lo = body0 + mm.start(1)
        mod_hi = body0 + mm.end(1)
        mf = re.search(r"([\w.]+)\(([^)]*)\)$", mod)
        if mf:
            fn_lo = mod_lo + mf.start(1)
            fill(mod_lo, fn_lo, _C_EMB_MODPATH)
            fill(fn_lo, mod_hi, _C_EMB_FUNC)
        else:
            fill(mod_lo, mod_hi, _C_EMB_MODPATH)

    msg_hi = n
    msg_lo = msg_off

    def body_default(i: int) -> bool:
        return msg_lo <= i < msg_hi and col[i] == _C_EMB_MSG

    def claim(a: int, b: int, c: str) -> None:
        for i in range(max(msg_lo, a), min(msg_hi, b)):
            if body_default(i):
                col[i] = c

    sub = s[msg_lo:msg_hi]

    # (a) 0x hex — message body only
    for m0 in _ARLO_BODY_0X.finditer(sub):
        claim(msg_lo + m0.start(), msg_lo + m0.end(), _C_EMB_HEX)

    # (b) 6–8 char lowercase hex: after `--`, or ssrc value
    dash = sub.find("--")
    if dash >= 0:
        from_i = msg_lo + dash + 2
        for mh in _ARLO_BODY_BARE_HEX_AFTER_DASH.finditer(sub[dash + 2 :]):
            piece = mh.group(0)
            if len(piece) < 6 or len(piece) > 8:
                continue
            if not all(c in "0123456789abcdef" for c in piece):
                continue
            a, b = from_i + mh.start(), from_i + mh.end()
            claim(a, b, _C_EMB_HEX)
    for ms in _ARLO_BODY_SSRC_HEX.finditer(sub):
        a = msg_lo + ms.start(1)
        b = msg_lo + ms.end(1)
        claim(a, b, _C_EMB_HEX)

    # (c) label keywords
    for ml in _ARLO_BODY_LABEL.finditer(sub):
        claim(msg_lo + ml.start(), msg_lo + ml.end(), _C_EMB_LABEL)

    # (d) numeric values (still default-colored positions only)
    for mn in _ARLO_BODY_NUM.finditer(sub):
        a = msg_lo + mn.start(1)
        b = msg_lo + mn.end(1)
        claim(a, b, _C_EMB_NUM)

    # (e) `--` separator (override punctuation)
    for md in _ARLO_BODY_DASH.finditer(sub):
        a = msg_lo + md.start(2)
        b = msg_lo + md.end(2)
        for i in range(a, b):
            if msg_lo <= i < msg_hi:
                col[i] = _C_EMB_DASH

    def paint_subsystem_kw(a: int, b: int, color: str) -> None:
        """Highlight mqtt/cloud inside module path or message (after body rules)."""
        for i in range(max(0, a), min(n, b)):
            if mod_lo is not None and mod_lo <= i < mod_hi:
                col[i] = color
            elif msg_lo <= i < msg_hi:
                col[i] = color

    for mk in _RE_SUBSYS_MQTT.finditer(s):
        if mk.start() < body0:
            continue
        paint_subsystem_kw(mk.start(), mk.end(), _C_EMB_MQTT)
    for ck in _RE_SUBSYS_CLOUD.finditer(s):
        if ck.start() < body0:
            continue
        paint_subsystem_kw(ck.start(), ck.end(), _C_EMB_CLOUD)

    out: list[tuple[str, str | None]] = []
    i = 0
    while i < n:
        c = col[i]
        j = i + 1
        while j < n and col[j] == c:
            j += 1
        out.append((s[i:j], c))
        i = j
    return out


_BADGE_CONNECTED = "background:#1a3d2a;color:#9ae6b4;padding:2px 8px;border-radius:4px;font-size:11px;"
_BADGE_DISCONNECTED = "background:#3d1a1a;color:#feb2b2;padding:2px 8px;border-radius:4px;font-size:11px;"
_BADGE_LIVE = "background:#1a2d3d;color:#90cdf4;padding:2px 8px;border-radius:4px;font-size:11px;"
_BADGE_STOPPED = "background:#2d2d2d;color:#a0aec0;padding:2px 8px;border-radius:4px;font-size:11px;"
_BADGE_CONNECTING = "background:#3d3a1a;color:#faf089;padding:2px 8px;border-radius:4px;font-size:11px;"

# Row highlight (multi-select): translucent so token colors stay visible.
_ROW_SEL_FILL = QColor(42, 90, 138, 64)  # rgba(42, 90, 138, 0.25)
_ROW_SEL_BORDER = QColor("#2a7abf")


class _MessageDelegate(QStyledItemDelegate):
    """Paint message column with simple token colors (hex, numbers, strings, keys)."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        entry = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, dict):
            super().paint(painter, option, index)
            return

        opt0 = QStyleOptionViewItem(option)
        self.initStyleOption(opt0, index)
        row_sel = bool(opt0.state & QStyle.StateFlag.State_Selected)

        if entry.get("kind") == "system":
            if row_sel:
                painter.save()
                opt = opt0
                r = opt.rect
                painter.fillRect(r, _ROW_SEL_FILL)
                painter.fillRect(r.left(), r.top(), 2, r.height(), _ROW_SEL_BORDER)
                text = opt.text
                rect = r.adjusted(12, 2, -4, -2)
                painter.setClipRect(r)
                painter.setPen(QColor("#feb2b2"))
                f = QFont(opt.font)
                f.setBold(True)
                painter.setFont(f)
                fm = QFontMetrics(f)
                y_base = rect.top() + fm.ascent() + 1
                painter.drawText(rect.left(), y_base, text)
                painter.restore()
            else:
                super().paint(painter, option, index)
            return

        painter.save()
        opt = opt0
        style = opt.widget.style() if opt.widget else None
        if row_sel:
            r = opt.rect
            painter.fillRect(r, _ROW_SEL_FILL)
            painter.fillRect(r.left(), r.top(), 2, r.height(), _ROW_SEL_BORDER)
        elif style:
            style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, opt.widget)

        text = opt.text
        if entry.get("kind") == "json":
            text = json_display_message(entry)

        if _ARLO_LINE_HEAD.match(text):
            segments = _tokenize_arlo_embedded_log_line(text)
        else:
            segments = tokenize_message_for_paint(text)

        left_pad = 12 if row_sel else 4
        rect = opt.rect.adjusted(left_pad, 2, -4, -2)
        painter.setClipRect(opt.rect)
        default_pen = QPen(QColor("#c0c8d8"))
        painter.setFont(opt.font)

        x = rect.left()
        y_base = rect.top() + QFontMetrics(opt.font).ascent() + 1
        for seg, color_hex in segments:
            if color_hex:
                painter.setPen(QColor(color_hex))
            else:
                painter.setPen(default_pen)
            w = QFontMetrics(opt.font).horizontalAdvance(seg)
            if x + w > rect.right():
                # single-line clip: elide by skipping overflow paint
                painter.drawText(x, y_base, seg)
                break
            painter.drawText(x, y_base, seg)
            x += w
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        sh = super().sizeHint(option, index)
        return QSize(sh.width(), max(sh.height(), 22))


class LogViewerWidget(QWidget):
    """
    Session / tail log surface: structured table, filters, optional raw plain view.
    Append via append_plain (line-buffered); flush_partial_line() at end of stream.
    """

    def __init__(
        self,
        *,
        show_transport_badge: bool = True,
        tail_mode: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tail_mode = tail_mode
        self._entries: deque[dict[str, Any]] = deque(maxlen=_MAX_ENTRIES)
        self._line_buf = ""
        self._filter_tab: str = "all"  # all | info | notice | warn | error
        self._filter_needle = ""
        self._show_raw = False
        self._device_connected: bool | None = None
        self._tail_streaming: bool = True
        self._tail_autoscroll_paused: bool = False
        self._tail_scroll_programmatic: bool = False
        self._tail_mute_scroll_pause_updates: bool = False
        self._tail_structured_entry_len: int = 0
        self._tail_structured_last_mod: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        self._tab_buttons: dict[str, QPushButton] = {}
        self._tab_pills: dict[str, QLabel] = {}
        self._pill_to_tab_key: dict[QObject, str] = {}

        toolbar_block = QWidget()
        tb_outer = QVBoxLayout(toolbar_block)
        tb_outer.setContentsMargins(0, 0, 0, 0)
        tb_outer.setSpacing(0)

        row1 = QWidget()
        row1.setStyleSheet("background-color: #13151c; border-bottom: 1px solid #252830;")
        row1_lay = QHBoxLayout(row1)
        row1_lay.setContentsMargins(10, 4, 10, 4)
        row1_lay.setSpacing(4)

        tab_labels = {
            "all": "All",
            "info": "Info",
            "notice": "Notice",
            "warn": "Warn",
            "error": "Error",
        }
        for key in _TAB_KEYS:
            cell = QWidget()
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(6)
            btn = QPushButton(tab_labels[key])
            btn.setFlat(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda *_a, k=key: self._on_filter_tab_clicked(k))
            self._tab_buttons[key] = btn
            cl.addWidget(btn)
            pill = QLabel("")
            pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pill.setFixedHeight(20)
            pill.hide()
            if key == "notice":
                pill.setStyleSheet(
                    "QLabel { background-color: #1a2430; color: #3db88a; border-radius: 10px; "
                    "padding: 2px 8px; font-size: 11px; font-weight: 600; }"
                )
            elif key in _PILL_QSS:
                pill.setStyleSheet(_PILL_QSS[key])
            else:
                pill.setStyleSheet("")
            pill.setCursor(Qt.CursorShape.PointingHandCursor)
            pill.installEventFilter(self)
            self._pill_to_tab_key[pill] = key
            self._tab_pills[key] = pill
            cl.addWidget(pill)
            row1_lay.addWidget(cell)

        row1_lay.addStretch(1)
        tb_outer.addWidget(row1)

        row2 = QWidget()
        row2.setObjectName("logToolbarRow2")
        row2.setStyleSheet(
            "QWidget#logToolbarRow2 { background-color: #13151c; "
            "border-left: 1px solid #252830; border-right: 1px solid #252830; "
            "border-bottom: 1px solid #252830; "
            "border-bottom-left-radius: 8px; border-bottom-right-radius: 8px; }"
        )
        row2_lay = QHBoxLayout(row2)
        row2_lay.setContentsMargins(10, 8, 10, 10)
        row2_lay.setSpacing(10)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search logs…" if tail_mode else "Search…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search_changed)
        self._search.setStyleSheet(
            "QLineEdit { background-color: #1a1f26; color: #e8eef4; border: 1px solid #2a2d3a; "
            "border-radius: 6px; padding: 6px 10px; font-size: 12px; }"
        )
        row2_lay.addWidget(self._search, 1)

        self._raw_btn = QPushButton("Raw")
        self._raw_btn.setCheckable(True)
        self._raw_btn.setToolTip("Toggle plain-text view (same buffer)")
        self._raw_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._raw_btn.toggled.connect(self._on_raw_toggled)
        self._raw_btn.setStyleSheet(_ROW2_ACTION_BTN_QSS)
        row2_lay.addWidget(self._raw_btn, 0)

        self._copy_selected_btn = QPushButton("Copy selected")
        self._copy_selected_btn.setToolTip(
            "Table: copy selected rows (Shift+click or drag for multiple). Raw: copy selected text. Esc clears table selection."
        )
        self._copy_selected_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_selected_btn.setEnabled(False)
        self._copy_selected_btn.setStyleSheet(_ROW2_ACTION_BTN_QSS)
        self._copy_selected_btn.clicked.connect(self._on_copy_selected_clicked)
        row2_lay.addWidget(self._copy_selected_btn, 0)

        self._save_btn: QPushButton | None = None
        if tail_mode:
            self._save_btn = QPushButton("Save log…")
            self._save_btn.setToolTip("Save buffered lines to a .txt file (UTF-8)")
            self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._save_btn.setStyleSheet(_ROW2_ACTION_BTN_QSS)
            self._save_btn.clicked.connect(self._on_save_log_clicked)
            row2_lay.addWidget(self._save_btn, 0)

        self._badge = QLabel()
        self._badge.setVisible(bool(show_transport_badge))
        row2_lay.addWidget(self._badge, 0)

        tb_outer.addWidget(row2)
        root.addWidget(toolbar_block)

        self._sync_log_tab_styles()
        self._update_level_tab_counts()

        self._stack = QStackedWidget()
        self._table = QTableWidget(0, 1)
        self._table.setHorizontalHeaderLabels(["Message"])
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._table.setWordWrap(False)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setItemDelegateForColumn(0, _MessageDelegate(self._table))
        self._table.viewport().installEventFilter(self)
        self._table.cellDoubleClicked.connect(self._on_table_cell_double_clicked)
        _sm = self._table.selectionModel()
        if _sm is not None:
            _sm.selectionChanged.connect(lambda *_a: self._sync_copy_selected_button())
        _esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self._table)
        _esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        _esc.activated.connect(self._table.clearSelection)
        self._table.setStyleSheet(
            "QTableWidget { background: #161a20; color: #c5ced9; gridline-color: transparent; "
            "border: 1px solid rgba(255,255,255,0.08); border-radius: 4px; }"
            "QHeaderView::section { background: #1e242d; color: #8b95a5; padding: 4px; "
            "border: none; border-bottom: 1px solid rgba(255,255,255,0.1); font-size: 11px; }"
        )
        self._plain_raw = QPlainTextEdit()
        self._plain_raw.setReadOnly(True)
        self._plain_raw.setFont(QFont("Consolas", 10))
        self._plain_raw.setStyleSheet(
            "QPlainTextEdit { background: #161a20; color: #c5ced9; border: 1px solid rgba(255,255,255,0.08); }"
        )
        if hasattr(self._plain_raw, "copyAvailable"):
            self._plain_raw.copyAvailable.connect(lambda _c: self._sync_copy_selected_button())
        self._plain_raw.cursorPositionChanged.connect(self._sync_copy_selected_button)
        self._stack.addWidget(self._table)
        self._stack.addWidget(self._plain_raw)
        root.addWidget(self._stack, 1)

        self._footer = QLabel("Visible: 0  ·  Buffered: 0")
        self._footer.setStyleSheet("color: #8b95a5; font-size: 11px; padding: 2px 4px;")
        root.addWidget(self._footer)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._rebuild_structured_view)

        self._footer_restore_timer = QTimer(self)
        self._footer_restore_timer.setSingleShot(True)
        self._footer_restore_timer.timeout.connect(self._footer_update)

        if tail_mode:
            self._table.verticalScrollBar().valueChanged.connect(
                lambda v: self._on_tail_scrollbar_user_moved(self._table.verticalScrollBar(), v)
            )
            self._plain_raw.verticalScrollBar().valueChanged.connect(
                lambda v: self._on_tail_scrollbar_user_moved(self._plain_raw.verticalScrollBar(), v)
            )

        self._sync_badge()
        self._footer_update()

    # --- Public API matching prior QTextEdit call sites ---

    def append(self, text: str) -> None:
        self.append_plain(text)

    def append_plain(self, text: str) -> None:
        self._line_buf += text
        parts = self._line_buf.split("\n")
        self._line_buf = parts[-1]
        changed = False
        for line in parts[:-1]:
            self._entries.append(parse_device_log_line(line))
            changed = True
        if changed:
            self._after_entries_mutated()

    def flush_partial_line(self) -> None:
        """Append any buffered tail fragment as one logical line (end of file / stream)."""
        if self._line_buf:
            self._entries.append(parse_device_log_line(self._line_buf))
            self._line_buf = ""
            self._after_entries_mutated()

    def clear(self) -> None:
        self._line_buf = ""
        self._entries.clear()
        self._tail_structured_entry_len = 0
        self._tail_structured_last_mod = None
        self._table.setRowCount(0)
        self._plain_raw.clear()
        if self._tail_mode:
            self._tail_autoscroll_paused = False
        self._update_level_tab_counts()
        self._sync_copy_selected_button()
        self._footer_update()

    def setPlainText(self, text: str) -> None:
        """Replace buffer (QTextEdit-compatible for session reopen messages)."""
        self.clear()
        t = text or ""
        if t and not t.endswith("\n"):
            t += "\n"
        self.append_plain(t)

    def toPlainText(self) -> str:
        return "\n".join((e.get("raw") or "") for e in self._entries)

    def _tail_export_text(self) -> str:
        """Full session text for export (completed lines + raw + any buffered tail fragment)."""
        lines = [(e.get("raw") or "") for e in self._entries]
        body = "\n".join(lines)
        if self._line_buf:
            body = body + ("\n" if body else "") + self._line_buf
        return body

    def _on_save_log_clicked(self) -> None:
        if not self._tail_mode:
            return
        text = self._tail_export_text()
        if not text.strip():
            QMessageBox.information(self, "Save log", "Nothing to save yet — no lines in this tail session.")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"tail_log_{stamp}.txt"
        start_dir = get_arlo_logs_dir()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save tail log",
            os.path.join(start_dir, default_name),
            "Text files (*.txt);;All files (*.*)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                if not text.endswith("\n"):
                    f.write("\n")
        except OSError as e:
            QMessageBox.warning(self, "Save log", f"Could not save file:\n{e}")
            return
        self._footer.setText(f"Saved: {path}")
        self._footer_restore_timer.start(5000)

    def set_device_connected(self, connected: bool) -> None:
        self._device_connected = connected
        if not self._tail_mode:
            self._sync_badge()

    def apply_main_window_connection(self, *, phase: str, device_connected: bool) -> None:
        """Session / welcome tabs: mirror MainWindow status (phase: connecting|connected|…)."""
        if self._tail_mode:
            return
        ph = (phase or "").strip().lower()
        self._device_connected = device_connected
        if not self._badge.isVisible():
            return
        if ph == "connecting":
            self._badge.setText(" Connecting… ")
            self._badge.setStyleSheet(_BADGE_CONNECTING)
            return
        if ph == "connected" and device_connected:
            self._badge.setText(" Connected ")
            self._badge.setStyleSheet(_BADGE_CONNECTED)
            return
        self._badge.setText(" Disconnected ")
        self._badge.setStyleSheet(_BADGE_DISCONNECTED)

    def set_tail_streaming(self, active: bool) -> None:
        self._tail_streaming = active
        if self._tail_mode:
            self._sync_badge()

    def set_badge_visible(self, visible: bool) -> None:
        self._badge.setVisible(visible)
        if not visible:
            self._badge.clear()

    def is_tail_mode(self) -> bool:
        return self._tail_mode

    # --- Internals ---

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # _table is created after filter pills; layout/show can deliver events during __init__.
        _table = getattr(self, "_table", None)
        if (
            self._tail_mode
            and _table is not None
            and watched is _table.viewport()
            and event.type() == QEvent.Type.Wheel
        ):
            we = event
            if isinstance(we, QWheelEvent) and we.angleDelta().y() > 0:
                self._tail_autoscroll_paused = True
        if event.type() == QEvent.Type.MouseButtonRelease:
            me = event
            if isinstance(me, QMouseEvent) and me.button() == Qt.MouseButton.LeftButton:
                tab_key = self._pill_to_tab_key.get(watched)
                if tab_key is not None:
                    self._on_filter_tab_clicked(tab_key)
                    return True
        return super().eventFilter(watched, event)

    def _qss_log_tab_button(self, key: str, active: bool) -> str:
        acc = _TAB_ACCENT.get(key, "#3db88a")
        if active:
            return (
                "QPushButton { border: none; border-bottom: 2px solid "
                + acc
                + "; background: transparent; color: "
                + acc
                + "; font-size: 12px; font-weight: 600; padding: 8px 4px 6px 4px; }"
            )
        return (
            "QPushButton { border: none; border-bottom: 2px solid transparent; "
            "background: transparent; color: #8b95a5; font-size: 12px; font-weight: 500; "
            "padding: 8px 4px 6px 4px; }"
            "QPushButton:hover { color: #aeb8c4; }"
        )

    def _sync_log_tab_styles(self) -> None:
        for key, btn in self._tab_buttons.items():
            btn.setStyleSheet(self._qss_log_tab_button(key, self._filter_tab == key))

    def _on_filter_tab_clicked(self, key: str) -> None:
        self._filter_tab = key
        self._sync_log_tab_styles()
        self._rebuild_structured_view()

    def _update_level_tab_counts(self) -> None:
        n_info = n_notice = n_warn = n_error = 0
        for e in self._entries:
            raw = e.get("raw") or ""
            if _RE_USER_INFO.search(raw):
                n_info += 1
            if _RE_USER_NOTICE.search(raw):
                n_notice += 1
            if _RE_USER_WARN.search(raw):
                n_warn += 1
            if _RE_USER_ERR.search(raw):
                n_error += 1
        counts = {"info": n_info, "notice": n_notice, "warn": n_warn, "error": n_error}
        for key, n in counts.items():
            pill = self._tab_pills.get(key)
            if pill is None:
                continue
            if n > 0:
                pill.setText(str(n))
                pill.show()
            else:
                pill.clear()
                pill.hide()

    def _entry_matches_filter_tab(self, entry: dict[str, Any]) -> bool:
        tab = self._filter_tab
        if tab == "all":
            return True
        k = entry.get("kind")
        if k in ("json", "system"):
            return True
        raw = entry.get("raw") or ""
        if tab == "info":
            return entry_matches_level(entry, "I") or bool(_RE_USER_INFO.search(raw))
        if tab == "notice":
            return entry_matches_level(entry, "N") or bool(_RE_USER_NOTICE.search(raw))
        if tab == "warn":
            return entry_matches_level(entry, "W") or bool(_RE_USER_WARN.search(raw))
        if tab == "error":
            return entry_matches_level(entry, "E") or bool(_RE_USER_ERR.search(raw))
        return True

    def _after_entries_mutated(self) -> None:
        self._update_level_tab_counts()
        self._sync_raw_plain()
        if self._show_raw:
            self._footer_update()
        elif (
            self._tail_mode
            and self._filter_tab == "all"
            and not (self._filter_needle or "").strip()
            and len(self._entries) > self._tail_structured_entry_len
        ):
            self._tail_mute_scroll_pause_updates = True
            try:
                self._append_structured_rows_for_range(
                    self._tail_structured_entry_len, len(self._entries)
                )
            finally:
                self._tail_mute_scroll_pause_updates = False
            self._tail_structured_entry_len = len(self._entries)
            self._footer.setText(
                f"Visible: {self._table.rowCount()}  ·  Buffered: {len(self._entries)}"
            )
            self._sync_copy_selected_button()
            # Scroll before sync: after insertRow, max grows while value is stale; syncing first
            # falsely sets _tail_autoscroll_paused and skips follow-scroll.
            if not self._tail_autoscroll_paused:
                self._tail_scroll_table_to_bottom()
            self._tail_sync_autoscroll_paused_from_scrollbar()
        else:
            self._rebuild_structured_view()

    def _sync_raw_plain(self) -> None:
        if self._tail_mode:
            self._tail_mute_scroll_pause_updates = True
        try:
            self._plain_raw.setPlainText("\n".join((e.get("raw") or "") for e in self._entries))
        finally:
            if self._tail_mode:
                self._tail_mute_scroll_pause_updates = False
        if self._tail_mode:
            if self._show_raw and not self._tail_autoscroll_paused:
                self._tail_scroll_plain_to_bottom()
            self._tail_sync_autoscroll_paused_from_scrollbar()

    def _on_raw_toggled(self, on: bool) -> None:
        self._show_raw = on
        self._stack.setCurrentIndex(1 if on else 0)
        self._sync_raw_plain()
        if not on:
            self._rebuild_structured_view()
        if self._tail_mode and not self._tail_autoscroll_paused:
            if self._show_raw:
                self._tail_scroll_plain_to_bottom()
            else:
                self._tail_scroll_table_to_bottom()
        self._sync_copy_selected_button()
        self._footer_update()

    def _on_search_changed(self, t: str) -> None:
        self._filter_needle = (t or "").strip()
        self._debounce.start(_SEARCH_DEBOUNCE_MS)

    def _sync_badge(self) -> None:
        if not self._badge.isVisible():
            return
        if self._tail_mode:
            if self._tail_streaming:
                self._badge.setText(" Live ")
                self._badge.setStyleSheet(_BADGE_LIVE)
            else:
                self._badge.setText(" Stopped ")
                self._badge.setStyleSheet(_BADGE_STOPPED)
            return
        c = self._device_connected
        if c is True:
            self._badge.setText(" Connected ")
            self._badge.setStyleSheet(_BADGE_CONNECTED)
        elif c is False:
            self._badge.setText(" Disconnected ")
            self._badge.setStyleSheet(_BADGE_DISCONNECTED)
        else:
            self._badge.setText(" — ")
            self._badge.setStyleSheet("color:#8b95a5;padding:2px 8px;font-size:11px;")

    def _row_visible(self, entry: dict[str, Any]) -> bool:
        return self._entry_matches_filter_tab(entry) and entry_matches_search(
            entry, self._filter_needle
        )

    def _append_structured_rows_for_range(self, i0: int, i1: int) -> None:
        """Append visible rows for entries[i0:i1] without clearing the table (tail + all + no search)."""
        last_mod = self._tail_structured_last_mod
        for idx in range(i0, i1):
            entry = self._entries[idx]
            if not self._row_visible(entry):
                continue
            k = entry.get("kind")
            if k == "system":
                r = self._table.rowCount()
                self._table.insertRow(r)
                it = QTableWidgetItem(entry.get("message") or entry.get("raw") or "")
                it.setData(Qt.ItemDataRole.UserRole, entry)
                it.setForeground(QBrush(QColor("#feb2b2")))
                f = it.font()
                f.setBold(True)
                it.setFont(f)
                it.setBackground(QBrush(QColor("#2d1a1a")))
                self._table.setItem(r, 0, it)
                continue

            if k == "json":
                r = self._table.rowCount()
                self._table.insertRow(r)
                self._set_single_message_cell(r, entry, is_json=True)
                continue

            mod = (entry.get("module") or "").strip()
            if mod and mod != last_mod:
                r = self._table.rowCount()
                self._table.insertRow(r)
                div = QTableWidgetItem(f" — {mod} — ")
                div.setForeground(QBrush(QColor("#718096")))
                div.setBackground(QBrush(QColor("#1e242d")))
                f = div.font()
                f.setItalic(True)
                div.setFont(f)
                div.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self._table.setItem(r, 0, div)
                last_mod = mod
            elif mod:
                last_mod = mod

            r = self._table.rowCount()
            self._table.insertRow(r)
            self._set_single_message_cell(r, entry, is_json=False)
        self._tail_structured_last_mod = last_mod

    def _rebuild_structured_view(self) -> None:
        if self._show_raw:
            return
        if self._tail_mode:
            self._tail_mute_scroll_pause_updates = True
        try:
            self._table.setRowCount(0)
            last_mod: str | None = None
            visible = 0
            for entry in self._entries:
                if not self._row_visible(entry):
                    continue
                k = entry.get("kind")
                if k == "system":
                    r = self._table.rowCount()
                    self._table.insertRow(r)
                    it = QTableWidgetItem(entry.get("message") or entry.get("raw") or "")
                    it.setData(Qt.ItemDataRole.UserRole, entry)
                    it.setForeground(QBrush(QColor("#feb2b2")))
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                    it.setBackground(QBrush(QColor("#2d1a1a")))
                    self._table.setItem(r, 0, it)
                    visible += 1
                    continue

                if k == "json":
                    r = self._table.rowCount()
                    self._table.insertRow(r)
                    self._set_single_message_cell(r, entry, is_json=True)
                    visible += 1
                    continue

                mod = (entry.get("module") or "").strip()
                if mod and mod != last_mod:
                    r = self._table.rowCount()
                    self._table.insertRow(r)
                    div = QTableWidgetItem(f" — {mod} — ")
                    div.setForeground(QBrush(QColor("#718096")))
                    div.setBackground(QBrush(QColor("#1e242d")))
                    f = div.font()
                    f.setItalic(True)
                    div.setFont(f)
                    div.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    self._table.setItem(r, 0, div)
                    last_mod = mod
                elif mod:
                    last_mod = mod

                r = self._table.rowCount()
                self._table.insertRow(r)
                self._set_single_message_cell(r, entry, is_json=False)
                visible += 1

            self._footer.setText(f"Visible: {visible}  ·  Buffered: {len(self._entries)}")
            self._sync_copy_selected_button()
            self._tail_structured_entry_len = len(self._entries)
            self._tail_structured_last_mod = last_mod
            if self._tail_mode and not self._show_raw and not self._tail_autoscroll_paused:
                self._tail_scroll_table_to_bottom()
        finally:
            if self._tail_mode:
                self._tail_mute_scroll_pause_updates = False
        if self._tail_mode:
            self._tail_sync_autoscroll_paused_from_scrollbar()

    def _tail_sync_autoscroll_paused_from_scrollbar(self) -> None:
        """Recompute pause flag after content/layout (valueChanged can be skipped while mute is on)."""
        if not self._tail_mode:
            return
        if self._show_raw:
            sb = self._plain_raw.verticalScrollBar()
        else:
            sb = self._table.verticalScrollBar()
        d = sb.maximum() - sb.value()
        self._tail_autoscroll_paused = d > _TAIL_SCROLL_PAUSE_DISTANCE_PX

    def _on_tail_scrollbar_user_moved(self, bar: QScrollBar, _value: int) -> None:
        if not self._tail_mode or self._tail_scroll_programmatic:
            return
        if self._tail_mute_scroll_pause_updates:
            return
        # Hidden view still gets document updates; ignore its scrollbar so we don't false-pause.
        if bar is self._table.verticalScrollBar() and self._show_raw:
            return
        if bar is self._plain_raw.verticalScrollBar() and not self._show_raw:
            return
        max_v = bar.maximum()
        value = bar.value()
        distance_from_bottom = max_v - value
        self._tail_autoscroll_paused = (
            distance_from_bottom > _TAIL_SCROLL_PAUSE_DISTANCE_PX
        )

    def _tail_scroll_table_to_bottom(self) -> None:
        """Snap to bottom; repeat next tick so range/layout catches new rows (QTableView lazy max)."""

        def _snap() -> None:
            sb = self._table.verticalScrollBar()
            self._tail_scroll_programmatic = True
            sb.setValue(sb.maximum())
            self._tail_scroll_programmatic = False

        _snap()
        QTimer.singleShot(0, _snap)

    def _tail_scroll_plain_to_bottom(self) -> None:
        def _snap() -> None:
            sb = self._plain_raw.verticalScrollBar()
            self._tail_scroll_programmatic = True
            sb.setValue(sb.maximum())
            self._tail_scroll_programmatic = False

        _snap()
        QTimer.singleShot(0, _snap)

    def _set_single_message_cell(self, r: int, entry: dict[str, Any], *, is_json: bool) -> None:
        """One column: full raw line for normal logs; JSON rows keep collapse/expand display text."""
        if is_json:
            txt = json_display_message(entry)
        else:
            txt = entry.get("raw") or entry.get("message") or ""
        it = QTableWidgetItem(txt)
        it.setData(Qt.ItemDataRole.UserRole, entry)
        it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self._table.setItem(r, 0, it)

    def _sync_copy_selected_button(self) -> None:
        if self._show_raw:
            self._copy_selected_btn.setEnabled(self._plain_raw.textCursor().hasSelection())
        else:
            sm = self._table.selectionModel()
            self._copy_selected_btn.setEnabled(
                bool(sm and sm.hasSelection())
            )

    def _on_copy_selected_clicked(self) -> None:
        if self._show_raw:
            cur = self._plain_raw.textCursor()
            if cur.hasSelection():
                QGuiApplication.clipboard().setText(cur.selectedText())
            return
        sm = self._table.selectionModel()
        if sm is None or not sm.hasSelection():
            return
        rows = sorted({ix.row() for ix in self._table.selectedIndexes()})
        lines: list[str] = []
        for r in rows:
            it = self._table.item(r, 0)
            if it is None:
                continue
            e = it.data(Qt.ItemDataRole.UserRole)
            if isinstance(e, dict):
                lines.append(str(e.get("raw") or ""))
            else:
                lines.append(it.text())
        if not lines:
            return
        QGuiApplication.clipboard().setText("\n".join(lines))

    def _footer_update(self) -> None:
        if self._show_raw:
            doc_lines = self._plain_raw.document().blockCount()
            self._footer.setText(f"Lines (raw view): {doc_lines}  ·  Buffered: {len(self._entries)}")
        else:
            vis = self._table.rowCount()
            self._footer.setText(f"Visible: {vis}  ·  Buffered: {len(self._entries)}")

    def _on_table_cell_double_clicked(self, row: int, _col: int) -> None:
        it = self._table.item(row, 0)
        if it is None:
            return
        e = it.data(Qt.ItemDataRole.UserRole)
        if isinstance(e, dict) and e.get("kind") == "json":
            e["json_expanded"] = not bool(e.get("json_expanded"))
            self._rebuild_structured_view()
