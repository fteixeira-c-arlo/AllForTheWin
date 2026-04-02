"""GUI wizard: Artifactory firmware setup, local server, camera update_url."""
from __future__ import annotations

import os
import re
from typing import Any, Callable

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.artifactory_client import ARTIFACTORY_REPO, test_artifactory_access
from core.local_server import (
    DEFAULT_PORT,
    check_server_status,
    firmware_folder_rename_blocked_reason,
    firmware_rename_access_denied_user_hint,
    firmware_server_listener_summary,
    get_running_server_url,
    is_firmware_port_accepting_connections,
    read_fw_server_state,
    stop_http_server,
)
from core.fw_setup_service import (
    compute_download_model,
    default_artifactory_url,
    default_fw_server_root,
    download_firmware_to_layout,
    ensure_server_and_camera_url,
    extract_firmware_archive,
    folder_has_firmware_artifacts,
    list_environment_folders,
    prepare_env_directories,
    rename_server_folder,
    sanitize_server_folder_name,
    scan_firmware_folders_with_versions,
    search_firmware_archives,
)
from core.camera_models import get_models
from utils.config_manager import (
    decode_token,
    get_config_path,
    load_config_file,
    save_config_file,
    update_last_used,
)

# Match main window accents (see gui_window.py)
_ACCENT = "#00897B"
_OK = "#4caf7d"
_ERR = "#e05555"
_AMBER = "#c9a227"
_MUTED = "#9e9e9e"
_SIDEBAR_BG = "#0d0d0d"
_BORDER = "#1e1e1e"


ShellAsyncFn = Callable[[str, list[str], Callable[[bool, str], None]], None]


def _port_from_running_server_url(ok: bool, url: str) -> str:
    if ok and url:
        part = url.replace("http://", "").replace("https://", "").strip("/")
        return part.split(":")[-1] if ":" in part else str(DEFAULT_PORT)
    return str(DEFAULT_PORT)


class _SearchThread(QThread):
    finished_search = Signal(bool, object, str)

    def __init__(
        self,
        base_url: str,
        token: str,
        username: str | None,
        version_filter: str,
        fw_search_models: list[str],
    ) -> None:
        super().__init__()
        self._base_url = base_url
        self._token = token
        self._username = username
        self._version_filter = version_filter
        self._fw_search_models = fw_search_models

    def run(self) -> None:
        ok, flat, err = search_firmware_archives(
            self._base_url,
            self._token,
            self._version_filter,
            self._fw_search_models,
            self._username,
        )
        self.finished_search.emit(ok, flat, err)


class _DownloadThread(QThread):
    byte_progress = Signal(int, object)
    status_text = Signal(str)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(
        self,
        token: str,
        download_model: str,
        version: str,
        binaries_dir_for_download: str,
        updaterules_dir: str,
        archive_dir: str,
        base_url: str,
        username: str | None,
        selected_filename: str | None,
        archive_path: str,
        chosen_binaries_dir: str,
        rules_dir: str,
    ) -> None:
        super().__init__()
        self._token = token
        self._download_model = download_model
        self._version = version
        self._binaries_dir_for_download = binaries_dir_for_download
        self._updaterules_dir = updaterules_dir
        self._archive_dir = archive_dir
        self._base_url = base_url
        self._username = username
        self._selected_filename = selected_filename
        self._archive_path = archive_path
        self._chosen_binaries_dir = chosen_binaries_dir
        self._rules_dir = rules_dir

    def run(self) -> None:
        def _per_file(name: str, idx: int, total: int) -> None:
            self.status_text.emit(f"Downloading {name} ({idx}/{total})…")

        def _bytes(done: int, total: int | None) -> None:
            self.byte_progress.emit(done, total)

        self.status_text.emit("Downloading from Artifactory…")
        ok, err = download_firmware_to_layout(
            self._token,
            self._download_model,
            self._version,
            self._binaries_dir_for_download,
            self._updaterules_dir,
            self._archive_dir,
            self._base_url,
            self._username,
            self._selected_filename,
            progress_callback=_per_file,
            byte_progress_callback=_bytes,
        )
        if not ok:
            self.failed.emit(err or "Download failed.")
            return
        if self._selected_filename and os.path.isfile(self._archive_path):
            self.status_text.emit("Extracting .enc and update-rules JSON…")
            ok_e, err_e = extract_firmware_archive(
                self._archive_path, self._chosen_binaries_dir, self._rules_dir
            )
            if not ok_e:
                self.failed.emit(err_e or "Extraction failed.")
                return
        self.finished_ok.emit()


class FwSetupWizard(QDialog):
    """Five-step firmware setup wizard."""

    server_started = Signal(str)
    update_sent = Signal(bool)
    wizard_closed = Signal()

    def __init__(
        self,
        parent: QWidget | None,
        model_dict: dict[str, Any],
        shell_async: ShellAsyncFn,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Firmware setup")
        self.setMinimumSize(560, 440)
        self.setMaximumSize(920, 640)
        self.resize(900, 600)
        self.setModal(False)

        self._model_dict = dict(model_dict)
        self._shell_async = shell_async

        self._base_url = default_artifactory_url()
        self._username: str | None = None
        self._token = ""
        self._search_busy = False
        self._skip_server_close_dialog = False

        self._fw_root = default_fw_server_root()
        self._search_results: list[tuple[str, str]] = []
        self._selected_folder: str | None = None
        self._selected_filename: str | None = None
        self._version_path: str = ""
        self._server_folder_name = ""
        self._primary_model_name = (self._model_dict.get("name") or "Camera").strip()
        self._fw_search_models: list[str] = list(
            self._model_dict.get("fw_search_models") or [self._primary_model_name]
        )
        self._camera_url = ""
        self._search_thread: _SearchThread | None = None
        self._download_thread: _DownloadThread | None = None
        raw_ob = self._model_dict.get("is_onboarded")
        self._is_onboarded: bool | None = raw_ob if isinstance(raw_ob, bool) else None

        self._build_ui()
        self._refresh_server_footer()
        self._server_timer = QTimer(self)
        self._server_timer.timeout.connect(self._refresh_server_footer)
        self._server_timer.start(2000)

        self._load_config_into_step1()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._skip_server_close_dialog:
            running, _ = check_server_status()
            if running:
                ok_u, url = get_running_server_url()
                port = _port_from_running_server_url(ok_u, url)
                mb = QMessageBox(self)
                mb.setWindowTitle("Firmware setup")
                mb.setIcon(QMessageBox.Icon.Question)
                mb.setText(
                    f"Local firmware server is still running on port {port}. "
                    "Keep it running, stop it, or cancel?"
                )
                mb.addButton("Keep running", QMessageBox.ButtonRole.AcceptRole)
                stop_btn = mb.addButton("Stop and close", QMessageBox.ButtonRole.DestructiveRole)
                cancel_btn = mb.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
                mb.setDefaultButton(cancel_btn)
                mb.exec()
                clicked = mb.clickedButton()
                if clicked == cancel_btn:
                    event.ignore()
                    return
                if clicked == stop_btn:
                    stop_http_server()
            else:
                st = read_fw_server_state()
                busy_port: int | None = None
                foreign = False
                if st and is_firmware_port_accepting_connections(int(st["port"])):
                    busy_port = int(st["port"])
                    foreign = int(st["pid"]) != os.getpid()
                elif is_firmware_port_accepting_connections(DEFAULT_PORT):
                    busy_port = DEFAULT_PORT
                if busy_port is not None:
                    mb = QMessageBox(self)
                    mb.setWindowTitle("Firmware setup")
                    mb.setIcon(QMessageBox.Icon.Question)
                    if foreign and st:
                        mb.setText(
                            f"A firmware server is still listening on port {busy_port} "
                            f"(another ArloShell, PID {int(st['pid'])}). "
                            "This window cannot stop it. Close the wizard anyway?"
                        )
                    else:
                        mb.setText(
                            f"Port {busy_port} is still in use (unknown listener). Close the wizard anyway?"
                        )
                    mb.addButton("Close anyway", QMessageBox.ButtonRole.AcceptRole)
                    cancel_btn = mb.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
                    mb.setDefaultButton(cancel_btn)
                    mb.exec()
                    if mb.clickedButton() == cancel_btn:
                        event.ignore()
                        return
        self.wizard_closed.emit()
        super().closeEvent(event)

    def _build_ui(self) -> None:
        self._current_step = 0
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QFrame()
        sidebar.setFixedWidth(210)
        sidebar.setStyleSheet(f"QFrame {{ background-color: {_SIDEBAR_BG}; border-right: 1px solid {_BORDER}; }}")
        side_lay = QVBoxLayout(sidebar)
        side_lay.setContentsMargins(14, 20, 14, 16)
        side_lay.setSpacing(0)

        self._step_labels: list[QLabel] = []
        self._step_checks: list[str] = []  # "", "done", "active"
        titles = [
            "Credentials",
            "Search firmware",
            "Select version",
            "Download",
            "Serve & update",
        ]
        for i, title in enumerate(titles):
            row = QHBoxLayout()
            num = QLabel(str(i + 1))
            num.setFixedWidth(22)
            num.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._step_labels.append(num)
            lab = QLabel(title)
            lab.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
            lab.setWordWrap(True)
            row.addWidget(num)
            row.addWidget(lab, 1)
            side_lay.addLayout(row)
            if i < len(titles) - 1:
                conn_row = QHBoxLayout()
                conn_row.setContentsMargins(0, 2, 0, 2)
                indent = QWidget()
                indent.setFixedWidth(10)
                vline = QFrame()
                vline.setFixedWidth(1)
                vline.setMinimumHeight(12)
                vline.setMaximumWidth(1)
                vline.setStyleSheet(f"background-color: {_BORDER}; border: none;")
                conn_row.addWidget(indent)
                conn_row.addWidget(vline)
                conn_row.addStretch(1)
                conn_wrap = QWidget()
                conn_wrap.setLayout(conn_row)
                side_lay.addWidget(conn_wrap)

        side_lay.addStretch(1)

        self._server_footer_dot = QLabel("●")
        self._server_footer_dot.setStyleSheet(f"color: {_MUTED}; font-size: 14px;")
        self._server_footer_text = QLabel("Server: —")
        self._server_footer_text.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        self._server_footer_text.setWordWrap(True)
        foot = QHBoxLayout()
        foot.addWidget(self._server_footer_dot)
        foot.addWidget(self._server_footer_text, 1)
        side_lay.addLayout(foot)

        outer.addWidget(sidebar)

        main = QWidget()
        main_lay = QVBoxLayout(main)
        main_lay.setContentsMargins(20, 20, 20, 16)
        main_lay.setSpacing(12)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._page_credentials())
        self._stack.addWidget(self._page_search())
        self._stack.addWidget(self._page_select())
        self._stack.addWidget(self._page_download())
        self._stack.addWidget(self._page_serve())
        main_lay.addWidget(self._stack, 1)

        nav = QHBoxLayout()
        self._btn_back = QPushButton("Back")
        self._btn_back.clicked.connect(self._go_back)
        self._btn_next = QPushButton("Next")
        self._btn_next.clicked.connect(self._go_next)
        self._btn_next.setStyleSheet(
            f"QPushButton {{ background-color: {_ACCENT}; color: white; padding: 6px 18px; }}"
            "QPushButton:disabled { background-color: #333; color: #777; }"
        )
        self._btn_done = QPushButton("Done")
        self._btn_done.clicked.connect(self._on_done_keep_server)
        self._btn_done.setStyleSheet(
            f"QPushButton {{ background-color: {_ACCENT}; color: white; padding: 6px 18px; }}"
        )
        self._btn_stop_close = QPushButton("Stop server and close")
        self._btn_stop_close.setStyleSheet(
            "QPushButton { padding: 6px 16px; color: #e57373; border: 1px solid #5c3333; }"
        )
        self._btn_stop_close.clicked.connect(self._on_stop_server_and_close)

        nav.addStretch(1)
        nav.addWidget(self._btn_back)
        nav.addWidget(self._btn_next)
        nav.addWidget(self._btn_done)
        nav.addWidget(self._btn_stop_close)
        main_lay.addLayout(nav)

        outer.addWidget(main, 1)

        self._update_sidebar()
        self._sync_nav()

    def _page_credentials(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(10)
        h = QLabel("Artifactory credentials")
        h.setStyleSheet("font-size: 15px; font-weight: bold;")
        lay.addWidget(h)
        sub = QLabel(
            f"Use your Artifactory base URL and API token. Repo: {ARTIFACTORY_REPO}. "
            "VPN may be required."
        )
        sub.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        self._fld_url = QLineEdit()
        self._fld_url.setPlaceholderText("https://artifactory.arlocloud.com")
        self._fld_user = QLineEdit()
        self._fld_user.setPlaceholderText("Username (if required)")
        self._fld_token = QLineEdit()
        self._fld_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._fld_token.setPlaceholderText("API token / identity token")
        self._fld_url.textChanged.connect(self._on_step0_fields_changed)
        self._fld_user.textChanged.connect(self._on_step0_fields_changed)
        self._fld_token.textChanged.connect(self._on_step0_fields_changed)

        form = QVBoxLayout()
        form.addWidget(QLabel("Artifactory URL"))
        form.addWidget(self._fld_url)
        form.addWidget(QLabel("Username"))
        form.addWidget(self._fld_user)
        form.addWidget(QLabel("API token"))
        form.addWidget(self._fld_token)
        lay.addLayout(form)

        self._chk_save_creds = QCheckBox(f"Save credentials to config ({get_config_path()}) when leaving this step")
        lay.addWidget(self._chk_save_creds)

        row = QHBoxLayout()
        self._btn_test = QPushButton("Test connection")
        self._btn_test.clicked.connect(self._test_credentials)
        self._cred_status = QLabel("")
        self._cred_status.setWordWrap(True)
        row.addWidget(self._btn_test)
        row.addWidget(self._cred_status, 1)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def _page_search(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        h = QLabel("Search firmware")
        h.setStyleSheet("font-size: 15px; font-weight: bold;")
        lay.addWidget(h)

        self._combo_model = QComboBox()
        models = get_models()
        prof = (self._model_dict.get("command_profile") or "").strip()
        for m in models:
            if prof and (m.get("command_profile") or "") != prof:
                continue
            self._combo_model.addItem(m.get("display_name") or m["name"], m)
        if self._combo_model.count() == 0:
            for m in models:
                self._combo_model.addItem(m.get("display_name") or m["name"], m)
        self._combo_model.currentIndexChanged.connect(self._on_model_combo_changed)
        lay.addWidget(QLabel("Camera model group"))
        lay.addWidget(self._combo_model)

        self._pills_host = QWidget()
        self._pills_layout = QHBoxLayout(self._pills_host)
        self._pills_layout.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("Search includes these Artifactory model names:"))
        lay.addWidget(self._pills_host)

        self._lbl_bin_target = QLabel("Artifactory download target (2K / FHD)")
        self._lbl_bin_target.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        lay.addWidget(self._lbl_bin_target)
        self._combo_bin_target = QComboBox()
        self._combo_bin_target.currentIndexChanged.connect(self._on_search_fields_changed)
        lay.addWidget(self._combo_bin_target)
        bin_hint = QLabel(
            "Chooses which Artifactory product folder to download from. Extracted firmware still "
            "goes under binaries/<connected VMC> on the local server."
        )
        bin_hint.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        bin_hint.setWordWrap(True)
        lay.addWidget(bin_hint)

        self._fld_version_filter = QLineEdit()
        self._fld_version_filter.setPlaceholderText("Version filter (leave empty to match broadly)")
        self._fld_version_filter.textChanged.connect(self._on_search_fields_changed)
        lay.addWidget(QLabel("Version filter"))
        lay.addWidget(self._fld_version_filter)

        self._lbl_vmc_bin = QLabel("")
        self._lbl_vmc_bin.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        self._lbl_vmc_bin.setWordWrap(True)
        lay.addWidget(self._lbl_vmc_bin)

        sf_row = QHBoxLayout()
        self._combo_server_folder = QComboBox()
        self._combo_server_folder.setEditable(True)
        self._combo_server_folder.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo_server_folder.currentIndexChanged.connect(self._on_search_fields_changed)
        le_sf = self._combo_server_folder.lineEdit()
        if le_sf is not None:
            le_sf.textChanged.connect(self._on_search_fields_changed)
        sf_row.addWidget(self._combo_server_folder, 1)
        self._btn_rename_folder = QPushButton("Rename…")
        self._btn_rename_folder.setToolTip("Rename an existing folder under the server root")
        self._btn_rename_folder.clicked.connect(self._on_rename_server_folder)
        sf_row.addWidget(self._btn_rename_folder)
        lay.addWidget(QLabel("Server folder (local HTTP path segment)"))
        lay.addLayout(sf_row)
        sf_hint = QLabel(
            "Name for the folder on the local server (e.g. qa, qa1, downgrade, stress-v2). "
            "Pick an existing folder from the list or type a new name to create one. "
            "Different names let you keep multiple firmware trees side by side."
        )
        sf_hint.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        sf_hint.setWordWrap(True)
        lay.addWidget(sf_hint)

        self._search_status = QLabel("")
        self._search_status.setWordWrap(True)
        self._search_status.setStyleSheet(f"color: {_MUTED};")
        lay.addWidget(self._search_status)
        lay.addStretch(1)

        self._sync_model_combo_default()
        self._populate_server_folder_combo()
        self._refresh_vmc_binaries_label()
        return w

    def _page_select(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        h = QLabel("Select firmware build")
        h.setStyleSheet("font-size: 15px; font-weight: bold;")
        lay.addWidget(h)
        hint = QLabel("Choose one row. Version is the Artifactory folder path; Variant is the archive file name.")
        hint.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Version (path)", "Archive", "Size", "Date", "Variant"]
        )
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setSortingEnabled(True)
        self._table.itemSelectionChanged.connect(self._on_table_selection)
        lay.addWidget(self._table)
        return w

    def _page_download(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        h = QLabel("Download & extract")
        h.setStyleSheet("font-size: 15px; font-weight: bold;")
        lay.addWidget(h)

        self._dl_path_label = QLabel("")
        self._dl_path_label.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        self._dl_path_label.setWordWrap(True)
        lay.addWidget(self._dl_path_label)

        self._dl_progress = QProgressBar()
        self._dl_progress.setRange(0, 0)
        self._dl_progress.setTextVisible(True)
        lay.addWidget(self._dl_progress)

        self._dl_status = QLabel("")
        self._dl_status.setWordWrap(True)
        lay.addWidget(self._dl_status)

        row = QHBoxLayout()
        self._btn_retry_dl = QPushButton("Retry download")
        self._btn_retry_dl.clicked.connect(self._start_download)
        self._btn_retry_dl.hide()
        row.addWidget(self._btn_retry_dl)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def _page_serve(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        h = QLabel("Serve & set update URL")
        h.setStyleSheet("font-size: 15px; font-weight: bold;")
        lay.addWidget(h)

        self._url_banner = QLabel("")
        self._url_banner.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._url_banner.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {_ACCENT};")
        self._url_banner.setWordWrap(True)
        lay.addWidget(self._url_banner)

        sub = QLabel(
            "The camera loads firmware from this URL (LAN IP so the device can reach your PC). "
            "The local server keeps running after you close this wizard."
        )
        sub.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        sum_l = QLabel("All firmware folders on this server")
        sum_l.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        lay.addWidget(sum_l)
        self._serve_folder_summary = QPlainTextEdit()
        self._serve_folder_summary.setReadOnly(True)
        self._serve_folder_summary.setMaximumHeight(96)
        self._serve_folder_summary.setPlaceholderText("Scanning folders…")
        sum_mono = QFont("Menlo", 9) if os.name != "nt" else QFont("Consolas", 9)
        self._serve_folder_summary.setFont(sum_mono)
        lay.addWidget(self._serve_folder_summary)

        self._step5_log = QPlainTextEdit()
        self._step5_log.setReadOnly(True)
        self._step5_log.setMinimumHeight(120)
        self._step5_log.setMaximumHeight(160)
        self._step5_log.setPlaceholderText("Command output appears here…")
        mono = QFont("Menlo", 9) if os.name != "nt" else QFont("Consolas", 9)
        self._step5_log.setFont(mono)
        lay.addWidget(self._step5_log)

        self._btn_push = QPushButton("Set update URL")
        self._btn_push.setStyleSheet(
            f"QPushButton {{ background-color: {_ACCENT}; color: white; padding: 8px 20px; }}"
        )
        self._btn_push.clicked.connect(self._push_update_url)
        lay.addWidget(self._btn_push)

        self._panel_factory = QWidget()
        fl = QVBoxLayout(self._panel_factory)
        fl.setContentsMargins(0, 8, 0, 0)
        fl.setSpacing(8)
        self._lbl_factory_hint = QLabel(
            "This camera is not onboarded. After the update URL is set, ArloShell sends a reboot "
            "automatically so the device picks up firmware from the new URL on boot."
        )
        self._lbl_factory_hint.setWordWrap(True)
        self._lbl_factory_hint.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        fl.addWidget(self._lbl_factory_hint)
        lay.addWidget(self._panel_factory)
        self._panel_factory.hide()

        self._panel_onboarded = QWidget()
        ol = QVBoxLayout(self._panel_onboarded)
        ol.setContentsMargins(0, 8, 0, 0)
        ol.setSpacing(8)
        self._lbl_onboarded_hint = QLabel(
            "This camera is onboarded — firmware is staged on your local server. "
            "Use the Arlo app to trigger an update check, or press the button below to check from the device now."
        )
        self._lbl_onboarded_hint.setWordWrap(True)
        self._lbl_onboarded_hint.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        ol.addWidget(self._lbl_onboarded_hint)
        self._btn_trigger_refresh = QPushButton("Trigger update check")
        self._btn_trigger_refresh.setStyleSheet(
            f"QPushButton {{ background-color: #3949ab; color: #e8eaf6; padding: 8px 16px; }}"
        )
        self._btn_trigger_refresh.clicked.connect(self._on_trigger_update_refresh)
        ol.addWidget(self._btn_trigger_refresh)
        lay.addWidget(self._panel_onboarded)
        self._panel_onboarded.hide()

        lay.addStretch(1)
        return w

    def _load_config_into_step1(self) -> None:
        try:
            config = load_config_file()
        except ValueError:
            config = None
        if not config:
            self._fld_url.setText(self._base_url or default_artifactory_url())
            return
        art = config["artifactory"]
        self._fld_url.setText((art.get("base_url") or "").strip() or default_artifactory_url())
        self._fld_user.setText((art.get("username") or "").strip())
        try:
            tok = decode_token(art.get("access_token") or "")
        except Exception:
            tok = ""
        self._fld_token.setText(tok or "")

    def _sync_model_combo_default(self) -> None:
        name_u = self._primary_model_name.upper()
        for i in range(self._combo_model.count()):
            m = self._combo_model.itemData(i)
            if not isinstance(m, dict):
                continue
            if (m.get("name") or "").upper() == name_u:
                self._combo_model.setCurrentIndex(i)
                break
        self._on_model_combo_changed()

    def _on_model_combo_changed(self) -> None:
        m = self._combo_model.currentData()
        if not isinstance(m, dict):
            return
        self._primary_model_name = (m.get("name") or "Camera").strip()
        self._fw_search_models = list(m.get("fw_search_models") or [self._primary_model_name])
        while self._pills_layout.count():
            item = self._pills_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for tag in self._fw_search_models:
            pill = QLabel(tag)
            pill.setStyleSheet(
                "background-color: #252525; color: #ccc; border-radius: 10px; padding: 4px 10px; font-size: 11px;"
            )
            self._pills_layout.addWidget(pill)
        self._pills_layout.addStretch(1)

        self._combo_bin_target.blockSignals(True)
        self._combo_bin_target.clear()
        for tag in self._fw_search_models:
            tu = tag.upper()
            if re.match(r"^VMC3\d{3}$", tu):
                disp = f"{tag} (2K)"
            elif re.match(r"^VMC2\d{3}$", tu):
                disp = f"{tag} (FHD)"
            else:
                disp = tag
            self._combo_bin_target.addItem(disp, tag)
        self._combo_bin_target.blockSignals(False)
        multi = self._combo_bin_target.count() > 1
        self._lbl_bin_target.setVisible(multi)
        self._combo_bin_target.setVisible(multi)
        if self._combo_bin_target.count() > 0:
            name_u = self._vmc_binaries_folder_name().upper()
            idx = 0
            for i in range(self._combo_bin_target.count()):
                d = self._combo_bin_target.itemData(i)
                if isinstance(d, str) and d.upper() == name_u:
                    idx = i
                    break
            self._combo_bin_target.setCurrentIndex(idx)

        if self._current_step == 1:
            self._sync_nav()
        self._refresh_vmc_binaries_label()

    def _vmc_binaries_folder_name(self) -> str:
        n = (self._primary_model_name or "").strip().upper()
        if re.match(r"^VMC\d{4}$", n):
            return n
        return (self._primary_model_name or "Camera").strip() or "Camera"

    def _refresh_vmc_binaries_label(self) -> None:
        v = self._vmc_binaries_folder_name()
        self._lbl_vmc_bin.setText(
            f"Firmware for this camera is stored under …/binaries/{v}/ on the local server "
            "(from the connected model; not configurable)."
        )

    def _populate_server_folder_combo(self) -> None:
        cur = (self._combo_server_folder.currentText() or "").strip()
        self._combo_server_folder.blockSignals(True)
        self._combo_server_folder.clear()
        for name in list_environment_folders(self._fw_root):
            self._combo_server_folder.addItem(name)
        self._combo_server_folder.blockSignals(False)
        if cur:
            idx = -1
            c_low = cur.lower()
            for i in range(self._combo_server_folder.count()):
                if self._combo_server_folder.itemText(i).lower() == c_low:
                    idx = i
                    break
            if idx >= 0:
                self._combo_server_folder.setCurrentIndex(idx)
            else:
                self._combo_server_folder.setEditText(cur)
        self._on_search_fields_changed()

    def _current_server_folder_input(self) -> str:
        return (self._combo_server_folder.currentText() or "").strip()

    def _on_rename_server_folder(self) -> None:
        old_name = self._current_server_folder_input()
        if not sanitize_server_folder_name(old_name):
            QMessageBox.information(
                self,
                "Rename folder",
                "Enter or select a valid folder name first.",
            )
            return
        old_path = os.path.join(self._fw_root, old_name)
        if not os.path.isdir(old_path):
            QMessageBox.information(
                self,
                "Rename folder",
                "Rename only applies to folders that already exist under the server root.",
            )
            return
        new_name, ok = QInputDialog.getText(
            self,
            "Rename folder",
            "New folder name:",
            text=old_name,
        )
        if not ok:
            return

        blocked = firmware_folder_rename_blocked_reason(old_path)
        stopped_for_rename = False
        if blocked:
            if "this window" in blocked.lower():
                r = QMessageBox.question(
                    self,
                    "Rename folder",
                    blocked
                    + "\n\nStop the firmware server in this window now so the folder can be renamed?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if r != QMessageBox.StandardButton.Yes:
                    return
                stop_ok, stop_msg = stop_http_server()
                if not stop_ok:
                    QMessageBox.warning(
                        self,
                        "Rename folder",
                        f"Could not stop the server: {stop_msg}\n\nStop it manually (e.g. server stop), then try rename again.",
                    )
                    return
                stopped_for_rename = True
                self._refresh_server_footer()
            else:
                QMessageBox.warning(self, "Rename folder", blocked)
                return

        ok_r, err = rename_server_folder(self._fw_root, old_name, new_name)
        if not ok_r:
            detail = err or "Rename failed."
            if (
                "Access is denied" in detail
                or "WinError 5" in detail
                or "Errno 13" in detail
                or "permission denied" in detail.lower()
            ):
                detail += "\n\n" + firmware_rename_access_denied_user_hint()
            QMessageBox.warning(self, "Rename folder", detail)
            return
        self._populate_server_folder_combo()
        self._combo_server_folder.setEditText(new_name)
        if stopped_for_rename:
            QMessageBox.information(
                self,
                "Rename folder",
                f"Renamed to “{new_name}”. The local firmware server was stopped; start it again "
                "when you are ready (FW Setup final step or fw local).",
            )

    def _append_step5_log(self, line: str) -> None:
        t = (line or "").rstrip()
        if t:
            self._step5_log.appendPlainText(t)

    def _clear_step5_log(self) -> None:
        self._step5_log.clear()

    def _update_sidebar(self) -> None:
        for i, lab in enumerate(self._step_labels):
            if i < self._current_step:
                lab.setText("✓")
                lab.setStyleSheet(f"color: {_OK}; font-weight: bold; font-size: 13px;")
            elif i == self._current_step:
                lab.setText(str(i + 1))
                lab.setStyleSheet(
                    f"color: white; font-weight: bold; background-color: {_ACCENT}; "
                    "border-radius: 11px; min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;"
                )
            else:
                lab.setText(str(i + 1))
                lab.setStyleSheet(f"color: {_MUTED}; font-weight: normal;")

    def _step0_fields_valid(self) -> bool:
        url = (self._fld_url.text() or "").strip()
        tok = (self._fld_token.text() or "").strip()
        if not tok:
            return False
        u = url.lower()
        return bool(url and (u.startswith("http://") or u.startswith("https://")))

    def _step1_server_folder_valid(self) -> bool:
        return sanitize_server_folder_name(self._current_server_folder_input()) is not None

    def _apply_credentials_from_step0(self) -> None:
        raw_url = (self._fld_url.text() or "").strip() or default_artifactory_url()
        self._base_url = raw_url.rstrip("/")
        self._token = (self._fld_token.text() or "").strip()
        u = (self._fld_user.text() or "").strip()
        self._username = u or None
        if self._chk_save_creds.isChecked():
            try:
                save_config_file(u, self._token, self._base_url, ARTIFACTORY_REPO)
                update_last_used()
            except OSError as e:
                QMessageBox.warning(self, "Firmware setup", f"Could not save credentials: {e}")

    def _sync_nav(self) -> None:
        self._btn_back.setVisible(self._current_step > 0 and self._current_step < 4)
        step = self._current_step
        self._btn_next.setVisible(step < 4)
        self._btn_done.setVisible(step == 4)
        self._btn_stop_close.setVisible(step == 4)

        if step == 0:
            self._btn_next.setEnabled(self._step0_fields_valid())
            self._btn_next.setText("Next")
        elif step == 1:
            self._btn_next.setEnabled(not self._search_busy and self._step1_server_folder_valid())
            self._btn_next.setText("Searching…" if self._search_busy else "Next")
        elif step == 2:
            self._btn_next.setEnabled(self._selected_filename is not None)
            self._btn_next.setText("Next")
        elif step == 3:
            self._btn_next.setEnabled(False)
            self._btn_next.setText("Next")

    def _go_back(self) -> None:
        if self._current_step <= 0:
            return
        if self._current_step >= 4:
            return
        if self._current_step == 3 and self._download_thread and self._download_thread.isRunning():
            return
        if self._current_step == 2:
            self._search_results = []
            self._fill_results_table()
            self._selected_filename = None
            self._selected_folder = None
            self._search_status.setText("")
            self._search_status.setStyleSheet(f"color: {_MUTED};")
        self._current_step -= 1
        self._stack.setCurrentIndex(self._current_step)
        self._update_sidebar()
        self._sync_nav()

    def _go_next(self) -> None:
        if self._current_step >= 4:
            return
        if self._current_step == 0:
            if not self._step0_fields_valid():
                return
            self._apply_credentials_from_step0()
            self._current_step = 1
            self._stack.setCurrentIndex(1)
            self._update_sidebar()
            self._sync_nav()
            return
        if self._current_step == 1:
            self._start_search_from_next()
            return
        if self._current_step == 2:
            if not self._selected_filename:
                return
            if not self._server_folder_name:
                self._server_folder_name = (
                    sanitize_server_folder_name(self._current_server_folder_input()) or ""
                )
            self._prepare_download_page()
            self._current_step = 3
            self._stack.setCurrentIndex(3)
            self._update_sidebar()
            self._sync_nav()
            self._start_download()
            return

    def _on_step0_fields_changed(self, *_args: object) -> None:
        if self._current_step == 0:
            self._sync_nav()

    def _on_search_fields_changed(self, *_args: object) -> None:
        if self._current_step == 1:
            self._sync_nav()

    def _on_done_keep_server(self) -> None:
        self._skip_server_close_dialog = True
        self.close()

    def _on_stop_server_and_close(self) -> None:
        stop_http_server()
        self._refresh_server_footer()
        self._skip_server_close_dialog = True
        self.close()

    def _test_credentials(self) -> None:
        url = (self._fld_url.text() or "").strip() or default_artifactory_url()
        token = (self._fld_token.text() or "").strip()
        user = (self._fld_user.text() or "").strip() or None
        self._btn_test.setEnabled(False)
        self._cred_status.setText("Testing…")
        self._cred_status.setStyleSheet(f"color: {_MUTED};")

        ok, err = test_artifactory_access(url, token, user)
        self._btn_test.setEnabled(True)
        if ok:
            self._cred_status.setText("Connection OK (optional — Next does not require this).")
            self._cred_status.setStyleSheet(f"color: {_OK};")
            if self._chk_save_creds.isChecked():
                try:
                    save_config_file(user or "", token, url, ARTIFACTORY_REPO)
                    update_last_used()
                    self._cred_status.setText("Connection OK. Credentials saved.")
                except OSError as e:
                    self._cred_status.setText(f"Connection OK (could not save config: {e})")
        else:
            self._cred_status.setText(err or "Connection failed.")
            self._cred_status.setStyleSheet(f"color: {_ERR};")
        self._sync_nav()

    def _start_search_from_next(self) -> None:
        if self._search_thread and self._search_thread.isRunning():
            return
        folder = sanitize_server_folder_name(self._current_server_folder_input())
        if not folder:
            QMessageBox.warning(
                self,
                "Firmware setup",
                "Enter a valid server folder name (letters, numbers, dash, underscore; no slashes).",
            )
            return
        existing_path = os.path.join(self._fw_root, folder)
        if os.path.isdir(existing_path) and folder_has_firmware_artifacts(existing_path):
            r = QMessageBox.warning(
                self,
                "Firmware setup",
                f'Folder "{folder}" already contains firmware (archive, binaries, or updaterules). '
                "Continuing can overwrite files in that tree.\n\nProceed with search and download?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        self._server_folder_name = folder
        self._search_busy = True
        self._sync_nav()
        self._search_status.setText("Searching Artifactory…")
        self._search_status.setStyleSheet(f"color: {_MUTED};")
        vf = (self._fld_version_filter.text() or "").strip()
        self._search_thread = _SearchThread(
            self._base_url, self._token, self._username, vf, self._fw_search_models
        )
        self._search_thread.finished_search.connect(self._on_search_done)
        self._search_thread.start()

    def _on_search_done(self, ok: bool, flat: object, err: str) -> None:
        self._search_busy = False
        rows = flat if isinstance(flat, list) else []
        if not ok:
            self._search_results = []
            self._search_status.setText(err or "Search failed.")
            self._search_status.setStyleSheet(f"color: {_ERR};")
            self._sync_nav()
            return
        self._search_results = [(str(a), str(b)) for a, b in rows]
        if not self._search_results:
            self._search_status.setText(
                "No matching firmware archives (.zip or env .tar.gz). "
                "Adjust the version filter and press Next again."
            )
            self._search_status.setStyleSheet(f"color: {_ERR};")
            self._sync_nav()
            return
        self._search_status.setText(f"{len(self._search_results)} match(es).")
        self._search_status.setStyleSheet(f"color: {_OK};")
        self._fill_results_table()
        self._current_step = 2
        self._stack.setCurrentIndex(2)
        self._update_sidebar()
        self._sync_nav()

    def _fill_results_table(self) -> None:
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        for folder, fn in self._search_results:
            r = self._table.rowCount()
            self._table.insertRow(r)
            self._table.setItem(r, 0, QTableWidgetItem(folder))
            self._table.setItem(r, 1, QTableWidgetItem(fn))
            self._table.setItem(r, 2, QTableWidgetItem("—"))
            self._table.setItem(r, 3, QTableWidgetItem("—"))
            ext = fn.lower().split(".")[-1] if "." in fn else fn
            self._table.setItem(r, 4, QTableWidgetItem(ext))
        self._table.setSortingEnabled(True)
        self._selected_filename = None
        self._selected_folder = None

    def _on_table_selection(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            self._selected_filename = None
            self._selected_folder = None
            self._sync_nav()
            return
        r = rows[0].row()
        folder_item = self._table.item(r, 0)
        file_item = self._table.item(r, 1)
        if not folder_item or not file_item:
            return
        self._selected_folder = folder_item.text()
        self._selected_filename = file_item.text()
        self._version_path = self._selected_folder or ""
        self._sync_nav()

    def _prepare_download_page(self) -> None:
        path = os.path.join(self._fw_root, self._server_folder_name)
        vmc = self._vmc_binaries_folder_name()
        self._dl_path_label.setText(
            f"FW server root: {self._fw_root}\n"
            f"Server folder: {path}  (archive/, binaries/{vmc}/, updaterules/)"
        )

    def _start_download(self) -> None:
        if self._download_thread and self._download_thread.isRunning():
            return
        folder = self._server_folder_name or sanitize_server_folder_name(
            self._current_server_folder_input()
        )
        if not folder:
            self._dl_status.setText("Enter a valid server folder name.")
            self._dl_status.setStyleSheet(f"color: {_ERR};")
            self._btn_retry_dl.show()
            return
        self._btn_retry_dl.hide()
        self._dl_progress.setRange(0, 0)
        self._dl_status.setText("")
        self._dl_status.setStyleSheet("")

        vmc = self._vmc_binaries_folder_name()
        ok_setup, msg_or_env, binaries_base, _pb, updaterules_dir, archive_dir = prepare_env_directories(
            self._fw_root, folder, vmc, self._fw_search_models
        )
        if not ok_setup:
            self._dl_status.setText(msg_or_env)
            self._dl_status.setStyleSheet(f"color: {_ERR};")
            self._btn_retry_dl.show()
            return

        art_target = self._combo_bin_target.currentData()
        if not isinstance(art_target, str) or not art_target.strip():
            art_target = self._primary_model_name
        download_model = compute_download_model(
            self._version_path, self._selected_filename, art_target.strip()
        )
        binaries_dir_for_download = os.path.join(msg_or_env, "binaries", vmc)
        chosen_binaries_dir = os.path.abspath(os.path.join(binaries_base, vmc))
        archive_path = os.path.abspath(os.path.join(archive_dir, self._selected_filename or ""))
        rules_dir = os.path.abspath(updaterules_dir)

        self._download_thread = _DownloadThread(
            self._token,
            download_model,
            self._version_path,
            binaries_dir_for_download,
            updaterules_dir,
            archive_dir,
            self._base_url,
            self._username,
            self._selected_filename,
            archive_path,
            chosen_binaries_dir,
            rules_dir,
        )
        self._download_thread.byte_progress.connect(self._on_dl_bytes)
        self._download_thread.status_text.connect(self._on_dl_status)
        self._download_thread.finished_ok.connect(self._on_dl_ok)
        self._download_thread.failed.connect(self._on_dl_failed)
        self._download_thread.start()

    def _on_dl_bytes(self, done: int, total: int | None) -> None:
        if total and total > 0:
            self._dl_progress.setRange(0, int(total))
            self._dl_progress.setValue(min(done, int(total)))
        else:
            self._dl_progress.setRange(0, 0)

    def _on_dl_status(self, text: str) -> None:
        self._dl_status.setText(text)

    def _on_dl_ok(self) -> None:
        self._dl_progress.setRange(0, 100)
        self._dl_progress.setValue(100)
        self._dl_status.setText("Download and extraction complete.")
        self._dl_status.setStyleSheet(f"color: {_OK};")
        QTimer.singleShot(400, self._auto_advance_serve)

    def _on_dl_failed(self, msg: str) -> None:
        self._dl_status.setText(msg)
        self._dl_status.setStyleSheet(f"color: {_ERR};")
        self._btn_retry_dl.show()

    def _auto_advance_serve(self) -> None:
        if self._current_step != 3:
            return
        self._current_step = 4
        self._stack.setCurrentIndex(4)
        self._update_sidebar()
        self._sync_nav()
        self._enter_serve_step()

    def _enter_serve_step(self) -> None:
        self._panel_factory.hide()
        self._panel_onboarded.hide()
        self._clear_step5_log()
        folder = self._server_folder_name.strip()
        ok, err, cam_url = ensure_server_and_camera_url(self._fw_root, folder)
        if not ok:
            self._url_banner.setText("Could not start or read server.")
            self._append_step5_log(err or "Server error.")
            self._btn_push.setEnabled(False)
            return
        self._camera_url = cam_url
        self._url_banner.setText(cam_url)
        self._btn_push.setEnabled(True)
        self._btn_push.setVisible(True)
        vmc = self._vmc_binaries_folder_name()
        rows = scan_firmware_folders_with_versions(self._fw_root, vmc)
        lines = "\n".join(f"  • {n}  —  {v}" for n, v in rows)
        self._serve_folder_summary.setPlainText(
            f"Server root: {self._fw_root}\nFolders for {vmc}:\n{lines if lines else '  (none found)'}"
        )
        self.server_started.emit(cam_url)
        self._refresh_server_footer()

    def _push_update_url(self) -> None:
        if not self._camera_url:
            return
        self._btn_push.setEnabled(False)
        self._append_step5_log("Sending arlocmd update_url…")

        def done(ok: bool, msg: str) -> None:
            self._btn_push.setEnabled(True)
            if ok:
                self._append_step5_log("arlocmd update_url: OK")
                self.update_sent.emit(True)
                self._btn_push.setEnabled(False)
                self._finish_step5_after_update_url_success()
            else:
                self._append_step5_log(msg or "Command failed.")
                self.update_sent.emit(False)

        self._shell_async("arlocmd update_url", [self._camera_url], done)

    def _finish_step5_after_update_url_success(self) -> None:
        """Onboarded: app/update_refresh UI. Not onboarded: auto-reboot and show both results."""
        self._panel_factory.hide()
        self._panel_onboarded.hide()
        if self._is_onboarded is True:
            self._panel_onboarded.show()
            return
        self._panel_factory.show()
        self._append_step5_log("Sending arlocmd reboot…")

        def reboot_done(ok_r: bool, msg_r: str) -> None:
            if ok_r:
                self._append_step5_log("arlocmd reboot: OK")
            else:
                self._append_step5_log("arlocmd reboot failed: " + (msg_r or "error"))

        self._shell_async("arlocmd reboot", [], reboot_done)

    def _on_trigger_update_refresh(self) -> None:
        self._btn_trigger_refresh.setEnabled(False)
        self._append_step5_log("Running arlocmd update_refresh 1…")

        def done(ok: bool, msg: str) -> None:
            self._btn_trigger_refresh.setEnabled(True)
            if ok:
                self._append_step5_log((msg or "OK").strip() or "update_refresh completed.")
            else:
                self._append_step5_log(msg or "update_refresh failed.")

        self._shell_async("arlocmd update_refresh", ["1"], done)

    def _refresh_server_footer(self) -> None:
        hint, line, tooltip = firmware_server_listener_summary()
        if hint == "green":
            self._server_footer_dot.setStyleSheet(f"color: {_OK}; font-size: 14px;")
        elif hint == "amber":
            self._server_footer_dot.setStyleSheet(f"color: {_AMBER}; font-size: 14px;")
        else:
            self._server_footer_dot.setStyleSheet(f"color: {_MUTED}; font-size: 14px;")
        self._server_footer_text.setText(line)
        self._server_footer_text.setToolTip(tooltip)
        self._server_footer_dot.setToolTip(tooltip)
