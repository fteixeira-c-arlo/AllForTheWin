"""Dockable panel: switch camera update_url between local firmware folders (stress testing)."""
from __future__ import annotations

from functools import partial
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.fw_setup_service import (
    active_folder_from_camera_update_url,
    build_camera_fota_url_for_folder,
    default_fw_server_root,
    scan_firmware_folders_with_versions,
)
from core.local_server import check_server_status, firmware_server_listener_summary, stop_http_server

ShellAsyncFn = Callable[[str, list[str], Callable[[bool, str], None]], None]

_ACCENT = "#00897B"
_MUTED = "#9e9e9e"
_OK = "#4caf7d"


class FwQuickSwitchPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shell_async: ShellAsyncFn | None = None
        self._connected = False
        self._profile_ok = False
        self._vmc_model = ""
        self._update_url_raw = ""
        self._onboarded: bool | None = None
        self._fw_root = default_fw_server_root()
        self._rows_host = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(6)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)

        title = QLabel("Firmware folders")
        title.setStyleSheet(f"color: {_ACCENT}; font-size: 13px; font-weight: bold;")
        outer.addWidget(title)

        hint = QLabel(
            "Requires a running local firmware server and E3 wired profile. "
            "Switch sends update_url to the camera (same rules as the FW Setup wizard)."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        outer.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._rows_host)
        scroll.setMinimumHeight(160)
        outer.addWidget(scroll, stretch=1)

        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        outer.addWidget(self._status_lbl)

        stop_row = QHBoxLayout()
        self._btn_stop = QPushButton("Stop server (this window)")
        self._btn_stop.setToolTip(
            "Stops the firmware HTTP server only if it was started in this ArloShell process."
        )
        self._btn_stop.clicked.connect(self._on_stop_server)
        stop_row.addWidget(self._btn_stop)
        stop_row.addStretch(1)
        outer.addLayout(stop_row)

        foot = QFrame()
        foot.setStyleSheet("QFrame { border-top: 1px solid #2a313a; }")
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(0, 8, 0, 0)
        self._foot_model = QLabel("Model: —")
        self._foot_model.setStyleSheet("color: #c5ced9; font-size: 11px;")
        self._foot_badge = QLabel("Onboarded")
        self._foot_badge.setVisible(False)
        self._foot_badge.setStyleSheet(
            "QLabel { background-color: #3949ab; color: #e8eaf6; border-radius: 8px; "
            "padding: 2px 8px; font-size: 10px; font-weight: 600; }"
        )
        fl.addWidget(self._foot_model)
        fl.addWidget(self._foot_badge)
        outer.addWidget(foot)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick_footer)
        self._timer.start(2500)

    def set_shell_async(self, fn: ShellAsyncFn) -> None:
        self._shell_async = fn

    def apply_state(self, info: dict[str, Any]) -> None:
        self._connected = bool(info.get("connected"))
        self._profile_ok = (info.get("command_profile") or "") == "e3_wired"
        self._vmc_model = str(info.get("model") or "").strip().upper() or ""
        self._update_url_raw = str(info.get("update_url_raw") or "").strip()
        raw_ob = info.get("is_onboarded")
        self._onboarded = raw_ob if isinstance(raw_ob, bool) else None
        self._fw_root = default_fw_server_root()
        self._foot_model.setText(f"Model: {self._vmc_model or '—'}")
        self._foot_badge.setVisible(self._onboarded is True)
        self._rebuild_rows()
        self._tick_footer()

    @Slot()
    def _tick_footer(self) -> None:
        hint, line, tip = firmware_server_listener_summary()
        self._status_lbl.setText(line)
        self._status_lbl.setToolTip(tip)
        running, _ = check_server_status()
        self._btn_stop.setEnabled(running)

    def _rebuild_rows(self) -> None:
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._connected or not self._profile_ok:
            lab = QLabel("Connect over E3 wired to use folder switching.")
            lab.setWordWrap(True)
            lab.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
            self._rows_layout.addWidget(lab)
            self._rows_layout.addStretch(1)
            return

        pairs = scan_firmware_folders_with_versions(self._fw_root, self._vmc_model)
        names = [n for n, _v in pairs]
        active = active_folder_from_camera_update_url(self._update_url_raw, names)

        if not pairs:
            lab = QLabel(
                f"No firmware folders found under:\n{self._fw_root}\n\n"
                f"(Expected binaries/{self._vmc_model or 'VMCxxxx'}/ or archives.)"
            )
            lab.setWordWrap(True)
            lab.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
            self._rows_layout.addWidget(lab)
        else:
            for name, ver in pairs:
                row = QFrame()
                row.setStyleSheet(
                    "QFrame { background-color: #161a20; border: 1px solid #2a313a; border-radius: 6px; }"
                )
                rl = QVBoxLayout(row)
                rl.setContentsMargins(8, 6, 8, 6)
                top = QHBoxLayout()
                nm = QLabel(name)
                bold = "font-weight: bold; color: #e8eef4;" if name == active else "color: #c5ced9;"
                nm.setStyleSheet(f"font-size: 12px; {bold}")
                top.addWidget(nm, 1)
                btn = QPushButton("Switch")
                btn.setStyleSheet(
                    f"QPushButton {{ background-color: {_ACCENT}; color: white; padding: 4px 12px; font-size: 11px; }}"
                )
                btn.clicked.connect(partial(self._on_switch_folder, name))
                top.addWidget(btn)
                rl.addLayout(top)
                vl = QLabel(f"Version: {ver}")
                vl.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
                rl.addWidget(vl)
                if name == active:
                    al = QLabel("● Active (matches update URL)")
                    al.setStyleSheet(f"color: {_OK}; font-size: 10px;")
                    rl.addWidget(al)
                self._rows_layout.addWidget(row)

        self._rows_layout.addStretch(1)

    def _on_switch_folder(self, folder_name: str) -> None:
        if not self._shell_async:
            return
        ok_b, err_b, url = build_camera_fota_url_for_folder(self._fw_root, folder_name)
        if not ok_b:
            QMessageBox.warning(self, "Firmware folders", err_b or "Cannot build URL.")
            return

        def after_reboot(ok_r: bool, msg_r: str) -> None:
            if not ok_r:
                QMessageBox.warning(
                    self,
                    "Firmware folders",
                    msg_r or "Reboot after update_url failed.",
                )

        def after_url(ok: bool, msg: str) -> None:
            if not ok:
                QMessageBox.warning(self, "Firmware folders", msg or "update_url failed.")
                return
            if self._onboarded is True:
                r = QMessageBox.question(
                    self,
                    "Firmware folders",
                    "Camera is onboarded. Run update check now (arlocmd update_refresh 1)?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if r == QMessageBox.StandardButton.Yes:
                    self._shell_async("arlocmd update_refresh", ["1"], lambda _o, _m: None)
            else:
                self._shell_async("arlocmd reboot", [], after_reboot)

        self._shell_async("arlocmd update_url", [url], after_url)

    @Slot()
    def _on_stop_server(self) -> None:
        ok, msg = stop_http_server()
        if not ok:
            QMessageBox.warning(self, "Firmware folders", msg or "Could not stop server.")
        self._tick_footer()
        self._rebuild_rows()
