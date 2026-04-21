"""FW Wizard Step 4 (single mode): select firmware build from search results."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from interface.app_styles import qcombobox_dark_stylesheet

FwSearchRow = tuple[str, str, int | None, str | None]

_ACCENT = "#00897B"
_OK = "#4caf7d"
_AMBER = "#c9a227"
_MUTED = "#8b95a5"


def _fw_qlabel_ss(declarations: str) -> str:
    d = declarations.strip()
    if not d.endswith(";"):
        d += ";"
    return f"QLabel {{ {d} }}"


def _fw_lineedit_ss() -> str:
    return (
        f"QLineEdit {{ background-color: #1a1f26; color: #e8eef4; "
        f"border: 1px solid rgba(255,255,255,0.10); border-radius: 6px; padding: 6px 10px; "
        f"font-size: 13px; selection-background-color: {_ACCENT}; }}"
    )


def _fw_combo_ss() -> str:
    return qcombobox_dark_stylesheet(
        border_radius=6,
        padding="5px 10px",
        min_height=22,
        dropdown_width=22,
        font_size="13px",
    )


def _fw_status_dot_qss(bg: str) -> str:
    return (
        f"QLabel {{ background-color: {bg}; border-radius: 4px; border: none; "
        "min-width: 8px; max-width: 8px; min-height: 8px; max-height: 8px; }"
    )


def _format_fw_bytes(n: int | None) -> str:
    if n is None or n < 0:
        return "—"
    for label, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {label}"
    return f"{n} B"


def _format_artifactory_ts(raw: str | None) -> str:
    if not raw:
        return "—"
    s = str(raw).strip()
    if s.isdigit():
        try:
            ms = int(s)
            return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            return s[:19]
    if "T" in s:
        return s.replace("T", " ")[:16]
    return s[:24]


def _elide(s: str, max_len: int) -> str:
    t = (s or "").strip()
    if len(t) <= max_len:
        return t
    if max_len <= 3:
        return "…"
    return t[: max_len - 1] + "…"


def _variant_key(filename: str) -> str:
    n = (filename or "").lower()
    if n.endswith(".xz"):
        return "xz"
    if n.endswith(".gz") or ".tar.gz" in n:
        return "gz"
    if "." in n:
        return n.rsplit(".", 1)[-1]
    return ""


class SelectVersion(QWidget):
    """Select one firmware row from Step 3 search results (client-side filter/sort)."""

    selection_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._all_rows: list[FwSearchRow] = []
        self._display: list[FwSearchRow] = []
        self._sort_col: int | None = None
        self._sort_asc: bool = True

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        title = QLabel("Select firmware build")
        title.setStyleSheet(_fw_qlabel_ss("font-size: 15px; font-weight: 500;"))
        lay.addWidget(title)
        sub = QLabel(
            "Choose one row. Version is the Artifactory folder path; Variant is the archive file name."
        )
        sub.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 12px;"))
        sub.setWordWrap(True)
        lay.addWidget(sub)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Search path or archive…")
        self._filter_edit.setStyleSheet(_fw_lineedit_ss())
        self._filter_edit.textChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self._filter_edit, 1)
        vf_lab = QLabel("Variant")
        vf_lab.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px; font-weight: 500;"))
        filter_row.addWidget(vf_lab)
        self._variant_combo = QComboBox()
        self._variant_combo.setStyleSheet(_fw_combo_ss())
        self._variant_combo.addItem("All variants", "all")
        self._variant_combo.addItem("gz", "gz")
        self._variant_combo.addItem("xz", "xz")
        self._variant_combo.currentIndexChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self._variant_combo)
        lay.addLayout(filter_row)

        self._table = QTableWidget(0, 6)
        self._table.setObjectName("fwWizardSelectVersionTable")
        self._table.setHorizontalHeaderLabels(
            ["#", "Version (path)", "Archive", "Size", "Date", "Variant"]
        )
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setSortingEnabled(False)
        self._table.setAlternatingRowColors(False)
        self._table.setShowGrid(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setFont(QFont("Consolas", 9) if os.name == "nt" else QFont("Menlo", 9))
        self._table.setStyleSheet(
            f"QTableWidget#fwWizardSelectVersionTable {{ background-color: #1a1f26; color: #e8eef4; "
            f"gridline-color: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.10); "
            f"border-radius: 6px; }}"
            f"QTableWidget#fwWizardSelectVersionTable::item:selected {{ "
            f"background-color: rgba(0, 137, 123, 0.22); border-left: 3px solid {_ACCENT}; }}"
            f"QTableWidget#fwWizardSelectVersionTable::item:hover {{ background-color: rgba(255,255,255,0.04); }}"
            f"QHeaderView::section {{ background-color: #161a20; color: {_MUTED}; padding: 6px 8px; "
            f"border: none; border-bottom: 1px solid rgba(255,255,255,0.10); font-weight: 500; }}"
        )
        self._table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self._table.itemSelectionChanged.connect(self._emit_selection)
        lay.addWidget(self._table, 1)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self._srv_dot = QLabel()
        self._srv_dot.setFixedSize(8, 8)
        self._srv_dot.setStyleSheet(_fw_status_dot_qss("#5c6570"))
        self._srv_text = QLabel("Firmware server off")
        self._srv_text.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px;"))
        self._srv_text.setWordWrap(True)
        foot.addWidget(self._srv_dot)
        foot.addWidget(self._srv_text, 1)
        lay.addLayout(foot)

    def sync_server_footer(self, hint: str, line: str, tooltip: str) -> None:
        if hint == "green":
            self._srv_dot.setStyleSheet(_fw_status_dot_qss(_OK))
        elif hint == "amber":
            self._srv_dot.setStyleSheet(_fw_status_dot_qss(_AMBER))
        else:
            self._srv_dot.setStyleSheet(_fw_status_dot_qss("#5c6570"))
        self._srv_text.setText(line or "Firmware server off")
        self._srv_text.setToolTip(tooltip)
        self._srv_dot.setToolTip(tooltip)

    def set_search_rows(self, rows: list[FwSearchRow]) -> None:
        self._all_rows = list(rows)
        self._filter_edit.blockSignals(True)
        self._filter_edit.clear()
        self._filter_edit.blockSignals(False)
        self._variant_combo.blockSignals(True)
        self._variant_combo.setCurrentIndex(0)
        self._variant_combo.blockSignals(False)
        self._sort_col = None
        self._sort_asc = True
        self._recompute_display()
        self._table.clearSelection()
        self.selection_changed.emit(None)

    def _on_filter_changed(self, *_args: Any) -> None:
        self._recompute_display()

    def _filtered_source(self) -> list[FwSearchRow]:
        q = (self._filter_edit.text() or "").strip().lower()
        vf = self._variant_combo.currentData()
        vf_s = str(vf) if vf else "all"
        out: list[FwSearchRow] = []
        for row in self._all_rows:
            folder, fn, _sz, _md = row
            if q and q not in folder.lower() and q not in fn.lower():
                continue
            vk = _variant_key(fn)
            if vf_s == "gz" and vk != "gz":
                continue
            if vf_s == "xz" and vk != "xz":
                continue
            out.append(row)
        return out

    def _recompute_display(self) -> None:
        self._display = self._filtered_source()
        self._apply_sort_to_display()
        self._rebuild_table()

    def _sort_key(self, row: FwSearchRow, col: int) -> Any:
        folder, fn, sz, md = row
        if col == 1:
            return folder.lower()
        if col == 2:
            return fn.lower()
        if col == 3:
            return sz if sz is not None else -1
        if col == 4:
            return (md or "").strip()
        return ""

    def _apply_sort_to_display(self) -> None:
        if self._sort_col is None:
            return
        rev = not self._sort_asc
        self._display.sort(key=lambda r: self._sort_key(r, self._sort_col), reverse=rev)

    def _on_header_clicked(self, logical_index: int) -> None:
        if logical_index not in (1, 2, 3, 4):
            return
        if self._sort_col == logical_index:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = logical_index
            self._sort_asc = True
        self._apply_sort_to_display()
        self._rebuild_table()

    def _update_header_labels(self) -> None:
        bases = ["#", "Version (path)", "Archive", "Size", "Date", "Variant"]
        labels: list[str] = []
        for i, b in enumerate(bases):
            if self._sort_col == i and i in (1, 2, 3, 4):
                labels.append(b + (" ▲" if self._sort_asc else " ▼"))
            else:
                labels.append(b)
        self._table.setHorizontalHeaderLabels(labels)

    def _variant_item(self, vk: str) -> QTableWidgetItem:
        it = QTableWidgetItem(vk or "—")
        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if vk == "gz":
            it.setBackground(QBrush(QColor(76, 175, 125, 55)))
            it.setForeground(QBrush(QColor("#c8e6c9")))
        elif vk == "xz":
            it.setBackground(QBrush(QColor(156, 39, 176, 55)))
            it.setForeground(QBrush(QColor("#e1bee7")))
        else:
            it.setForeground(QBrush(QColor(_MUTED)))
        return it

    def _rebuild_table(self) -> None:
        self._update_header_labels()
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        for i, (folder, fn, sz, md) in enumerate(self._display):
            r = self._table.rowCount()
            self._table.insertRow(r)
            ix = QTableWidgetItem(str(i + 1))
            ix.setFlags(ix.flags() & ~Qt.ItemFlag.ItemIsEditable)
            ix.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            self._table.setItem(r, 0, ix)
            p_disp = _elide(folder, 72)
            pi = QTableWidgetItem(p_disp)
            pi.setData(Qt.ItemDataRole.UserRole, (folder, fn))
            pi.setToolTip(folder)
            pi.setFlags(pi.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(r, 1, pi)
            ai = QTableWidgetItem(_elide(fn, 48))
            ai.setToolTip(fn)
            ai.setFlags(ai.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(r, 2, ai)
            sz_t = _format_fw_bytes(sz)
            si = QTableWidgetItem(sz_t)
            si.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
            si.setFlags(si.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(r, 3, si)
            dt = _format_artifactory_ts(md)
            di = QTableWidgetItem(dt)
            di.setFlags(di.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(r, 4, di)
            vk = _variant_key(fn)
            self._table.setItem(r, 5, self._variant_item(vk))
        self._table.blockSignals(False)

    def _emit_selection(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            self.selection_changed.emit(None)
            return
        r = rows[0].row()
        it = self._table.item(r, 1)
        if not it:
            self.selection_changed.emit(None)
            return
        data = it.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, (tuple, list)) or len(data) < 2:
            self.selection_changed.emit(None)
            return
        folder, fn = str(data[0]), str(data[1])
        sz_i: int | None = None
        md_s: str | None = None
        if r < len(self._display):
            _f, _n, sz_i, md_s = self._display[r]
        row_dict = {
            "path": folder,
            "archive": fn,
            "size": _format_fw_bytes(sz_i),
            "date": _format_artifactory_ts(md_s),
            "variant": _variant_key(fn) or "—",
        }
        self.selection_changed.emit(row_dict)
