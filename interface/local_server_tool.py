"""Tools → Local Server: dashboard for the local firmware HTTP server and env folders."""
from __future__ import annotations

import os
from typing import Any, Callable

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFocusEvent, QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.fw_setup_service import (
    active_folder_from_camera_update_url,
    build_camera_fota_url_for_folder,
    default_fw_server_root,
    firmware_folder_model_label,
    firmware_folder_version_label,
    folder_has_firmware_artifacts,
    folder_matches_connected_camera,
    get_local_ipv4,
    list_environment_folders,
    rename_server_folder,
    sanitize_server_folder_name,
    should_filter_firmware_folders_by_camera,
)
from core.local_server import (
    DEFAULT_PORT,
    FW_ENV_TAR_GZ_SUFFIXES,
    check_server_status,
    firmware_folder_rename_blocked_reason,
    get_base_url_if_serving_root,
    get_in_process_server_root_abs,
    get_running_server_url,
    start_http_server,
    stop_http_server,
)
from interface.app_styles import ARLO_ACCENT, set_arlo_pushbutton_variant

ShellAsyncFn = Callable[[str, list[str], Callable[[bool, str], None]], None]

_MUTED = "#8b95a5"
_OK = "#4caf7d"
_AMBER = "#c9a227"
_CARD_BG = "#161a20"
_CARD_BORDER = "rgba(255, 255, 255, 0.12)"
_TEXT = "#c5ced9"
_SECTION = "#7a8494"
_MONO = "Consolas, 'Cascadia Mono', monospace"


def _ql(decl: str) -> str:
    d = decl.strip()
    if not d.endswith(";"):
        d += ";"
    return f"QLabel {{ {d} }}"


def _status_dot_style(color: str) -> str:
    return (
        f"QLabel {{ background-color: {color}; border-radius: 4px; border: none; "
        "min-width: 8px; max-width: 8px; min-height: 8px; max-height: 8px; }"
    )


def _port_from_base_url(url: str) -> str:
    part = (url or "").replace("http://", "").replace("https://", "").strip("/")
    return part.split(":")[-1] if ":" in part else str(DEFAULT_PORT)


def _public_base_url_from_listener(base_localhost: str) -> str:
    """http://<LAN-IP>:<port> (no path)."""
    port = _port_from_base_url(base_localhost)
    return f"http://{get_local_ipv4()}:{port}"


def _server_state_for_root(root_abs: str) -> tuple[bool, str, bool]:
    """
    (serving_this_root, public_base_url_or_empty, stop_allowed_this_process).
    """
    root_abs = os.path.abspath(root_abs)
    in_proc_running, _ = check_server_status()
    served_here = get_in_process_server_root_abs()
    if in_proc_running and served_here == root_abs:
        ok, url = get_running_server_url()
        if ok and url:
            return True, _public_base_url_from_listener(url.rstrip("/")), True
    base = get_base_url_if_serving_root(root_abs)
    if base:
        return True, _public_base_url_from_listener(base.rstrip("/")), False
    return False, "", False


def _primary_archive_basename(folder_abs: str) -> str:
    arch = os.path.join(folder_abs, "archive")
    if not os.path.isdir(arch):
        return "—"
    try:
        names = sorted(os.listdir(arch), key=str.lower)
    except OSError:
        return "—"
    for name in names:
        low = name.lower()
        if low.endswith(".zip") or ".tar.gz" in low:
            return name
        if any(low.endswith(s) for s in FW_ENV_TAR_GZ_SUFFIXES):
            return name
    return "—"


class LocalServerTool(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shell_async: ShellAsyncFn | None = None
        self._connected = False
        self._profile_ok = False
        self._vmc_model = ""
        self._fw_version = ""
        self._serial = ""
        self._update_url_raw = ""
        self._onboarded: bool | None = None
        self._fw_root = default_fw_server_root()
        self._pending_refresh_folder: str | None = None
        # folder_name -> (message, "ok" | "err") survives card rebuilds
        self._folder_status: dict[str, tuple[str, str]] = {}
        self._fw_search_models: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(0)

        # --- Top: server status ---
        top = QFrame()
        top.setStyleSheet(
            "QFrame { background-color: #12161c; border: none; border-bottom: 1px solid rgba(255,255,255,0.08); }"
        )
        tl = QHBoxLayout(top)
        tl.setContentsMargins(24, 16, 24, 16)
        tl.setSpacing(12)
        self._dot_server = QLabel()
        self._dot_server.setFixedSize(8, 8)
        self._dot_server.setStyleSheet(_status_dot_style("#5c6570"))
        tl.addWidget(self._dot_server, 0, Qt.AlignmentFlag.AlignVCenter)
        self._lbl_server_state = QLabel("Server off")
        self._lbl_server_state.setStyleSheet(_ql(f"color: {_TEXT}; font-size: 13px;"))
        tl.addWidget(self._lbl_server_state)
        tl.addSpacing(8)
        self._url_edit = QLineEdit()
        self._url_edit.setReadOnly(True)
        self._url_edit.setPlaceholderText("")
        self._url_edit.setVisible(False)
        self._url_edit.setStyleSheet(
            f"QLineEdit {{ background-color: #1e242c; color: #e8eef4; "
            "border: 1px solid rgba(255,255,255,0.10); border-radius: 6px; padding: 6px 10px; "
            f"font-family: {_MONO}; font-size: 12px; }}"
        )
        tl.addWidget(self._url_edit, stretch=1)
        self._btn_start = QPushButton("Start")
        set_arlo_pushbutton_variant(self._btn_start, variant="blue", compact=False)
        self._btn_start.clicked.connect(self._on_start_server)
        tl.addWidget(self._btn_start)
        self._btn_stop = QPushButton("Stop")
        set_arlo_pushbutton_variant(self._btn_stop, variant=None, compact=False)
        self._btn_stop.clicked.connect(self._on_stop_server)
        self._btn_stop.setVisible(False)
        tl.addWidget(self._btn_stop)
        root.addWidget(top)

        cam_bar = QFrame()
        cam_bar.setStyleSheet(
            "QFrame { border: none; border-bottom: 1px solid rgba(255,255,255,0.08); background: transparent; }"
        )
        cl = QHBoxLayout(cam_bar)
        cl.setContentsMargins(24, 16, 24, 16)
        cl.setSpacing(12)
        self._lbl_camera_bar = QLabel("")
        self._lbl_camera_bar.setStyleSheet(_ql(f"color: {_TEXT}; font-size: 13px;"))
        cl.addWidget(self._lbl_camera_bar, stretch=1)
        self._badge_onboarded = QLabel("Onboarded")
        self._badge_onboarded.setVisible(False)
        self._badge_onboarded.setStyleSheet(
            "QLabel { background-color: rgba(57, 73, 171, 0.35); color: #c5cae9; border-radius: 10px; "
            "padding: 4px 10px; font-size: 10px; font-weight: 600; }"
        )
        cl.addWidget(self._badge_onboarded, 0, Qt.AlignmentFlag.AlignRight)
        self._badge_not_onboarded = QLabel("Not onboarded")
        self._badge_not_onboarded.setVisible(False)
        self._badge_not_onboarded.setStyleSheet(
            "QLabel { background-color: rgba(201, 162, 39, 0.22); color: #ffe082; border-radius: 10px; "
            "padding: 4px 10px; font-size: 10px; font-weight: 600; }"
        )
        cl.addWidget(self._badge_not_onboarded, 0, Qt.AlignmentFlag.AlignRight)
        root.addWidget(cam_bar)

        root.addSpacing(16)
        sec = QLabel("FIRMWARE FOLDERS")
        sec.setStyleSheet(_ql(f"color: {_SECTION}; font-size: 12px; font-weight: 500;"))
        root.addWidget(sec)

        root.addSpacing(12)
        self._lbl_filter_note = QLabel("")
        self._lbl_filter_note.setWordWrap(True)
        self._lbl_filter_note.setStyleSheet(_ql(f"color: {_MUTED}; font-size: 11px;"))
        root.addWidget(self._lbl_filter_note)

        root.addSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._folders_host = QWidget()
        self._folders_layout = QVBoxLayout(self._folders_host)
        self._folders_layout.setContentsMargins(0, 0, 0, 0)
        self._folders_layout.setSpacing(11)
        scroll.setWidget(self._folders_host)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(scroll, stretch=1)

        bottom = QFrame()
        bottom.setStyleSheet("QFrame { border: none; border-top: 1px solid rgba(255,255,255,0.08); }")
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(0, 14, 0, 0)
        bl.setSpacing(10)
        self._lbl_root_path = QLabel("")
        self._lbl_root_path.setStyleSheet(
            _ql(f"color: {_MUTED}; font-size: 11px; font-family: {_MONO};")
        )
        self._lbl_root_path.setWordWrap(True)
        bl.addWidget(self._lbl_root_path, stretch=1)
        self._btn_download_fw = QPushButton("Download firmware")
        set_arlo_pushbutton_variant(self._btn_download_fw, variant="primary", compact=True)
        self._btn_download_fw.clicked.connect(self._on_download_firmware)
        bl.addWidget(self._btn_download_fw)
        self._btn_open_root = QPushButton("Open root folder")
        set_arlo_pushbutton_variant(self._btn_open_root, variant=None, compact=True)
        self._btn_open_root.clicked.connect(self._on_open_root)
        bl.addWidget(self._btn_open_root)
        root.addWidget(bottom)

        self._full_refresh()

    def set_shell_async(self, fn: ShellAsyncFn) -> None:
        self._shell_async = fn

    def apply_state(self, info: dict[str, Any]) -> None:
        self._connected = bool(info.get("connected"))
        if not self._connected:
            self._profile_ok = False
            self._vmc_model = ""
            self._fw_version = "—"
            self._serial = ""
            self._update_url_raw = ""
            self._onboarded = None
            self._fw_search_models = []
        else:
            self._profile_ok = (info.get("command_profile") or "") == "e3_wired"
            self._vmc_model = str(info.get("model") or "").strip() or ""
            self._fw_version = str(info.get("fw") or "").strip() or "—"
            self._serial = str(info.get("serial") or "").strip()
            self._update_url_raw = str(info.get("update_url_raw") or "").strip()
            raw_ob = info.get("is_onboarded")
            self._onboarded = raw_ob if isinstance(raw_ob, bool) else None
            raw_fs = info.get("fw_search_models")
            if isinstance(raw_fs, list) and raw_fs:
                self._fw_search_models = [str(x) for x in raw_fs if str(x).strip()]
            else:
                self._fw_search_models = []
        self._fw_root = default_fw_server_root()
        self._sync_camera_bar()
        self._sync_filter_note()
        self._rebuild_folder_cards()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._full_refresh()

    def focusInEvent(self, event: QFocusEvent) -> None:
        super().focusInEvent(event)
        self._full_refresh()

    def refresh_if_visible(self) -> None:
        if self.isVisible():
            self._full_refresh()

    def _full_refresh(self) -> None:
        self._fw_root = default_fw_server_root()
        self._sync_server_bar()
        self._sync_camera_bar()
        self._sync_filter_note()
        self._rebuild_folder_cards()
        self._lbl_root_path.setText(self._fw_root)

    def _sync_server_bar(self) -> None:
        running, pub_url, can_stop = _server_state_for_root(self._fw_root)
        if running and pub_url:
            self._dot_server.setStyleSheet(_status_dot_style(_OK))
            self._lbl_server_state.setText("Server running")
            self._url_edit.setText(pub_url)
            self._url_edit.setVisible(True)
            self._btn_start.setVisible(False)
            self._btn_stop.setVisible(can_stop)
            self._btn_stop.setEnabled(can_stop)
        else:
            self._dot_server.setStyleSheet(_status_dot_style("#5c6570"))
            self._lbl_server_state.setText("Server off")
            self._url_edit.clear()
            self._url_edit.setVisible(False)
            self._btn_start.setVisible(True)
            self._btn_stop.setVisible(False)

    def _sync_camera_bar(self) -> None:
        self._badge_onboarded.setVisible(self._onboarded is True)
        self._badge_not_onboarded.setVisible(
            self._connected and self._profile_ok and self._onboarded is False
        )
        if not self._connected or not self._profile_ok:
            self._lbl_camera_bar.setText(
                f'<span style="color:{_MUTED};">No camera connected</span>'
            )
            self._lbl_camera_bar.setTextFormat(Qt.TextFormat.RichText)
            return
        mono_serial = (
            f'<span style="font-family: Consolas, monospace; font-size:13px;">{self._serial or "—"}</span>'
        )
        self._lbl_camera_bar.setText(
            f'<span style="font-size:15px;font-weight:500;color:#e8eef4;">{self._vmc_model or "—"}</span>'
            f" &nbsp; {mono_serial} &nbsp; "
            f'<span style="color:{_TEXT};font-size:13px;">FW {self._fw_version}</span>'
        )
        self._lbl_camera_bar.setTextFormat(Qt.TextFormat.RichText)

    def _should_filter_folders(self) -> bool:
        return should_filter_firmware_folders_by_camera(
            connected=self._connected,
            profile_e3_wired=self._profile_ok,
            model_name=self._vmc_model,
        )

    def _sync_filter_note(self) -> None:
        if self._should_filter_folders():
            self._lbl_filter_note.hide()
            self._lbl_filter_note.clear()
        else:
            self._lbl_filter_note.setText("Connect a camera to filter by model.")
            self._lbl_filter_note.show()

    def _visible_folder_names(self, all_names: list[str], root_abs: str) -> list[str]:
        if not self._should_filter_folders():
            return list(all_names)
        vmc = self._vmc_model.strip().upper()
        out: list[str] = []
        for n in all_names:
            p = os.path.join(root_abs, n)
            if folder_matches_connected_camera(
                p, vmc, search_aliases=self._fw_search_models
            ):
                out.append(n)
        return out

    def _on_download_firmware(self) -> None:
        from interface.local_server_download_dialog import LocalServerDownloadDialog

        dlg = LocalServerDownloadDialog(
            self,
            fw_root=self._fw_root,
            camera_connected=self._connected and self._profile_ok,
            device_model=self._vmc_model,
            fw_search_models=self._fw_search_models,
            on_complete=self._full_refresh,
        )
        dlg.exec()

    def _on_start_server(self) -> None:
        root_abs = os.path.abspath(self._fw_root)
        running, _, _ = _server_state_for_root(root_abs)
        if running:
            self._full_refresh()
            return
        ok, msg = start_http_server(root_abs, DEFAULT_PORT)
        if not ok:
            QMessageBox.warning(self, "Local Server", msg or "Could not start server.")
        self._full_refresh()

    def _on_stop_server(self) -> None:
        ok, msg = stop_http_server()
        if not ok:
            QMessageBox.warning(self, "Local Server", msg or "Could not stop server.")
        self._full_refresh()

    def _on_open_root(self) -> None:
        p = os.path.abspath(self._fw_root)
        if os.path.isdir(p):
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    def _open_folder(self, folder_abs: str) -> None:
        if os.path.isdir(folder_abs):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder_abs))

    def _rebuild_folder_cards(self) -> None:
        while self._folders_layout.count():
            item = self._folders_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        root_abs = os.path.abspath(self._fw_root)
        all_names = list_environment_folders(self._fw_root)
        visible = self._visible_folder_names(all_names, root_abs)
        running, _pub, _ = _server_state_for_root(root_abs)
        active = active_folder_from_camera_update_url(self._update_url_raw, visible)

        if not visible:
            if not all_names:
                lab = QLabel(f"No subfolders under the server root.\n{root_abs}")
            elif self._should_filter_folders():
                lab = QLabel(
                    f"No firmware found for {self._vmc_model.strip().upper()}. "
                    "Use the download button below or the FW Wizard to add firmware."
                )
            else:
                lab = QLabel(f"No subfolders under the server root.\n{root_abs}")
            lab.setStyleSheet(_ql(f"color: {_MUTED}; font-size: 12px;"))
            lab.setWordWrap(True)
            self._folders_layout.addWidget(lab)
            self._folders_layout.addStretch(1)
            return

        for name in visible:
            card = self._make_folder_card(name, root_abs, running, active == name)
            self._folders_layout.addWidget(card)
        self._folders_layout.addStretch(1)

    def _make_folder_card(
        self,
        folder_name: str,
        root_abs: str,
        server_running: bool,
        is_active: bool,
    ) -> QFrame:
        folder_abs = os.path.join(root_abs, folder_name)
        has_fw = folder_has_firmware_artifacts(folder_abs)
        if is_active:
            border = f"2px solid {ARLO_ACCENT}"
        else:
            border = f"1px solid {_CARD_BORDER}"

        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background-color: {_CARD_BG}; border: {border}; border-radius: 11px; }}"
        )
        vl = QVBoxLayout(card)
        vl.setContentsMargins(16, 15, 16, 15)
        vl.setSpacing(10)

        st = self._folder_status.get(folder_name)
        if st:
            txt, kind = st
            col = _OK if kind == "ok" else _AMBER
            line = QLabel(txt)
            line.setStyleSheet(_ql(f"color: {col}; font-size: 11px;"))
            vl.addWidget(line)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title = QLabel(folder_name)
        title.setStyleSheet(_ql("font-size: 15px; font-weight: 500; color: #e8eef4;"))
        title_row.addWidget(title)
        if is_active:
            badge = QLabel("Active")
            badge.setStyleSheet(
                f"QLabel {{ background-color: rgba(0, 137, 123, 0.28); color: #80cbc4; border-radius: 10px; "
                "padding: 4px 10px; font-size: 10px; font-weight: 600; }"
            )
            title_row.addWidget(badge)
        title_row.addStretch(1)
        vl.addLayout(title_row)

        if is_active:
            sub = QLabel("Camera's update URL points here")
            sub.setStyleSheet(_ql(f"color: {_MUTED}; font-size: 11px;"))
            vl.addWidget(sub)

        if has_fw:
            inner = QFrame()
            inner.setStyleSheet(
                "QFrame { background-color: rgba(0,0,0,0.22); border: 1px solid rgba(255,255,255,0.06); "
                "border-radius: 8px; }"
            )
            il = QVBoxLayout(inner)
            il.setContentsMargins(12, 10, 12, 10)
            il.setSpacing(10)
            ver = firmware_folder_version_label(folder_abs)
            model_l = firmware_folder_model_label(folder_abs)
            arch = _primary_archive_basename(folder_abs)
            il.addLayout(self._kv_block("Version", ver, mono_value=False))
            il.addLayout(self._kv_block("Model", model_l, mono_value=False))
            il.addLayout(self._kv_block("Archive", arch, mono_value=True))
            vl.addWidget(inner)
        else:
            empty_l = QLabel("Empty — no firmware detected")
            empty_l.setStyleSheet(_ql(f"color: {_MUTED}; font-size: 12px;"))
            vl.addWidget(empty_l)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        use_tooltip = ""
        can_connect = self._connected and self._profile_ok
        if not can_connect:
            use_tooltip = "Connect a camera first."
        elif not server_running:
            use_tooltip = "Start the local server first."
        elif not has_fw:
            use_tooltip = ""

        use_btn = QPushButton("Use this")
        set_arlo_pushbutton_variant(use_btn, variant="primary", compact=True)
        use_btn.setEnabled(
            has_fw
            and server_running
            and can_connect
            and not is_active
        )
        if use_tooltip:
            use_btn.setToolTip(use_tooltip)
        use_btn.clicked.connect(lambda _c=False, fn=folder_name: self._on_use_folder(fn))
        btn_row.addWidget(use_btn)

        ref_btn = QPushButton("Trigger update check")
        set_arlo_pushbutton_variant(ref_btn, variant="blue", compact=True)
        show_ref = self._pending_refresh_folder == folder_name and self._onboarded is True
        ref_btn.setVisible(bool(show_ref))
        ref_btn.clicked.connect(
            lambda _c=False, fn=folder_name, rb=ref_btn: self._on_trigger_refresh(fn, rb)
        )
        btn_row.addWidget(ref_btn)

        btn_row.addStretch(1)

        ren = QPushButton("Rename")
        set_arlo_pushbutton_variant(ren, variant=None, compact=True)
        ren.clicked.connect(lambda _c=False, fn=folder_name: self._on_rename_folder(fn))
        btn_row.addWidget(ren)

        opn = QPushButton("Open")
        set_arlo_pushbutton_variant(opn, variant=None, compact=True)
        opn.clicked.connect(lambda _c=False, fa=folder_abs: self._open_folder(fa))
        btn_row.addWidget(opn)

        vl.addLayout(btn_row)
        return card

    def _kv_block(self, caption: str, value: str, *, mono_value: bool) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(2)
        cap = QLabel(caption)
        cap.setStyleSheet(_ql(f"color: {_MUTED}; font-size: 11px;"))
        col.addWidget(cap)
        val = QLabel(value or "—")
        mono = f"font-family: {_MONO};" if mono_value else ""
        val.setStyleSheet(_ql(f"color: {_TEXT}; font-size: 13px; {mono}"))
        val.setWordWrap(True)
        col.addWidget(val)
        return col

    def _on_rename_folder(self, old_name: str) -> None:
        folder_abs = os.path.join(os.path.abspath(self._fw_root), old_name)
        if firmware_folder_rename_blocked_reason(folder_abs):
            QMessageBox.information(
                self,
                "Local Server",
                "Stop the server first to rename folders.",
            )
            return
        new_name, ok = QInputDialog.getText(self, "Rename folder", "New folder name:", text=old_name)
        if not ok:
            return
        new_name = (new_name or "").strip()
        if not new_name or new_name == old_name:
            return
        sn = sanitize_server_folder_name(new_name)
        if not sn:
            QMessageBox.warning(self, "Local Server", "Invalid folder name.")
            return
        r_ok, err = rename_server_folder(self._fw_root, old_name, sn)
        if not r_ok:
            QMessageBox.warning(self, "Local Server", err or "Rename failed.")
        self._full_refresh()

    def _on_use_folder(self, folder_name: str) -> None:
        if not self._shell_async:
            return
        ok_b, err_b, url = build_camera_fota_url_for_folder(self._fw_root, folder_name)
        if not ok_b:
            self._folder_status[folder_name] = (err_b or "Cannot build URL.", "err")
            self._full_refresh()
            return

        def done_refresh_ui() -> None:
            self._full_refresh()

        def after_reboot(ok_r: bool, msg_r: str) -> None:
            if ok_r:
                self._folder_status[folder_name] = ("Reboot sent ✓", "ok")
            else:
                self._folder_status[folder_name] = (msg_r or "Reboot failed.", "err")
            done_refresh_ui()

        def after_url(ok: bool, msg: str) -> None:
            if not ok:
                self._folder_status[folder_name] = (msg or "update_url failed.", "err")
                done_refresh_ui()
                return
            if self._onboarded is True:
                self._folder_status[folder_name] = ("update_url OK ✓", "ok")
                self._pending_refresh_folder = folder_name
                done_refresh_ui()
            else:
                self._folder_status[folder_name] = ("update_url OK ✓", "ok")
                self._shell_async("arlocmd reboot", [], after_reboot)

        self._shell_async("arlocmd update_url", [url], after_url)

    def _on_trigger_refresh(self, folder_name: str, ref_btn: QPushButton) -> None:
        if not self._shell_async:
            return

        def done(ok: bool, msg: str) -> None:
            ref_btn.setEnabled(True)
            if ok:
                self._folder_status[folder_name] = ((msg or "update_refresh OK").strip()[:200], "ok")
                self._pending_refresh_folder = None
            else:
                self._folder_status[folder_name] = (msg or "update_refresh failed.", "err")
            self._full_refresh()

        ref_btn.setEnabled(False)
        self._shell_async("arlocmd update_refresh", ["1"], done)
