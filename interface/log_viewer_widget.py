"""Structured log viewer (Qt): table, filters, raw toggle, capped buffer. No device I/O."""
from __future__ import annotations

import os
from collections import deque
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer, QModelIndex, QSize
from PySide6.QtGui import QColor, QBrush, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
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

_LEVEL_BTN_STYLE = """
QPushButton { padding: 4px 10px; border-radius: 4px; border: 1px solid rgba(255,255,255,0.12);
  background: #252a32; color: #c5ced9; font-size: 11px; }
QPushButton:checked { background: #2d3d4d; border-color: #00897B; color: #e8eef5; font-weight: 600; }
QPushButton:hover { background: #2f3640; }
"""

_BADGE_CONNECTED = "background:#1a3d2a;color:#9ae6b4;padding:2px 8px;border-radius:4px;font-size:11px;"
_BADGE_DISCONNECTED = "background:#3d1a1a;color:#feb2b2;padding:2px 8px;border-radius:4px;font-size:11px;"
_BADGE_LIVE = "background:#1a2d3d;color:#90cdf4;padding:2px 8px;border-radius:4px;font-size:11px;"
_BADGE_STOPPED = "background:#2d2d2d;color:#a0aec0;padding:2px 8px;border-radius:4px;font-size:11px;"
_BADGE_CONNECTING = "background:#3d3a1a;color:#faf089;padding:2px 8px;border-radius:4px;font-size:11px;"


class _MessageDelegate(QStyledItemDelegate):
    """Paint message column with simple token colors (hex, numbers, strings, keys)."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        entry = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, dict):
            super().paint(painter, option, index)
            return
        if entry.get("kind") == "system":
            super().paint(painter, option, index)
            return

        painter.save()
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else None
        if style:
            style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, opt.widget)

        text = opt.text
        if entry.get("kind") == "json":
            text = json_display_message(entry)

        rect = opt.rect.adjusted(4, 2, -4, -2)
        painter.setClipRect(rect)
        default_pen = QPen(QColor("#c5ced9"))
        painter.setFont(opt.font)

        x = rect.left()
        y_base = rect.top() + QFontMetrics(opt.font).ascent() + 1
        for seg, color_hex in tokenize_message_for_paint(text):
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
        self._filter_level: str | None = None  # None = ALL
        self._filter_needle = ""
        self._show_raw = False
        self._device_connected: bool | None = None
        self._tail_streaming: bool = True

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        toolbar = QHBoxLayout()
        self._grp = QButtonGroup(self)
        self._grp.setExclusive(True)
        for label, val in (
            ("All", "ALL"),
            ("INFO", "I"),
            ("NOTICE", "N"),
            ("WARN", "W"),
            ("ERROR", "E"),
        ):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setStyleSheet(_LEVEL_BTN_STYLE)
            b.setProperty("filter_level", val)
            self._grp.addButton(b)
            toolbar.addWidget(b)
            if val == "ALL":
                b.setChecked(True)
        self._grp.buttonClicked.connect(self._on_level_button)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search…")
        self._search.setClearButtonEnabled(True)
        self._search.setMaximumWidth(280)
        self._search.textChanged.connect(self._on_search_changed)
        toolbar.addWidget(self._search, 1)

        self._raw_btn = QPushButton("Raw")
        self._raw_btn.setCheckable(True)
        self._raw_btn.setToolTip("Toggle plain-text view (same buffer)")
        self._raw_btn.toggled.connect(self._on_raw_toggled)
        self._raw_btn.setStyleSheet(_LEVEL_BTN_STYLE)
        toolbar.addWidget(self._raw_btn)

        self._save_btn: QPushButton | None = None
        if tail_mode:
            self._save_btn = QPushButton("Save log…")
            self._save_btn.setToolTip("Save buffered lines to a .txt file (UTF-8)")
            self._save_btn.setStyleSheet(_LEVEL_BTN_STYLE)
            self._save_btn.clicked.connect(self._on_save_log_clicked)
            toolbar.addWidget(self._save_btn)

        self._badge = QLabel()
        self._badge.setVisible(bool(show_transport_badge))
        toolbar.addWidget(self._badge)
        root.addLayout(toolbar)

        self._stack = QStackedWidget()
        self._table = QTableWidget(0, 1)
        self._table.setHorizontalHeaderLabels(["Message"])
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._table.setWordWrap(False)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setItemDelegateForColumn(0, _MessageDelegate(self._table))
        self._table.cellDoubleClicked.connect(self._on_table_cell_double_clicked)
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
        self._table.setRowCount(0)
        self._plain_raw.clear()
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

    def _after_entries_mutated(self) -> None:
        self._sync_raw_plain()
        if self._show_raw:
            self._footer_update()
        else:
            self._rebuild_structured_view()

    def _sync_raw_plain(self) -> None:
        self._plain_raw.setPlainText("\n".join((e.get("raw") or "") for e in self._entries))

    def _on_raw_toggled(self, on: bool) -> None:
        self._show_raw = on
        self._stack.setCurrentIndex(1 if on else 0)
        self._sync_raw_plain()
        if not on:
            self._rebuild_structured_view()
        self._footer_update()

    def _on_level_button(self, btn: QAbstractButton) -> None:
        val = str(btn.property("filter_level") or "ALL")
        self._filter_level = None if val == "ALL" else val
        self._rebuild_structured_view()

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
        return entry_matches_level(entry, self._filter_level) and entry_matches_search(
            entry, self._filter_needle
        )

    def _rebuild_structured_view(self) -> None:
        if self._show_raw:
            return
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
