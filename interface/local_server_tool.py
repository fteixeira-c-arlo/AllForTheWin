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
    create_empty_server_folder,
    default_fw_server_root,
    extract_vmc_model_ids_from_text,
    firmware_folder_model_label,
    firmware_folder_version_label,
    folder_has_firmware_artifacts,
    folder_matches_connected_camera,
    get_local_ipv4,
    is_firmware_archive,
    list_environment_folders,
    rename_server_folder,
    sanitize_server_folder_name,
    should_filter_firmware_folders_by_camera,
    vmc_binaries_folder_name_for_device,
)
from core.local_server import (
    DEFAULT_PORT,
    check_server_status,
    firmware_folder_rename_blocked_reason,
    get_base_url_if_serving_root,
    get_in_process_server_root_abs,
    get_running_server_url,
    start_http_server,
    stop_http_server,
)
from interface.app_styles import ARLO_ACCENT, apply_qframe_stylesheet, set_arlo_pushbutton_variant

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


def _brief_shell_status_line(msg: str, *, fallback: str, max_len: int = 200) -> str:
    """One line for UI labels — avoids multiline / RichText mis-detection on device output."""
    t = (msg or "").strip()
    if not t:
        return fallback
    line = t.splitlines()[0].strip()
    if not line:
        return fallback
    return line[:max_len] if len(line) > max_len else line


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


def _primary_archive_basename(folder_abs: str, *, preferred_vmc: str | None = None) -> str:
    """
    Pick one archive/ filename to show on folder cards.

    Alphabetically, VMC2070* sorts before VMC3070*; prefer the archive whose name includes the
    connected camera model (or binaries/ model label), then newest mtime among ties.
    """
    arch = os.path.join(folder_abs, "archive")
    if not os.path.isdir(arch):
        return "—"
    try:
        names = [n for n in os.listdir(arch) if is_firmware_archive(n)]
    except OSError:
        return "—"
    if not names:
        return "—"

    def mtime(n: str) -> float:
        try:
            return os.path.getmtime(os.path.join(arch, n))
        except OSError:
            return 0.0

    pref = (preferred_vmc or "").strip().upper()
    if pref:
        want = frozenset([pref])
        matched = [n for n in names if extract_vmc_model_ids_from_text(n) & want]
        if matched:
            return max(matched, key=mtime)
    return max(names, key=mtime)


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
        apply_qframe_stylesheet(
            top,
            "QFrame { background-color: #12161c; border: none; border-bottom: 1px solid rgba(255,255,255,0.08); }",
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
        apply_qframe_stylesheet(
            cam_bar,
            "QFrame { border: none; border-bottom: 1px solid rgba(255,255,255,0.08); background-color: transparent; }",
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

        self._missing_root_banner = QFrame()
        apply_qframe_stylesheet(
            self._missing_root_banner,
            f"QFrame {{ background-color: rgba(201, 162, 39, 0.12); border: 1px solid {_AMBER}; "
            "border-radius: 8px; }",
        )
        mvl = QVBoxLayout(self._missing_root_banner)
        mvl.setContentsMargins(14, 12, 14, 12)
        mvl.setSpacing(8)
        self._lbl_missing_root = QLabel("")
        self._lbl_missing_root.setWordWrap(True)
        self._lbl_missing_root.setStyleSheet(_ql(f"color: {_TEXT}; font-size: 12px;"))
        mvl.addWidget(self._lbl_missing_root)
        self._btn_setup_root = QPushButton("Set up firmware folder…")
        set_arlo_pushbutton_variant(self._btn_setup_root, variant="primary", compact=True)
        self._btn_setup_root.clicked.connect(self._on_setup_fw_root)
        mvl.addWidget(self._btn_setup_root, 0, Qt.AlignmentFlag.AlignLeft)
        self._missing_root_banner.hide()
        root.addWidget(self._missing_root_banner)

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
        apply_qframe_stylesheet(
            bottom,
            "QFrame { border: none; border-top: 1px solid rgba(255,255,255,0.08); background-color: transparent; }",
        )
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
        self._btn_new_folder = QPushButton("New folder")
        set_arlo_pushbutton_variant(self._btn_new_folder, variant=None, compact=True)
        self._btn_new_folder.setToolTip(
            "Create archive/, binaries/, and updaterules/ under a new server folder name."
        )
        self._btn_new_folder.clicked.connect(self._on_new_folder)
        bl.addWidget(self._btn_new_folder)
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
        self._sync_missing_root_banner()
        self._sync_server_bar()
        self._sync_camera_bar()
        self._sync_filter_note()
        self._rebuild_folder_cards()
        self._lbl_root_path.setText(self._fw_root)

    def _sync_missing_root_banner(self) -> None:
        root_abs = os.path.abspath(self._fw_root)
        if os.path.isdir(root_abs):
            self._missing_root_banner.hide()
            return
        self._lbl_missing_root.setText(
            f"The firmware server folder does not exist yet:\n{root_abs}\n\n"
            "Create the recommended folder under your profile, use your configured path, or pick "
            "another location. The path is saved for next time (unless FW_SERVER_ROOT is set)."
        )
        self._missing_root_banner.show()

    def _on_setup_fw_root(self) -> None:
        from interface.fw_server_root_qt import qt_ensure_fw_server_root

        new_root = qt_ensure_fw_server_root(self, self._fw_root)
        if not new_root:
            return
        self._fw_root = new_root
        self._full_refresh()

    def _sync_server_bar(self) -> None:
        root_missing = not os.path.isdir(os.path.abspath(self._fw_root))
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
        self._btn_start.setEnabled(not root_missing)
        self._btn_start.setToolTip(
            "Set up the firmware folder first (banner above)."
            if root_missing
            else "Start HTTP server for this folder"
        )
        self._btn_download_fw.setEnabled(not root_missing)
        self._btn_new_folder.setEnabled(not root_missing)

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

    def _binaries_model_for_new_folder(self) -> str:
        """VMC subfolder under binaries/ — matches Download firmware / wizard when possible."""
        if self._connected and self._profile_ok and (self._vmc_model or "").strip():
            return vmc_binaries_folder_name_for_device(self._vmc_model)
        if self._fw_search_models:
            return (self._fw_search_models[0] or "VMC3070").strip().upper()
        return "VMC3070"

    def _on_new_folder(self) -> None:
        root_abs = os.path.abspath(self._fw_root)
        if not os.path.isdir(root_abs):
            QMessageBox.warning(
                self,
                "Local Server",
                f"Server root does not exist or is not a directory:\n{root_abs}",
            )
            return
        name, ok = QInputDialog.getText(
            self,
            "New folder",
            "Server folder name (one path segment, no \\ / : * ? \" < > |):",
        )
        if not ok:
            return
        model = self._binaries_model_for_new_folder()
        ok_c, path_or_err = create_empty_server_folder(
            root_abs,
            (name or "").strip(),
            model,
            self._fw_search_models,
        )
        if not ok_c:
            QMessageBox.warning(self, "Local Server", path_or_err or "Could not create folder.")
            return
        QMessageBox.information(
            self,
            "Local Server",
            f"Created empty firmware layout for model binaries “{model}”:\n{path_or_err}",
        )
        self._full_refresh()

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
            if self._should_filter_folders() and all_names:
                lab = QLabel(
                    f"No firmware found for {self._vmc_model.strip().upper()}. "
                    "Use the download button below or the FW Wizard to add firmware."
                )
            else:
                lab = QLabel(
                    f"No subfolders under the server root.\n{root_abs}\n\n"
                    'Use "New folder" for an empty layout, or "Download firmware" and type a new '
                    "name in the server folder field to pull builds from Artifactory."
                )
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
        apply_qframe_stylesheet(
            card,
            f"QFrame {{ background-color: {_CARD_BG}; border: {border}; border-radius: 11px; }}",
        )
        vl = QVBoxLayout(card)
        vl.setContentsMargins(16, 15, 16, 15)
        vl.setSpacing(10)

        st = self._folder_status.get(folder_name)
        if st:
            txt, kind = st
            col = _OK if kind == "ok" else _AMBER
            line = QLabel(txt)
            line.setTextFormat(Qt.TextFormat.PlainText)
            line.setWordWrap(True)
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
                "QLabel { background-color: rgba(0, 137, 123, 0.28); color: #80cbc4; border-radius: 10px; "
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
            apply_qframe_stylesheet(
                inner,
                "QFrame { background-color: rgba(0,0,0,0.22); border: 1px solid rgba(255,255,255,0.06); "
                "border-radius: 8px; }",
            )
            il = QVBoxLayout(inner)
            il.setContentsMargins(12, 10, 12, 10)
            il.setSpacing(10)
            ver = firmware_folder_version_label(folder_abs)
            model_l = firmware_folder_model_label(folder_abs)
            pref_vmc = ""
            if self._connected and self._profile_ok and (self._vmc_model or "").strip():
                pref_vmc = (self._vmc_model or "").strip().upper()
            elif model_l and model_l != "—":
                pref_vmc = str(model_l).strip().upper()
            arch = _primary_archive_basename(
                folder_abs, preferred_vmc=pref_vmc or None
            )
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
        val.setTextFormat(Qt.TextFormat.PlainText)
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
                self._folder_status[folder_name] = (
                    _brief_shell_status_line(msg_r, fallback="Reboot failed."),
                    "err",
                )
            done_refresh_ui()

        def after_url(ok: bool, msg: str) -> None:
            if not ok:
                self._folder_status[folder_name] = (
                    _brief_shell_status_line(msg, fallback="update_url failed."),
                    "err",
                )
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
            # Do not call ref_btn.setEnabled(True) here: apply_state / focusInEvent / this
            # callback's _full_refresh() may have already deleteLater()'d that widget; touching
            # a stale QPushButton* causes undefined behavior. Rebuild replaces the button.
            if ok:
                # stdout can be large HAL logs; QLabel AutoText may misparse "<..." as HTML.
                self._folder_status[folder_name] = ("Update check sent ✓", "ok")
                self._pending_refresh_folder = None
            else:
                self._folder_status[folder_name] = (
                    _brief_shell_status_line(msg, fallback="update_refresh failed."),
                    "err",
                )
            self._full_refresh()

        ref_btn.setEnabled(False)
        self._shell_async("arlocmd update_refresh", ["1"], done)
