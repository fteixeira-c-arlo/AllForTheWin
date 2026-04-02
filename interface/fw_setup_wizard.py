"""GUI wizard: Artifactory firmware setup, local server, camera update_url."""
from __future__ import annotations

import os
from typing import Any, Callable

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.artifactory_client import ARTIFACTORY_REPO, test_artifactory_access
from core.fw_setup_service import (
    compute_download_model,
    default_artifactory_url,
    default_fw_server_root,
    download_firmware_to_layout,
    ensure_server_and_camera_url,
    extract_firmware_archive,
    list_environment_folders,
    prepare_env_directories,
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
_MUTED = "#9e9e9e"
_SIDEBAR_BG = "#0d0d0d"
_BORDER = "#1e1e1e"


ShellAsyncFn = Callable[[str, list[str], Callable[[bool, str], None]], None]


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
        self.resize(920, 560)
        self.setModal(False)

        self._model_dict = dict(model_dict)
        self._shell_async = shell_async

        self._base_url = default_artifactory_url()
        self._username: str | None = None
        self._token = ""
        self._credentials_ok = False
        self._save_creds_after_test = False

        self._fw_root = default_fw_server_root()
        self._search_results: list[tuple[str, str]] = []
        self._selected_folder: str | None = None
        self._selected_filename: str | None = None
        self._version_path: str = ""
        self._env_name = ""
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
        self.wizard_closed.emit()
        super().closeEvent(event)

    def _build_ui(self) -> None:
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
                line = QFrame()
                line.setFixedHeight(14)
                line.setFrameShape(QFrame.Shape.NoFrame)
                side_lay.addWidget(line)

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
        nav.addStretch(1)
        nav.addWidget(self._btn_back)
        nav.addWidget(self._btn_next)
        main_lay.addLayout(nav)

        outer.addWidget(main, 1)

        self._current_step = 0
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

        form = QVBoxLayout()
        form.addWidget(QLabel("Artifactory URL"))
        form.addWidget(self._fld_url)
        form.addWidget(QLabel("Username"))
        form.addWidget(self._fld_user)
        form.addWidget(QLabel("API token"))
        form.addWidget(self._fld_token)
        lay.addLayout(form)

        self._chk_save_creds = QCheckBox(f"Save credentials to {get_config_path()} after successful test")
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

        self._fld_version_filter = QLineEdit()
        self._fld_version_filter.setPlaceholderText("Version filter (leave empty to match broadly)")
        lay.addWidget(QLabel("Version filter"))
        lay.addWidget(self._fld_version_filter)

        self._combo_env = QComboBox()
        lay.addWidget(QLabel("Local server environment folder"))
        lay.addWidget(self._combo_env)

        self._combo_binary = QComboBox()
        lay.addWidget(QLabel("Binary target (extract .enc into this folder under env/binaries/)"))
        lay.addWidget(self._combo_binary)

        self._search_status = QLabel("")
        self._search_status.setWordWrap(True)
        self._search_status.setStyleSheet(f"color: {_MUTED};")

        btn_row = QHBoxLayout()
        self._btn_search = QPushButton("Search Artifactory")
        self._btn_search.clicked.connect(self._run_search)
        btn_row.addWidget(self._btn_search)
        btn_row.addWidget(self._search_status, 1)
        lay.addLayout(btn_row)
        lay.addStretch(1)

        self._sync_model_combo_default()
        self._populate_env_combo()
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

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Version (path)", "Archive", "Variant"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
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
        h = QLabel("Serve & push to camera")
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

        self._push_status = QLabel("")
        self._push_status.setWordWrap(True)
        lay.addWidget(self._push_status)

        self._btn_push = QPushButton("Push to camera")
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
            "This camera is not onboarded. Reboot it so it picks up the staged firmware on boot."
        )
        self._lbl_factory_hint.setWordWrap(True)
        self._lbl_factory_hint.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        fl.addWidget(self._lbl_factory_hint)
        self._btn_reboot = QPushButton("Reboot camera")
        self._btn_reboot.setStyleSheet("QPushButton { padding: 8px 16px; }")
        self._btn_reboot.clicked.connect(self._on_reboot_camera)
        fl.addWidget(self._btn_reboot)
        self._reboot_status = QLabel("")
        self._reboot_status.setWordWrap(True)
        fl.addWidget(self._reboot_status)
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
        self._refresh_result = QLabel("")
        self._refresh_result.setWordWrap(True)
        ol.addWidget(self._refresh_result)
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

        self._combo_binary.clear()
        for tag in self._fw_search_models:
            self._combo_binary.addItem(tag)

    def _populate_env_combo(self) -> None:
        self._combo_env.clear()
        for name in list_environment_folders(self._fw_root):
            self._combo_env.addItem(name)
        if self._combo_env.count() == 0:
            self._combo_env.addItem("(create folders under FW root first)")

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

    def _sync_nav(self) -> None:
        self._btn_back.setVisible(self._current_step > 0)
        step = self._current_step
        if step == 0:
            self._btn_next.setEnabled(self._credentials_ok)
        elif step == 1:
            self._btn_next.setEnabled(len(self._search_results) > 0)
        elif step == 2:
            self._btn_next.setEnabled(self._selected_filename is not None)
        elif step == 3:
            self._btn_next.setEnabled(False)
        elif step == 4:
            self._btn_next.setEnabled(False)
        self._btn_next.setVisible(step < 4)

    def _go_back(self) -> None:
        if self._current_step <= 0:
            return
        if self._current_step == 3 and self._download_thread and self._download_thread.isRunning():
            return
        self._current_step -= 1
        self._stack.setCurrentIndex(self._current_step)
        self._update_sidebar()
        self._sync_nav()

    def _go_next(self) -> None:
        if self._current_step >= 4:
            return
        if self._current_step == 2:
            if not self._selected_filename:
                return
            self._prepare_download_page()
            self._env_name = (self._combo_env.currentText() or "").strip()
            self._current_step = 3
            self._stack.setCurrentIndex(3)
            self._update_sidebar()
            self._sync_nav()
            self._start_download()
            return
        if self._current_step == 1 and not self._search_results:
            return
        self._current_step += 1
        self._stack.setCurrentIndex(self._current_step)
        self._update_sidebar()
        self._sync_nav()

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
            self._base_url = url
            self._token = token
            self._username = user
            self._credentials_ok = True
            self._cred_status.setText("Connection OK. You can continue.")
            self._cred_status.setStyleSheet(f"color: {_OK};")
            if self._chk_save_creds.isChecked():
                try:
                    save_config_file(user or "", token, url, ARTIFACTORY_REPO)
                    update_last_used()
                    self._cred_status.setText("Connection OK. Credentials saved.")
                except OSError as e:
                    self._cred_status.setText(f"Connection OK (could not save config: {e})")
        else:
            self._credentials_ok = False
            self._cred_status.setText(err or "Connection failed.")
            self._cred_status.setStyleSheet(f"color: {_ERR};")
        self._sync_nav()

    def _run_search(self) -> None:
        if not self._credentials_ok:
            QMessageBox.warning(self, "FW Setup", "Test Artifactory connection first.")
            return
        env_txt = self._combo_env.currentText()
        if not env_txt or env_txt.startswith("("):
            QMessageBox.warning(
                self,
                "FW Setup",
                f"No environment folders under:\n{self._fw_root}\n\nCreate e.g. qa, dev, prod and retry.",
            )
            return
        self._env_name = env_txt
        self._btn_search.setEnabled(False)
        self._search_status.setText("Searching…")
        vf = (self._fld_version_filter.text() or "").strip()
        self._search_thread = _SearchThread(
            self._base_url, self._token, self._username, vf, self._fw_search_models
        )
        self._search_thread.finished_search.connect(self._on_search_done)
        self._search_thread.start()

    def _on_search_done(self, ok: bool, flat: object, err: str) -> None:
        self._btn_search.setEnabled(True)
        rows = flat if isinstance(flat, list) else []
        if not ok:
            self._search_results = []
            self._search_status.setText(err or "Search failed.")
            self._search_status.setStyleSheet(f"color: {_ERR};")
            self._sync_nav()
            return
        self._search_results = [(str(a), str(b)) for a, b in rows]
        self._search_status.setText(f"{len(self._search_results)} match(es).")
        self._search_status.setStyleSheet(f"color: {_OK};")
        self._fill_results_table()
        self._sync_nav()

    def _fill_results_table(self) -> None:
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        for folder, fn in self._search_results:
            r = self._table.rowCount()
            self._table.insertRow(r)
            self._table.setItem(r, 0, QTableWidgetItem(folder))
            self._table.setItem(r, 1, QTableWidgetItem(fn))
            ext = fn.lower().split(".")[-1] if "." in fn else fn
            self._table.setItem(r, 2, QTableWidgetItem(ext))
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
        env_lower = self._env_name.lower()
        path = os.path.join(self._fw_root, env_lower)
        self._dl_path_label.setText(
            f"FW server root: {self._fw_root}\n"
            f"Environment: {path}  (archive/, binaries/, updaterules/)"
        )

    def _start_download(self) -> None:
        if self._download_thread and self._download_thread.isRunning():
            return
        self._env_name = (self._combo_env.currentText() or "").strip()
        if not self._env_name or self._env_name.startswith("("):
            self._dl_status.setText("Select a valid environment folder first.")
            self._dl_status.setStyleSheet(f"color: {_ERR};")
            self._btn_retry_dl.show()
            return
        self._btn_retry_dl.hide()
        self._dl_progress.setRange(0, 0)
        self._dl_status.setText("")
        self._dl_status.setStyleSheet("")

        ok_setup, msg_or_env, binaries_base, _pb, updaterules_dir, archive_dir = prepare_env_directories(
            self._fw_root, self._env_name, self._primary_model_name, self._fw_search_models
        )
        if not ok_setup:
            self._dl_status.setText(msg_or_env)
            self._dl_status.setStyleSheet(f"color: {_ERR};")
            self._btn_retry_dl.show()
            return

        download_model = compute_download_model(
            self._version_path, self._selected_filename, self._primary_model_name
        )
        binaries_dir_for_download = os.path.join(msg_or_env, "binaries", download_model)
        chosen_name = self._combo_binary.currentText()
        chosen_binaries_dir = os.path.abspath(os.path.join(binaries_base, chosen_name))
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
        self._reboot_status.clear()
        self._refresh_result.clear()
        env_lower = self._env_name.lower()
        ok, err, cam_url = ensure_server_and_camera_url(self._fw_root, env_lower)
        if not ok:
            self._url_banner.setText("Could not start or read server.")
            self._push_status.setText(err)
            self._push_status.setStyleSheet(f"color: {_ERR};")
            self._btn_push.setEnabled(False)
            return
        self._camera_url = cam_url
        self._url_banner.setText(cam_url)
        self._push_status.setText("")
        self._btn_push.setEnabled(True)
        self._btn_push.setVisible(True)
        self.server_started.emit(cam_url)
        self._refresh_server_footer()

    def _push_update_url(self) -> None:
        if not self._camera_url:
            return
        self._btn_push.setEnabled(False)
        self._push_status.setText("Sending arlocmd update_url…")
        self._push_status.setStyleSheet(f"color: {_MUTED};")

        def done(ok: bool, msg: str) -> None:
            self._btn_push.setEnabled(True)
            if ok:
                self._push_status.setText("Camera acknowledged update URL.")
                self._push_status.setStyleSheet(f"color: {_OK};")
                self.update_sent.emit(True)
                self._btn_push.setEnabled(False)
                self._show_step5_after_update_url_success()
            else:
                self._push_status.setText(msg or "Command failed.")
                self._push_status.setStyleSheet(f"color: {_ERR};")
                self.update_sent.emit(False)

        self._shell_async("arlocmd update_url", [self._camera_url], done)

    def _show_step5_after_update_url_success(self) -> None:
        """Branch step 5: reboot path (factory / unknown) vs onboarded + update_refresh."""
        self._panel_factory.hide()
        self._panel_onboarded.hide()
        self._reboot_status.clear()
        self._refresh_result.clear()
        if self._is_onboarded is True:
            self._panel_onboarded.show()
        else:
            self._panel_factory.show()

    def _on_trigger_update_refresh(self) -> None:
        self._btn_trigger_refresh.setEnabled(False)
        self._refresh_result.setText("Running arlocmd update_refresh 1…")
        self._refresh_result.setStyleSheet(f"color: {_MUTED};")

        def done(ok: bool, msg: str) -> None:
            self._btn_trigger_refresh.setEnabled(True)
            if ok:
                self._refresh_result.setText(
                    (msg or "OK").strip() or "update_refresh completed."
                )
                self._refresh_result.setStyleSheet(f"color: {_OK};")
            else:
                self._refresh_result.setText(msg or "update_refresh failed.")
                self._refresh_result.setStyleSheet(f"color: {_ERR};")

        self._shell_async("arlocmd update_refresh", ["1"], done)

    def _on_reboot_camera(self) -> None:
        self._btn_reboot.setEnabled(False)
        self._reboot_status.setText("Sending arlocmd reboot…")
        self._reboot_status.setStyleSheet(f"color: {_MUTED};")

        def done(ok: bool, msg: str) -> None:
            self._btn_reboot.setEnabled(True)
            if ok:
                self._reboot_status.setText(
                    (msg or "Reboot command sent.").strip() or "Reboot command sent."
                )
                self._reboot_status.setStyleSheet(f"color: {_OK};")
            else:
                self._reboot_status.setText(msg or "Reboot failed.")
                self._reboot_status.setStyleSheet(f"color: {_ERR};")

        self._shell_async("arlocmd reboot", [], done)

    def _refresh_server_footer(self) -> None:
        from core.local_server import DEFAULT_PORT, check_server_status, get_running_server_url

        running, msg = check_server_status()
        if running:
            ok, url = get_running_server_url()
            if ok and url:
                part = url.replace("http://", "").replace("https://", "").strip("/")
                port = part.split(":")[-1] if ":" in part else str(DEFAULT_PORT)
                self._server_footer_dot.setStyleSheet(f"color: {_OK}; font-size: 14px;")
                self._server_footer_text.setText(f"Server running · port {port}")
            else:
                self._server_footer_dot.setStyleSheet(f"color: {_OK}; font-size: 14px;")
                self._server_footer_text.setText(msg or "Server running")
        else:
            self._server_footer_dot.setStyleSheet(f"color: {_MUTED}; font-size: 14px;")
            self._server_footer_text.setText("Server stopped")
