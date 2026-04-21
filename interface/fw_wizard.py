"""GUI wizard: Artifactory firmware download, local server, camera update_url (FW Wizard)."""
from __future__ import annotations

import logging
import os
import re
from functools import partial
from html import escape
from typing import Any, Callable

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QButtonGroup,
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
    QRadioButton,
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
    build_camera_fota_url_for_folder,
    classify_local_firmware_vs_selection,
    compute_download_model,
    debug_probe_local_firmware_folder,
    default_artifactory_url,
    default_fw_server_root,
    download_firmware_to_layout,
    ensure_server_and_camera_url,
    extract_firmware_archive,
    extract_vmc_model_ids_from_text,
    firmware_folder_version_label,
    list_environment_folders,
    prepare_env_directories,
    rename_server_folder,
    sanitize_server_folder_name,
    is_firmware_archive,
    normalize_firmware_search_row,
    scan_local_firmware_archives,
    search_firmware_archives,
    version_filter_matches_local_folder,
)
from core.camera_models import get_models
from utils.config_manager import (
    decode_token,
    get_config_path,
    load_config_file,
    save_config_file,
    update_last_used,
)

from interface.app_styles import (
    apply_qframe_stylesheet,
    prepare_qframe_for_qss,
    qcombobox_dark_stylesheet,
    set_arlo_pushbutton_variant,
)
from interface.fw_wizard_select_version import SelectVersion

_FW_GATE_LOG = logging.getLogger("arlohub.fw_local_detect")

_FwSearchRow = tuple[str, str, int | None, str | None]


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
            from datetime import datetime, timezone

            ms = int(s)
            return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            return s[:19]
    if "T" in s:
        return s.replace("T", " ")[:16]
    return s[:24]


# Match main window accents (see gui_window.py)
_ACCENT = "#00897B"
_OK = "#4caf7d"
_ERR = "#e05555"
_AMBER = "#c9a227"
_MUTED = "#8b95a5"
_SECTION = "#7a8494"
_SIDEBAR_BG = "#0d0d0d"
_BORDER = "#1e1e1e"
_MONO = "Consolas, 'Cascadia Mono', monospace"


def _fw_qlabel_ss(declarations: str) -> str:
    """Wrap rules in QLabel { }; bare property lists can fail to parse on some Qt builds."""
    d = declarations.strip()
    if not d.endswith(";"):
        d += ";"
    return f"QLabel {{ {d} }}"


def _fw_status_dot_qss(bg: str) -> str:
    return (
        f"QLabel {{ background-color: {bg}; border-radius: 4px; border: none; "
        "min-width: 8px; max-width: 8px; min-height: 8px; max-height: 8px; }"
    )


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


# Empty setStyleSheet("") can trigger "Could not parse stylesheet" on some Qt builds — use a neutral rule.
_QLABEL_STYLE_NEUTRAL = _fw_qlabel_ss(f"color: {_MUTED}")


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


class FwWizard(QDialog):
    """Six-step FW Wizard (Artifactory, local server, update URL)."""

    server_started = Signal(str)
    update_sent = Signal(bool)
    wizard_closed = Signal()
    open_local_server_tool = Signal()

    def __init__(
        self,
        parent: QWidget | None,
        model_dict: dict[str, Any],
        shell_async: ShellAsyncFn,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("FW Wizard")
        self.setMinimumSize(780, 440)
        self.setMaximumSize(1200, 720)
        self.resize(900, 600)
        self.setModal(False)

        self._model_dict = dict(model_dict)
        self._shell_async = shell_async
        self._device_shell_available = True
        self._update_url_succeeded = False

        self._base_url = default_artifactory_url()
        self._username: str | None = None
        self._token = ""
        self._search_busy = False
        self._skip_server_close_dialog = False

        self._fw_root = default_fw_server_root()
        self._search_results: list[_FwSearchRow] = []
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

        self._stress_mode = False
        self._stress_results_a: list[_FwSearchRow] = []
        self._stress_results_b: list[_FwSearchRow] = []
        self._stress_sel_a_folder: str | None = None
        self._stress_sel_a_file: str | None = None
        self._stress_sel_b_folder: str | None = None
        self._stress_sel_b_file: str | None = None
        self._stress_version_path_a = ""
        self._stress_version_path_b = ""
        self._stress_server_folder_a = ""
        self._stress_server_folder_b = ""
        self._stress_initial_ran = False
        self._stress_initial_dispatch_started = False
        self._stress_initial_ok = False
        self._stress_search_seq: str | None = None
        self._stress_folder_mismatch_label: QLabel | None = None
        self._stress_skip_download_a = False
        self._stress_skip_download_b = False
        self._prefetched_local_rows: list[tuple[str, str]] = []
        self._stress_prefetch_local_a: list[tuple[str, str]] = []
        self._stress_prefetch_local_b: list[tuple[str, str]] = []

        self._build_ui()
        self._refresh_server_footer()
        self._server_timer = QTimer(self)
        self._server_timer.timeout.connect(self._refresh_server_footer)
        self._server_timer.start(2000)

        self._load_config_into_step1()

    def apply_shell_connection(self, available: bool) -> None:
        """Called when the device session drops or returns; disables camera-only shell actions."""
        self._device_shell_available = available
        if self._current_step != 5:
            return
        if not available:
            self._btn_push.setEnabled(False)
            self._btn_trigger_refresh.setEnabled(False)
            if self._stress_mode:
                self._btn_open_local_server.setEnabled(False)
            return
        if self._stress_mode:
            self._btn_open_local_server.setEnabled(self._stress_initial_ok)
            return
        if self._camera_url:
            self._btn_push.setEnabled(not self._update_url_succeeded)
        if self._panel_onboarded.isVisible():
            self._btn_trigger_refresh.setEnabled(True)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._skip_server_close_dialog:
            running, _ = check_server_status()
            if running:
                ok_u, url = get_running_server_url()
                port = _port_from_running_server_url(ok_u, url)
                mb = QMessageBox(self)
                mb.setWindowTitle("FW Wizard")
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
                    mb.setWindowTitle("FW Wizard")
                    mb.setIcon(QMessageBox.Icon.Question)
                    if foreign and st:
                        mb.setText(
                            f"A firmware server is still listening on port {busy_port} "
                            f"(another ArloHub, PID {int(st['pid'])}). "
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
        apply_qframe_stylesheet(
            sidebar,
            f"QFrame {{ background-color: {_SIDEBAR_BG}; border-right: 1px solid {_BORDER}; }}",
        )
        side_lay = QVBoxLayout(sidebar)
        side_lay.setContentsMargins(14, 20, 14, 16)
        side_lay.setSpacing(0)

        self._step_labels: list[QLabel] = []
        self._step_checks: list[str] = []  # "", "done", "active"
        titles = [
            "Credentials",
            "Choose mode",
            "Search firmware",
            "Select version",
            "Download",
            "Server & update",
        ]
        for i, title in enumerate(titles):
            row = QHBoxLayout()
            num = QLabel(str(i + 1))
            num.setFixedWidth(22)
            num.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._step_labels.append(num)
            lab = QLabel(title)
            lab.setStyleSheet(_fw_qlabel_ss(f"color: {_SECTION}; font-size: 12px; font-weight: 500;"))
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
                apply_qframe_stylesheet(vline, f"QFrame {{ background-color: {_BORDER}; border: none; }}")
                conn_row.addWidget(indent)
                conn_row.addWidget(vline)
                conn_row.addStretch(1)
                conn_wrap = QWidget()
                conn_wrap.setLayout(conn_row)
                side_lay.addWidget(conn_wrap)

        side_lay.addStretch(1)

        self._server_footer_dot = QLabel()
        self._server_footer_dot.setFixedSize(8, 8)
        self._server_footer_dot.setStyleSheet(_fw_status_dot_qss("#5c6570"))
        self._server_footer_text = QLabel("Server: —")
        self._server_footer_text.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px;"))
        self._server_footer_text.setWordWrap(True)
        foot = QHBoxLayout()
        foot.addWidget(self._server_footer_dot)
        foot.addWidget(self._server_footer_text, 1)
        side_lay.addLayout(foot)

        outer.addWidget(sidebar)

        main = QWidget()
        main_lay = QVBoxLayout(main)
        main_lay.setContentsMargins(24, 22, 24, 18)
        main_lay.setSpacing(14)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._page_credentials())
        self._stack.addWidget(self._page_choose_mode())
        self._stack.addWidget(self._page_search())
        self._stack.addWidget(self._page_select())
        self._stack.addWidget(self._page_download())
        self._stack.addWidget(self._page_serve())
        main_lay.addWidget(self._stack, 1)
        self._stack.currentChanged.connect(self._on_main_stack_page_changed)

        nav = QHBoxLayout()
        nav.setSpacing(10)
        self._btn_back = QPushButton("Back")
        set_arlo_pushbutton_variant(self._btn_back, variant=None, nav=True)
        self._btn_back.clicked.connect(self._go_back)
        self._btn_next = QPushButton("Next")
        self._btn_next.clicked.connect(self._go_next)
        set_arlo_pushbutton_variant(self._btn_next, variant="primary", nav=True)
        self._btn_done = QPushButton("Done")
        self._btn_done.clicked.connect(self._on_done_keep_server)
        set_arlo_pushbutton_variant(self._btn_done, variant="primary", nav=True)
        self._btn_stop_close = QPushButton("Stop server and close")
        set_arlo_pushbutton_variant(self._btn_stop_close, variant="destructive", nav=True)
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
        lay.setSpacing(12)
        h = QLabel("Artifactory credentials")
        h.setStyleSheet(_fw_qlabel_ss("font-size: 15px; font-weight: 500;"))
        lay.addWidget(h)
        sub = QLabel(
            f"Use your Artifactory base URL and API token. Repo: {ARTIFACTORY_REPO}. "
            "VPN may be required."
        )
        sub.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 12px;"))
        sub.setWordWrap(True)
        lay.addWidget(sub)

        self._fld_url = QLineEdit()
        self._fld_url.setPlaceholderText("https://artifactory.arlocloud.com")
        self._fld_user = QLineEdit()
        self._fld_user.setPlaceholderText("Username (if required)")
        self._fld_token = QLineEdit()
        self._fld_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._fld_token.setPlaceholderText("API token / identity token")
        for fld in (self._fld_url, self._fld_user, self._fld_token):
            fld.setStyleSheet(_fw_lineedit_ss())
        self._fld_url.textChanged.connect(self._on_step0_fields_changed)
        self._fld_user.textChanged.connect(self._on_step0_fields_changed)
        self._fld_token.textChanged.connect(self._on_step0_fields_changed)

        form = QVBoxLayout()
        form.setSpacing(6)
        for caption, widget in (
            ("Artifactory URL", self._fld_url),
            ("Username", self._fld_user),
            ("API token", self._fld_token),
        ):
            cap = QLabel(caption)
            cap.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px; font-weight: 500;"))
            form.addWidget(cap)
            form.addWidget(widget)
        lay.addLayout(form)

        self._chk_save_creds = QCheckBox(f"Save credentials to config ({get_config_path()}) when leaving this step")
        lay.addWidget(self._chk_save_creds)

        row = QHBoxLayout()
        self._btn_test = QPushButton("Test connection")
        set_arlo_pushbutton_variant(self._btn_test, variant="primary", compact=True)
        self._btn_test.clicked.connect(self._test_credentials)
        self._cred_status = QLabel("")
        self._cred_status.setWordWrap(True)
        row.addWidget(self._btn_test)
        row.addWidget(self._cred_status, 1)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def _page_choose_mode(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(14)
        h = QLabel("Choose mode")
        h.setStyleSheet(_fw_qlabel_ss("font-size: 15px; font-weight: 500;"))
        lay.addWidget(h)
        sub = QLabel("Pick how you want to use the wizard. You can change this later with Back.")
        sub.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 12px;"))
        sub.setWordWrap(True)
        lay.addWidget(sub)

        self._grp_fw_mode = QButtonGroup(self)
        row = QHBoxLayout()
        row.setSpacing(12)

        def _mode_card(radio: QRadioButton, subtitle: str, *, object_name: str) -> QFrame:
            fr = QFrame()
            fr.setObjectName(object_name)
            prepare_qframe_for_qss(fr)
            inner = QVBoxLayout(fr)
            inner.setContentsMargins(16, 14, 16, 14)
            inner.setSpacing(10)
            radio.setStyleSheet("font-size: 15px; font-weight: 500;")
            sub_l = QLabel(subtitle)
            sub_l.setWordWrap(True)
            sub_l.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 12px;"))
            inner.addWidget(radio)
            inner.addWidget(sub_l)
            return fr

        self._radio_fw_single = QRadioButton("Single firmware")
        self._radio_fw_stress = QRadioButton("Stress test (two firmwares)")
        self._radio_fw_single.setChecked(True)
        self._grp_fw_mode.addButton(self._radio_fw_single)
        self._grp_fw_mode.addButton(self._radio_fw_stress)
        self._radio_fw_single.toggled.connect(self._on_fw_mode_toggled)
        self._radio_fw_stress.toggled.connect(self._on_fw_mode_toggled)

        self._frame_fw_single = _mode_card(
            self._radio_fw_single,
            "One Artifactory search, one download, one server folder — typical QA or dev install.",
            object_name="fwWizardModeSingle",
        )
        self._frame_fw_stress = _mode_card(
            self._radio_fw_stress,
            "Two firmware builds side by side for A/B stress testing on the same local server.",
            object_name="fwWizardModeStress",
        )
        row.addWidget(self._frame_fw_single, 1)
        row.addWidget(self._frame_fw_stress, 1)
        lay.addLayout(row)

        self._lbl_stress_onboarded_gate = QLabel("")
        self._lbl_stress_onboarded_gate.setWordWrap(True)
        self._lbl_stress_onboarded_gate.setStyleSheet(_fw_qlabel_ss(f"color: {_AMBER}; font-size: 12px;"))
        self._lbl_stress_onboarded_gate.hide()
        lay.addWidget(self._lbl_stress_onboarded_gate)

        lay.addStretch(1)
        self._sync_fw_mode_card_chrome()
        return w

    def _page_search(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(12)
        h = QLabel("Search firmware")
        h.setStyleSheet(_fw_qlabel_ss("font-size: 15px; font-weight: 500;"))
        lay.addWidget(h)

        self._combo_model = QComboBox()
        self._combo_model.setStyleSheet(_fw_combo_ss())
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
        _cm = QLabel("Camera model group")
        _cm.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px; font-weight: 500;"))
        lay.addWidget(_cm)
        lay.addWidget(self._combo_model)

        self._pills_host = QWidget()
        self._pills_layout = QHBoxLayout(self._pills_host)
        self._pills_layout.setContentsMargins(0, 0, 0, 0)
        _pill_cap = QLabel("Search includes these Artifactory model names:")
        _pill_cap.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px; font-weight: 500;"))
        lay.addWidget(_pill_cap)
        lay.addWidget(self._pills_host)

        self._lbl_vmc_bin = QLabel("")
        self._lbl_vmc_bin.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 12px;"))
        self._lbl_vmc_bin.setWordWrap(True)
        lay.addWidget(self._lbl_vmc_bin)

        self._search_mode_stack = QStackedWidget()
        single_pg = QWidget()
        single_lay = QVBoxLayout(single_pg)
        single_lay.setContentsMargins(0, 0, 0, 0)
        self._lbl_bin_target = QLabel("Artifactory download target (2K / FHD)")
        self._lbl_bin_target.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 12px;"))
        single_lay.addWidget(self._lbl_bin_target)
        self._combo_bin_target = QComboBox()
        self._combo_bin_target.setStyleSheet(_fw_combo_ss())
        self._combo_bin_target.currentIndexChanged.connect(self._on_search_fields_changed)
        single_lay.addWidget(self._combo_bin_target)
        bin_hint = QLabel(
            "Chooses which Artifactory product folder to download from. Extracted firmware still "
            "goes under binaries/<connected VMC> on the local server."
        )
        bin_hint.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px;"))
        bin_hint.setWordWrap(True)
        single_lay.addWidget(bin_hint)
        self._fld_version_filter = QLineEdit()
        self._fld_version_filter.setPlaceholderText("e.g. 1.300 (leave empty for all)")
        self._fld_version_filter.setStyleSheet(_fw_lineedit_ss())
        self._fld_version_filter.textChanged.connect(self._on_search_fields_changed)
        _vf = QLabel("Version filter")
        _vf.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px; font-weight: 500;"))
        single_lay.addWidget(_vf)
        single_lay.addWidget(self._fld_version_filter)
        sf_row = QHBoxLayout()
        self._combo_server_folder = QComboBox()
        self._combo_server_folder.setStyleSheet(_fw_combo_ss())
        self._combo_server_folder.setEditable(True)
        self._combo_server_folder.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo_server_folder.currentIndexChanged.connect(self._on_search_fields_changed)
        le_sf = self._combo_server_folder.lineEdit()
        _sf_overwrite_tip = (
            "If this folder already contains firmware (archive, binaries, or updaterules), "
            "searching and downloading again may overwrite files in that tree."
        )
        self._combo_server_folder.setToolTip(_sf_overwrite_tip)
        if le_sf is not None:
            le_sf.textChanged.connect(self._on_search_fields_changed)
            le_sf.setToolTip(_sf_overwrite_tip)
        sf_row.addWidget(self._combo_server_folder, 1)
        self._btn_rename_folder = QPushButton("Rename…")
        set_arlo_pushbutton_variant(self._btn_rename_folder, variant=None, compact=True)
        self._btn_rename_folder.setToolTip("Rename an existing folder under the server root")
        self._btn_rename_folder.clicked.connect(self._on_rename_server_folder)
        sf_row.addWidget(self._btn_rename_folder)
        _sf = QLabel("Server folder (local HTTP path segment)")
        _sf.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px; font-weight: 500;"))
        single_lay.addWidget(_sf)
        single_lay.addLayout(sf_row)
        sf_hint = QLabel(
            "Name for the folder on the local server (e.g. qa, qa1, downgrade, stress-v2). "
            "Pick an existing folder from the list or type a new name to create one. "
            "Different names let you keep multiple firmware trees side by side."
        )
        sf_hint.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px;"))
        sf_hint.setWordWrap(True)
        single_lay.addWidget(sf_hint)
        single_lay.addStretch(1)

        stress_pg = QWidget()
        stress_lay = QVBoxLayout(stress_pg)
        stress_lay.setContentsMargins(0, 8, 0, 0)
        stress_lay.setSpacing(10)

        def _stress_column_card(title: str) -> tuple[QFrame, QVBoxLayout]:
            card = QFrame()
            apply_qframe_stylesheet(
                card,
                "QFrame { border: 1px solid rgba(255,255,255,0.12); border-radius: 11px; "
                "background-color: #161a20; }",
            )
            inner = QVBoxLayout(card)
            inner.setContentsMargins(15, 14, 15, 14)
            inner.setSpacing(10)
            head = QLabel(title.upper())
            head.setStyleSheet(_fw_qlabel_ss(f"color: {_SECTION}; font-size: 12px; font-weight: 500;"))
            inner.addWidget(head)
            return card, inner

        col_row = QHBoxLayout()
        col_row.setSpacing(12)

        card_a, inner_a = _stress_column_card("Firmware A")
        self._fld_version_filter_a = QLineEdit()
        self._fld_version_filter_a.setPlaceholderText("e.g. 1.300 (leave empty for all)")
        self._fld_version_filter_a.setStyleSheet(_fw_lineedit_ss())
        self._fld_version_filter_a.textChanged.connect(self._on_search_fields_changed)
        _vfa = QLabel("Version filter")
        _vfa.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px; font-weight: 500;"))
        inner_a.addWidget(_vfa)
        inner_a.addWidget(self._fld_version_filter_a)
        _bta = QLabel("Artifactory download target (2K / FHD)")
        _bta.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px; font-weight: 500;"))
        inner_a.addWidget(_bta)
        self._combo_bin_target_a = QComboBox()
        self._combo_bin_target_a.setStyleSheet(_fw_combo_ss())
        self._combo_bin_target_a.currentIndexChanged.connect(self._on_search_fields_changed)
        inner_a.addWidget(self._combo_bin_target_a)
        _sfa = QLabel("Server folder")
        _sfa.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px; font-weight: 500;"))
        inner_a.addWidget(_sfa)
        sfr = QHBoxLayout()
        self._combo_server_folder_a = QComboBox()
        self._combo_server_folder_a.setStyleSheet(_fw_combo_ss())
        self._combo_server_folder_a.setEditable(True)
        self._combo_server_folder_a.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo_server_folder_a.setToolTip(_sf_overwrite_tip)
        le_sa = self._combo_server_folder_a.lineEdit()
        if le_sa is not None:
            le_sa.textChanged.connect(self._on_search_fields_changed)
            le_sa.setToolTip(_sf_overwrite_tip)
        self._combo_server_folder_a.currentIndexChanged.connect(self._on_search_fields_changed)
        sfr.addWidget(self._combo_server_folder_a, 1)
        self._btn_rename_a = QPushButton("Rename…")
        set_arlo_pushbutton_variant(self._btn_rename_a, variant=None, compact=True)
        self._btn_rename_a.setToolTip("Rename an existing folder under the server root")
        self._btn_rename_a.clicked.connect(lambda: self._on_rename_stress_folder("a"))
        sfr.addWidget(self._btn_rename_a)
        inner_a.addLayout(sfr)
        col_row.addWidget(card_a, 1)

        card_b, inner_b = _stress_column_card("Firmware B")
        self._fld_version_filter_b = QLineEdit()
        self._fld_version_filter_b.setPlaceholderText("e.g. 1.300 (leave empty for all)")
        self._fld_version_filter_b.setStyleSheet(_fw_lineedit_ss())
        self._fld_version_filter_b.textChanged.connect(self._on_search_fields_changed)
        _vfb = QLabel("Version filter")
        _vfb.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px; font-weight: 500;"))
        inner_b.addWidget(_vfb)
        inner_b.addWidget(self._fld_version_filter_b)
        _btb = QLabel("Artifactory download target (2K / FHD)")
        _btb.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px; font-weight: 500;"))
        inner_b.addWidget(_btb)
        self._combo_bin_target_b = QComboBox()
        self._combo_bin_target_b.setStyleSheet(_fw_combo_ss())
        self._combo_bin_target_b.currentIndexChanged.connect(self._on_search_fields_changed)
        inner_b.addWidget(self._combo_bin_target_b)
        _sfb = QLabel("Server folder")
        _sfb.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 11px; font-weight: 500;"))
        inner_b.addWidget(_sfb)
        sfr_b = QHBoxLayout()
        self._combo_server_folder_b = QComboBox()
        self._combo_server_folder_b.setStyleSheet(_fw_combo_ss())
        self._combo_server_folder_b.setEditable(True)
        self._combo_server_folder_b.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo_server_folder_b.setToolTip(_sf_overwrite_tip)
        le_sb = self._combo_server_folder_b.lineEdit()
        if le_sb is not None:
            le_sb.textChanged.connect(self._on_search_fields_changed)
            le_sb.setToolTip(_sf_overwrite_tip)
        self._combo_server_folder_b.currentIndexChanged.connect(self._on_search_fields_changed)
        sfr_b.addWidget(self._combo_server_folder_b, 1)
        self._btn_rename_b = QPushButton("Rename…")
        set_arlo_pushbutton_variant(self._btn_rename_b, variant=None, compact=True)
        self._btn_rename_b.setToolTip("Rename an existing folder under the server root")
        self._btn_rename_b.clicked.connect(lambda: self._on_rename_stress_folder("b"))
        sfr_b.addWidget(self._btn_rename_b)
        inner_b.addLayout(sfr_b)
        col_row.addWidget(card_b, 1)

        stress_lay.addLayout(col_row)
        self._stress_folder_mismatch_label = QLabel("")
        self._stress_folder_mismatch_label.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR}; font-size: 12px;"))
        self._stress_folder_mismatch_label.setWordWrap(True)
        stress_lay.addWidget(self._stress_folder_mismatch_label)
        stress_lay.addStretch(1)

        self._search_mode_stack.addWidget(single_pg)
        self._search_mode_stack.addWidget(stress_pg)
        lay.addWidget(self._search_mode_stack)

        self._search_status = QLabel("")
        self._search_status.setWordWrap(True)
        self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED};"))
        lay.addWidget(self._search_status)
        lay.addStretch(1)

        self._sync_model_combo_default()
        self._populate_server_folder_combo()
        self._populate_stress_bin_combos()
        self._populate_stress_folder_combos()
        self._refresh_vmc_binaries_label()
        return w

    def _page_select(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self._select_stack = QStackedWidget()
        w0 = QWidget()
        l0 = QVBoxLayout(w0)
        self._select_version = SelectVersion()
        self._select_version.selection_changed.connect(self._on_select_version_selection_changed)
        l0.addWidget(self._select_version)
        self._select_stack.addWidget(w0)

        w1 = QWidget()
        l1 = QVBoxLayout(w1)
        h1 = QLabel("Select one build for each firmware")
        h1.setStyleSheet(_fw_qlabel_ss("font-size: 15px; font-weight: 500;"))
        l1.addWidget(h1)
        self._lbl_select_a = QLabel("")
        self._lbl_select_a.setStyleSheet(_fw_qlabel_ss(f"color: {_ACCENT}; font-size: 12px; font-weight: bold;"))
        l1.addWidget(self._lbl_select_a)
        self._table_a = QTableWidget(0, 5)
        self._table_a.setHorizontalHeaderLabels(
            ["Version (path)", "Archive", "Size", "Date", "Variant"]
        )
        for col, mode in (
            (0, QHeaderView.ResizeMode.Stretch),
            (1, QHeaderView.ResizeMode.Stretch),
            (2, QHeaderView.ResizeMode.ResizeToContents),
            (3, QHeaderView.ResizeMode.ResizeToContents),
            (4, QHeaderView.ResizeMode.ResizeToContents),
        ):
            self._table_a.horizontalHeader().setSectionResizeMode(col, mode)
        self._table_a.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table_a.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table_a.setSortingEnabled(True)
        self._table_a.setMaximumHeight(180)
        self._table_a.itemSelectionChanged.connect(self._on_table_selection_a)
        l1.addWidget(self._table_a)
        self._lbl_select_b = QLabel("")
        self._lbl_select_b.setStyleSheet(_fw_qlabel_ss(f"color: {_ACCENT}; font-size: 12px; font-weight: bold;"))
        l1.addWidget(self._lbl_select_b)
        self._table_b = QTableWidget(0, 5)
        self._table_b.setHorizontalHeaderLabels(
            ["Version (path)", "Archive", "Size", "Date", "Variant"]
        )
        for col, mode in (
            (0, QHeaderView.ResizeMode.Stretch),
            (1, QHeaderView.ResizeMode.Stretch),
            (2, QHeaderView.ResizeMode.ResizeToContents),
            (3, QHeaderView.ResizeMode.ResizeToContents),
            (4, QHeaderView.ResizeMode.ResizeToContents),
        ):
            self._table_b.horizontalHeader().setSectionResizeMode(col, mode)
        self._table_b.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table_b.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table_b.setSortingEnabled(True)
        self._table_b.setMaximumHeight(180)
        self._table_b.itemSelectionChanged.connect(self._on_table_selection_b)
        l1.addWidget(self._table_b)
        self._select_stack.addWidget(w1)
        lay.addWidget(self._select_stack)
        return w

    def _page_download(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        h = QLabel("Download & extract")
        h.setStyleSheet(_fw_qlabel_ss("font-size: 15px; font-weight: 500;"))
        outer.addWidget(h)
        self._download_stack = QStackedWidget()
        dw0 = QWidget()
        lay = QVBoxLayout(dw0)
        self._dl_path_label = QLabel("")
        self._dl_path_label.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 12px;"))
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
        set_arlo_pushbutton_variant(self._btn_retry_dl, variant="primary", compact=True)
        self._btn_retry_dl.clicked.connect(self._start_download)
        self._btn_retry_dl.hide()
        row.addWidget(self._btn_retry_dl)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        self._download_stack.addWidget(dw0)

        dw1 = QWidget()
        l1 = QVBoxLayout(dw1)
        self._dl_path_label_stress = QLabel("")
        self._dl_path_label_stress.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 12px;"))
        self._dl_path_label_stress.setWordWrap(True)
        l1.addWidget(self._dl_path_label_stress)
        self._dl_lbl_a = QLabel("Firmware A")
        self._dl_lbl_a.setStyleSheet(_fw_qlabel_ss(f"color: {_ACCENT}; font-size: 12px; font-weight: bold;"))
        l1.addWidget(self._dl_lbl_a)
        self._dl_progress_a = QProgressBar()
        self._dl_progress_a.setRange(0, 0)
        self._dl_progress_a.setTextVisible(True)
        l1.addWidget(self._dl_progress_a)
        self._dl_status_a = QLabel("")
        self._dl_status_a.setWordWrap(True)
        l1.addWidget(self._dl_status_a)
        self._dl_lbl_b = QLabel("Firmware B")
        self._dl_lbl_b.setStyleSheet(_fw_qlabel_ss(f"color: {_ACCENT}; font-size: 12px; font-weight: bold;"))
        l1.addWidget(self._dl_lbl_b)
        self._dl_progress_b = QProgressBar()
        self._dl_progress_b.setRange(0, 0)
        self._dl_progress_b.setTextVisible(True)
        l1.addWidget(self._dl_progress_b)
        self._dl_status_b = QLabel("")
        self._dl_status_b.setWordWrap(True)
        l1.addWidget(self._dl_status_b)
        row2 = QHBoxLayout()
        self._btn_retry_dl_stress = QPushButton("Retry download")
        set_arlo_pushbutton_variant(self._btn_retry_dl_stress, variant="primary", compact=True)
        self._btn_retry_dl_stress.clicked.connect(self._on_stress_retry_download)
        self._btn_retry_dl_stress.hide()
        row2.addWidget(self._btn_retry_dl_stress)
        row2.addStretch(1)
        l1.addLayout(row2)
        l1.addStretch(1)
        self._download_stack.addWidget(dw1)
        outer.addWidget(self._download_stack)
        return w

    def _page_serve(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        h = QLabel("Server & set update URL")
        h.setStyleSheet(_fw_qlabel_ss("font-size: 15px; font-weight: 500;"))
        lay.addWidget(h)

        self._url_banner = QLabel("")
        self._url_banner.setTextFormat(Qt.TextFormat.RichText)
        self._url_banner.setOpenExternalLinks(True)
        self._url_banner.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self._url_banner.setStyleSheet(
            _fw_qlabel_ss(
                f"font-size: 13px; font-weight: 500; font-family: {_MONO}; color: {_ACCENT}; "
                "border: none; background: transparent;"
            )
        )
        self._url_banner.setWordWrap(True)
        lay.addWidget(self._url_banner)

        self._serve_sub_hint = QLabel(
            "The camera loads firmware from this URL (LAN IP so the device can reach your PC). "
            "The local server keeps running after you close this wizard."
        )
        self._serve_sub_hint.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 12px;"))
        self._serve_sub_hint.setWordWrap(True)
        lay.addWidget(self._serve_sub_hint)

        self._step5_log = QPlainTextEdit()
        self._step5_log.setReadOnly(True)
        self._step5_log.setMinimumHeight(120)
        self._step5_log.setMaximumHeight(160)
        self._step5_log.setPlaceholderText("Command output appears here…")
        mono = QFont("Menlo", 9) if os.name != "nt" else QFont("Consolas", 9)
        self._step5_log.setFont(mono)
        lay.addWidget(self._step5_log)

        self._btn_push = QPushButton("Set update URL")
        set_arlo_pushbutton_variant(self._btn_push, variant="primary", nav=True)
        self._btn_push.clicked.connect(self._push_update_url)
        lay.addWidget(self._btn_push)

        self._panel_onboarded = QWidget()
        ol = QVBoxLayout(self._panel_onboarded)
        ol.setContentsMargins(0, 8, 0, 0)
        ol.setSpacing(8)
        self._lbl_onboarded_hint = QLabel(
            "Camera is onboarded — firmware is staged on the local server. "
            "Use the Arlo app to trigger an update check, or press the button below."
        )
        self._lbl_onboarded_hint.setWordWrap(True)
        self._lbl_onboarded_hint.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 12px;"))
        ol.addWidget(self._lbl_onboarded_hint)
        self._btn_trigger_refresh = QPushButton("Trigger update check")
        set_arlo_pushbutton_variant(self._btn_trigger_refresh, variant="blue", nav=True)
        self._btn_trigger_refresh.clicked.connect(self._on_trigger_update_refresh)
        ol.addWidget(self._btn_trigger_refresh)
        lay.addWidget(self._panel_onboarded)
        self._panel_onboarded.hide()

        self._serve_stress_wrap = QWidget()
        ssl = QVBoxLayout(self._serve_stress_wrap)
        ssl.setContentsMargins(0, 8, 0, 0)
        self._lbl_stress_ready = QLabel("")
        self._lbl_stress_ready.setWordWrap(True)
        self._lbl_stress_ready.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-size: 12px;"))
        ssl.addWidget(self._lbl_stress_ready)
        self._btn_open_local_server = QPushButton("Open Local Server")
        set_arlo_pushbutton_variant(self._btn_open_local_server, variant="blue", nav=True)
        self._btn_open_local_server.clicked.connect(self._on_open_local_server)
        self._btn_open_local_server.hide()
        ssl.addWidget(self._btn_open_local_server)
        self._serve_stress_wrap.hide()
        lay.addWidget(self._serve_stress_wrap)

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
                _fw_qlabel_ss(
                    "background-color: rgba(0, 137, 123, 0.16); color: #b2dfdb; "
                    "border: 1px solid rgba(0, 137, 123, 0.28); border-radius: 10px; "
                    "padding: 4px 10px; font-size: 11px; font-weight: 500;"
                )
            )
            self._pills_layout.addWidget(pill)
        self._pills_layout.addStretch(1)

        self._fill_bin_target_combo(self._combo_bin_target)
        multi = self._combo_bin_target.count() > 1
        self._lbl_bin_target.setVisible(multi)
        self._combo_bin_target.setVisible(multi)
        self._populate_stress_bin_combos()

        if self._current_step == 2:
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

    def _on_main_stack_page_changed(self, index: int) -> None:
        if index == 1:
            self._refresh_choose_mode_onboarded_gate()

    def _refresh_choose_mode_onboarded_gate(self) -> None:
        """Entry gate only: block stress mode when the camera is already onboarded (checked once per visit)."""
        blocked = self._is_onboarded is True
        self._frame_fw_stress.setEnabled(not blocked)
        if blocked:
            self._lbl_stress_onboarded_gate.setText(
                "Stress test requires a camera that is not onboarded. "
                "Deregister the camera from its account first."
            )
            self._lbl_stress_onboarded_gate.show()
            if self._radio_fw_stress.isChecked():
                self._radio_fw_single.setChecked(True)
        else:
            self._lbl_stress_onboarded_gate.hide()
            self._lbl_stress_onboarded_gate.clear()

    def _on_fw_mode_toggled(self, *_args: object) -> None:
        self._stress_mode = self._radio_fw_stress.isChecked()
        self._search_mode_stack.setCurrentIndex(1 if self._stress_mode else 0)
        self._sync_fw_mode_card_chrome()
        if self._current_step in (1, 2):
            self._sync_nav()
        self._update_stress_folder_error()

    def _sync_fw_mode_card_chrome(self) -> None:
        single_sel = self._radio_fw_single.isChecked()
        acc = _ACCENT
        ina = "rgba(255, 255, 255, 0.12)"
        self._frame_fw_single.setStyleSheet(
            f"#fwWizardModeSingle {{ background-color: #161a20; border-radius: 11px; "
            f"border: {'2px solid ' + acc if single_sel else '1px solid ' + ina}; }}"
        )
        self._frame_fw_stress.setStyleSheet(
            f"#fwWizardModeStress {{ background-color: #161a20; border-radius: 11px; "
            f"border: {'2px solid ' + acc if not single_sel else '1px solid ' + ina}; }}"
        )

    def _update_stress_folder_error(self) -> None:
        if not self._stress_folder_mismatch_label:
            return
        fa = sanitize_server_folder_name((self._combo_server_folder_a.currentText() or "").strip())
        fb = sanitize_server_folder_name((self._combo_server_folder_b.currentText() or "").strip())
        if fa and fb and fa.lower() == fb.lower():
            self._stress_folder_mismatch_label.setText("Firmware A and B must use different server folder names.")
        else:
            self._stress_folder_mismatch_label.setText("")

    def _fill_bin_target_combo(self, combo: QComboBox) -> None:
        combo.blockSignals(True)
        combo.clear()
        for tag in self._fw_search_models:
            tu = tag.upper()
            if re.match(r"^VMC3\d{3}$", tu):
                disp = f"{tag} (2K)"
            elif re.match(r"^VMC2\d{3}$", tu):
                disp = f"{tag} (FHD)"
            else:
                disp = tag
            combo.addItem(disp, tag)
        combo.blockSignals(False)
        name_u = self._vmc_binaries_folder_name().upper()
        idx = 0
        for i in range(combo.count()):
            d = combo.itemData(i)
            if isinstance(d, str) and d.upper() == name_u:
                idx = i
                break
        combo.setCurrentIndex(idx)

    def _populate_stress_bin_combos(self) -> None:
        self._fill_bin_target_combo(self._combo_bin_target_a)
        self._fill_bin_target_combo(self._combo_bin_target_b)

    def _default_stress_folder_b_for_a(self, folder_a: str) -> str:
        a = (sanitize_server_folder_name((folder_a or "").strip()) or "qa").lower()
        if a == "qa":
            return "qa1"
        base = (folder_a or "").strip() or "qa"
        cand = f"{base}1"
        if cand.lower() != a:
            return cand
        return f"{base}-b"

    def _populate_stress_folder_combos(self) -> None:
        ca = (self._combo_server_folder_a.currentText() or "").strip()
        cb = (self._combo_server_folder_b.currentText() or "").strip()
        for combo, cur in (
            (self._combo_server_folder_a, ca),
            (self._combo_server_folder_b, cb),
        ):
            combo.blockSignals(True)
            combo.clear()
            for name in list_environment_folders(self._fw_root):
                combo.addItem(name)
            combo.blockSignals(False)
            if cur:
                idx = -1
                c_low = cur.lower()
                for i in range(combo.count()):
                    if combo.itemText(i).lower() == c_low:
                        idx = i
                        break
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                else:
                    combo.setEditText(cur)
        if not ca and not cb:
            self._combo_server_folder_a.setEditText("qa")
            self._combo_server_folder_b.setEditText("qa1")
        elif ca and not cb:
            self._combo_server_folder_b.setEditText(self._default_stress_folder_b_for_a(ca))

    def _on_rename_stress_folder(self, which: str) -> None:
        combo = self._combo_server_folder_a if which == "a" else self._combo_server_folder_b
        old_name = (combo.currentText() or "").strip()
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
        self._populate_stress_folder_combos()
        combo.setEditText(new_name)
        if stopped_for_rename:
            QMessageBox.information(
                self,
                "Rename folder",
                f"Renamed to “{new_name}”. The local firmware server was stopped; start it again "
                "when you are ready (FW Wizard final step or fw local).",
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
        self._populate_stress_folder_combos()
        self._update_stress_folder_error()
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
                "when you are ready (FW Wizard final step or fw local).",
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
                lab.setStyleSheet(_fw_qlabel_ss(f"color: {_OK}; font-weight: bold; font-size: 13px;"))
            elif i == self._current_step:
                lab.setText(str(i + 1))
                lab.setStyleSheet(
                    _fw_qlabel_ss(
                        f"color: white; font-weight: bold; background-color: {_ACCENT}; "
                        "border-radius: 11px; min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;"
                    )
                )
            else:
                lab.setText(str(i + 1))
                lab.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED}; font-weight: normal;"))

    def _step0_fields_valid(self) -> bool:
        url = (self._fld_url.text() or "").strip()
        tok = (self._fld_token.text() or "").strip()
        if not tok:
            return False
        u = url.lower()
        return bool(url and (u.startswith("http://") or u.startswith("https://")))

    def _step1_server_folder_valid(self) -> bool:
        if self._stress_mode:
            fa = sanitize_server_folder_name((self._combo_server_folder_a.currentText() or "").strip())
            fb = sanitize_server_folder_name((self._combo_server_folder_b.currentText() or "").strip())
            if not fa or not fb:
                return False
            if fa.lower() == fb.lower():
                return False
            return True
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
                QMessageBox.warning(self, "FW Wizard", f"Could not save credentials: {e}")

    def _sync_nav(self) -> None:
        self._btn_back.setVisible(self._current_step > 0 and self._current_step < 5)
        step = self._current_step
        self._btn_next.setVisible(step < 5)
        self._btn_done.setVisible(step == 5)
        self._btn_stop_close.setVisible(step == 5)

        if step == 0:
            self._btn_next.setEnabled(self._step0_fields_valid())
            self._btn_next.setText("Next")
        elif step == 1:
            self._btn_next.setEnabled(True)
            self._btn_next.setText("Next")
        elif step == 2:
            self._btn_next.setEnabled(not self._search_busy and self._step1_server_folder_valid())
            self._btn_next.setText("Searching…" if self._search_busy else "Next")
        elif step == 3:
            if self._stress_mode:
                ok_sel = self._stress_sel_a_file is not None and self._stress_sel_b_file is not None
                self._btn_next.setEnabled(ok_sel)
            else:
                self._btn_next.setEnabled(self._selected_filename is not None)
            self._btn_next.setText("Next")
        elif step == 4:
            self._btn_next.setEnabled(False)
            self._btn_next.setText("Next")

    def _go_back(self) -> None:
        if self._current_step <= 0:
            return
        if self._current_step >= 5:
            return
        if self._current_step == 4 and self._download_thread and self._download_thread.isRunning():
            return
        self._current_step -= 1
        self._stack.setCurrentIndex(self._current_step)
        self._update_sidebar()
        self._sync_nav()

    def _go_next(self) -> None:
        if self._current_step >= 5:
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
            self._current_step = 2
            self._stack.setCurrentIndex(2)
            self._update_sidebar()
            self._sync_nav()
            return
        if self._current_step == 2:
            self._start_search_from_next()
            return
        if self._current_step == 3:
            if self._stress_mode:
                if not self._stress_sel_a_file or not self._stress_sel_b_file:
                    return
                self._stress_version_path_a = self._stress_sel_a_folder or ""
                self._stress_version_path_b = self._stress_sel_b_folder or ""
                self._stress_skip_download_a = False
                self._stress_skip_download_b = False
                if not self._run_local_firmware_gate_stress():
                    return
            else:
                if not self._selected_filename:
                    return
                folder_in = sanitize_server_folder_name(self._current_server_folder_input())
                if folder_in:
                    self._server_folder_name = folder_in
                if not self._server_folder_name:
                    QMessageBox.warning(
                        self,
                        "FW Wizard",
                        "No server folder is set. Go back to Search firmware and choose a folder name.",
                    )
                    return
                if not self._run_local_firmware_gate_single():
                    return
                if self._maybe_skip_download_extract_single_already_deployed():
                    return
            self._prepare_download_page()
            self._current_step = 4
            self._stack.setCurrentIndex(4)
            self._update_sidebar()
            self._sync_nav()
            self._start_download()
            return

    def _on_stress_retry_download(self) -> None:
        self._stress_skip_download_a = False
        self._stress_skip_download_b = False
        self._start_download()

    def _pick_local_row_for_skip(self, folder: str, vf: str) -> _FwSearchRow | None:
        """First (version_path, filename) under archive/ for this folder, preferring vf substring match."""
        rows = scan_local_firmware_archives(self._fw_root, folder, vf)
        primary = str(self._model_dict.get("name") or "").strip().upper()
        if rows and primary:
            pid = frozenset([primary])
            pref = [r for r in rows if extract_vmc_model_ids_from_text(r[1] or "") & pid]
            if pref:
                rows = pref
        if rows:
            return rows[0]
        arch = os.path.join(self._fw_root, folder, "archive")
        if not os.path.isdir(arch):
            return None
        try:
            for name in sorted(os.listdir(arch), key=str.lower):
                if is_firmware_archive(name):
                    return (f"local/{folder}/{name}", name, None, None)
        except OSError:
            pass
        return None

    def _try_single_skip_search_use_local_only(self, folder: str, vf: str) -> bool:
        """
        If version filter is set and the server folder already holds that build, skip Artifactory,
        version selection, and download — go straight to Server & update.
        """
        if not (vf or "").strip():
            return False
        env_dir = os.path.abspath(os.path.join(self._fw_root, folder))
        try:
            primary = str(self._model_dict.get("name") or "").strip()
            ok_skip, loc = version_filter_matches_local_folder(
                env_dir, vf, primary_model_id=primary or None
            )
            if not ok_skip:
                return False
        except Exception:
            _FW_GATE_LOG.exception("pre-search local classify (single)")
            return False
        pair = self._pick_local_row_for_skip(folder, vf)
        row = normalize_firmware_search_row(pair) if pair else (f"local/{folder}/", "", None, None)
        vp, fn = row[0], row[1]
        QMessageBox.information(
            self,
            "FW Wizard",
            f"Firmware {loc} already exists in folder '{folder}'.\n\nSkipping search and download.",
        )
        self._selected_folder = vp
        self._selected_filename = fn.strip() or None
        self._version_path = vp or ""
        self._advance_to_serve_skipping_download(
            f"Skipped search, version selection, and download: firmware already present ({loc})."
        )
        return True

    def _stress_local_rows_if_skip_artifactory(self, folder: str, vf: str) -> list[_FwSearchRow] | None:
        """
        If version filter is non-empty and the folder already matches that build, return local-only
        result rows and skip Artifactory for this firmware. Otherwise None (run search).
        """
        vf_st = (vf or "").strip()
        if not vf_st:
            return None
        env_dir = os.path.abspath(os.path.join(self._fw_root, folder))
        try:
            primary = str(self._model_dict.get("name") or "").strip()
            ok_skip, _loc = version_filter_matches_local_folder(
                env_dir, vf_st, primary_model_id=primary or None
            )
            if not ok_skip:
                return None
        except Exception:
            _FW_GATE_LOG.exception("pre-search local classify (stress)")
            return None
        merged = list(scan_local_firmware_archives(self._fw_root, folder, vf_st))
        primary_st = str(self._model_dict.get("name") or "").strip().upper()
        if merged and primary_st:
            pid = frozenset([primary_st])
            pref = [r for r in merged if extract_vmc_model_ids_from_text(r[1] or "") & pid]
            if pref:
                merged = pref
        if not merged:
            pair = self._pick_local_row_for_skip(folder, vf_st)
            if pair:
                merged = [normalize_firmware_search_row(pair)]
        if not merged:
            return None
        return merged

    def _stress_show_select_version_step(self) -> None:
        self._lbl_select_a.setText(f"{self._stress_server_folder_a} — select version")
        self._lbl_select_b.setText(f"{self._stress_server_folder_b} — select version")
        self._fill_stress_tables()
        self._select_stack.setCurrentIndex(1)
        self._current_step = 3
        self._stack.setCurrentIndex(3)
        self._update_sidebar()
        self._sync_nav()

    def _advance_to_serve_skipping_download(self, log_line: str | None = None) -> None:
        self._prepare_download_page()
        self._current_step = 5
        self._stack.setCurrentIndex(5)
        self._update_sidebar()
        self._sync_nav()
        self._enter_serve_step()
        if log_line:
            self._append_step5_log(log_line)

    def _run_local_firmware_gate_single(self) -> bool:
        """False = stay on Select version (or already jumped to serve). True = continue to download step."""
        try:
            return self._run_local_firmware_gate_single_impl()
        except Exception as ex:
            print(f"[FW_LOCAL_DETECT] gate single exception: {ex!r}", flush=True)
            _FW_GATE_LOG.exception("local firmware gate (single)")
            QMessageBox.warning(
                self,
                "FW Wizard",
                "Could not inspect the local firmware folder; download will proceed as usual.\n\n"
                f"Detail: {ex}",
            )
            return True

    def _run_local_firmware_gate_single_impl(self) -> bool:
        folder = (self._server_folder_name or "").strip()
        env_dir = os.path.abspath(os.path.join(self._fw_root, folder))
        debug_probe_local_firmware_folder(
            "single",
            env_dir,
            selected_version_path=self._version_path,
            selected_archive_name=self._selected_filename,
        )
        cl = classify_local_firmware_vs_selection(env_dir, self._version_path, self._selected_filename)
        if cl == "exact_match":
            loc = firmware_folder_version_label(env_dir)
            self._show_firmware_already_in_folder_skip_message(folder, loc)
            self._advance_to_serve_skipping_download(
                f"Skipped download and extract: firmware already present ({loc})."
            )
            return False
        if cl == "different_present":
            loc = firmware_folder_version_label(env_dir)
            sel_disp = (self._selected_filename or self._version_path or "").strip() or "selection"
            r = QMessageBox.question(
                self,
                "FW Wizard",
                f"Folder '{folder}' contains {loc}. "
                f"Downloading the selected build ({sel_disp}) may overwrite existing files. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if r != QMessageBox.StandardButton.Yes:
                return False
        return True

    def _server_folder_ready_to_serve(self, env_dir: str) -> bool:
        """True if extracted layout exists (.enc and/or updaterules JSON), not just an archive file."""
        vmc = self._vmc_binaries_folder_name()
        bin_vm = os.path.join(env_dir, "binaries", vmc)
        if self._binaries_dir_has_enc(bin_vm):
            return True
        rules = os.path.join(env_dir, "updaterules")
        if os.path.isdir(rules):
            try:
                if any(name.lower().endswith(".json") for name in os.listdir(rules)):
                    return True
            except OSError:
                pass
        return False

    def _maybe_skip_download_extract_single_already_deployed(self) -> bool:
        """
        After gate: user picked a local/ table row and the server folder already has a deployable tree
        for that archive (skipped when classify was not exact_match but device is ready).
        """
        if not self._is_local_archive_row_path(self._selected_folder):
            return False
        folder = (self._server_folder_name or "").strip()
        fn = (self._selected_filename or "").strip()
        if not folder or not fn:
            return False
        env_dir = os.path.abspath(os.path.join(self._fw_root, folder))
        ap = os.path.join(env_dir, "archive", fn)
        if not os.path.isfile(ap) or not self._server_folder_ready_to_serve(env_dir):
            return False
        loc = firmware_folder_version_label(env_dir)
        self._show_firmware_already_in_folder_skip_message(folder, loc)
        self._advance_to_serve_skipping_download(
            f"Skipped download and extract — firmware already present ({loc})."
        )
        return True

    def _run_local_firmware_gate_stress(self) -> bool:
        try:
            return self._run_local_firmware_gate_stress_impl()
        except Exception as ex:
            print(f"[FW_LOCAL_DETECT] gate stress exception: {ex!r}", flush=True)
            _FW_GATE_LOG.exception("local firmware gate (stress)")
            QMessageBox.warning(
                self,
                "FW Wizard",
                "Could not inspect local firmware folders; download will proceed as usual.\n\n"
                f"Detail: {ex}",
            )
            return True

    def _run_local_firmware_gate_stress_impl(self) -> bool:
        fa = self._stress_server_folder_a
        fb = self._stress_server_folder_b
        env_a = os.path.abspath(os.path.join(self._fw_root, fa))
        env_b = os.path.abspath(os.path.join(self._fw_root, fb))
        debug_probe_local_firmware_folder(
            "stress A",
            env_a,
            selected_version_path=self._stress_version_path_a,
            selected_archive_name=self._stress_sel_a_file,
        )
        debug_probe_local_firmware_folder(
            "stress B",
            env_b,
            selected_version_path=self._stress_version_path_b,
            selected_archive_name=self._stress_sel_b_file,
        )
        ca = classify_local_firmware_vs_selection(env_a, self._stress_version_path_a, self._stress_sel_a_file)
        cb = classify_local_firmware_vs_selection(env_b, self._stress_version_path_b, self._stress_sel_b_file)

        if ca == "different_present":
            loc = firmware_folder_version_label(env_a)
            sel = (self._stress_sel_a_file or self._stress_version_path_a or "").strip() or "selection"
            r = QMessageBox.question(
                self,
                "FW Wizard",
                f"Folder '{fa}' contains {loc}. "
                f"Downloading Firmware A ({sel}) may overwrite existing files. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if r != QMessageBox.StandardButton.Yes:
                return False
        if cb == "different_present":
            loc = firmware_folder_version_label(env_b)
            sel = (self._stress_sel_b_file or self._stress_version_path_b or "").strip() or "selection"
            r = QMessageBox.question(
                self,
                "FW Wizard",
                f"Folder '{fb}' contains {loc}. "
                f"Downloading Firmware B ({sel}) may overwrite existing files. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if r != QMessageBox.StandardButton.Yes:
                return False

        if ca == "exact_match":
            self._stress_skip_download_a = True
        if cb == "exact_match":
            self._stress_skip_download_b = True

        if ca == "exact_match" and cb == "exact_match":
            la = firmware_folder_version_label(env_a)
            lb = firmware_folder_version_label(env_b)
            QMessageBox.information(
                self,
                "FW Wizard",
                f"Firmware is already in both folders ({la} in '{fa}', {lb} in '{fb}').\n\n"
                "Skipping download and extract — continuing to Server & update.",
            )
            self._advance_to_serve_skipping_download(
                f"Skipped download and extract: A ({la}) and B ({lb}) already present locally."
            )
            return False
        return True

    def _on_step0_fields_changed(self, *_args: object) -> None:
        if self._current_step == 0:
            self._sync_nav()

    def _on_search_fields_changed(self, *_args: object) -> None:
        if self._current_step == 2:
            self._update_stress_folder_error()
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
        self._cred_status.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED};"))

        ok, err = test_artifactory_access(url, token, user)
        self._btn_test.setEnabled(True)
        if ok:
            self._cred_status.setText("Connection OK (optional — Next does not require this).")
            self._cred_status.setStyleSheet(_fw_qlabel_ss(f"color: {_OK};"))
            if self._chk_save_creds.isChecked():
                try:
                    save_config_file(user or "", token, url, ARTIFACTORY_REPO)
                    update_last_used()
                    self._cred_status.setText("Connection OK. Credentials saved.")
                except OSError as e:
                    self._cred_status.setText(f"Connection OK (could not save config: {e})")
        else:
            self._cred_status.setText(err or "Connection failed.")
            self._cred_status.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
        self._sync_nav()

    def _start_search_from_next(self) -> None:
        if self._search_thread and self._search_thread.isRunning():
            return
        if self._stress_mode:
            fa = sanitize_server_folder_name((self._combo_server_folder_a.currentText() or "").strip())
            fb = sanitize_server_folder_name((self._combo_server_folder_b.currentText() or "").strip())
            if not fa or not fb:
                QMessageBox.warning(
                    self,
                    "FW Wizard",
                    "Enter valid server folder names for Firmware A and B.",
                )
                return
            if fa.lower() == fb.lower():
                QMessageBox.warning(
                    self,
                    "FW Wizard",
                    "Firmware A and B must use different server folder names.",
                )
                return
            self._stress_server_folder_a = fa
            self._stress_server_folder_b = fb
            vf_a = (self._fld_version_filter_a.text() or "").strip()
            vf_b = (self._fld_version_filter_b.text() or "").strip()
            self._stress_prefetch_local_a = scan_local_firmware_archives(self._fw_root, fa, vf_a)
            self._stress_prefetch_local_b = []
            skip_a = self._stress_local_rows_if_skip_artifactory(fa, vf_a)
            if skip_a is not None:
                self._stress_results_a = skip_a
                self._stress_prefetch_local_b = scan_local_firmware_archives(self._fw_root, fb, vf_b)
                nlb = len(self._stress_prefetch_local_b)
                skip_b = self._stress_local_rows_if_skip_artifactory(fb, vf_b)
                if skip_b is not None:
                    self._stress_results_b = skip_b
                    self._stress_search_seq = None
                    self._search_busy = False
                    self._search_status.setText(
                        "Firmware A and B: version filters match local folders — skipped Artifactory. "
                        "Select a row in each table."
                    )
                    self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_OK};"))
                    self._stress_show_select_version_step()
                    return
                self._stress_search_seq = "b"
                self._search_busy = True
                self._sync_nav()
                self._search_status.setText(
                    f"Firmware A: filter matches local folder (skipped Artifactory). "
                    f"Firmware B: scanned local archive/ ({nlb} match(es)); searching Artifactory…"
                    if nlb
                    else "Firmware A: filter matches local folder (skipped Artifactory). "
                    "Firmware B: no matches in local archive/; searching Artifactory…"
                )
                self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED};"))
                self._search_thread = _SearchThread(
                    self._base_url, self._token, self._username, vf_b, self._fw_search_models
                )
                self._search_thread.finished_search.connect(self._on_search_done)
                self._search_thread.start()
                return
            self._stress_search_seq = "a"
            vf = vf_a
            nla = len(self._stress_prefetch_local_a)
            self._search_busy = True
            self._sync_nav()
            self._search_status.setText(
                f"Firmware A: scanned local archive/ ({nla} match(es)); searching Artifactory…"
                if nla
                else "Firmware A: no matches in local archive/; searching Artifactory…"
            )
            self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED};"))
            self._search_thread = _SearchThread(
                self._base_url, self._token, self._username, vf, self._fw_search_models
            )
            self._search_thread.finished_search.connect(self._on_search_done)
            self._search_thread.start()
            return
        folder = sanitize_server_folder_name(self._current_server_folder_input())
        if not folder:
            QMessageBox.warning(
                self,
                "FW Wizard",
                "Enter a valid server folder name (letters, numbers, dash, underscore; no slashes).",
            )
            return
        self._server_folder_name = folder
        vf = (self._fld_version_filter.text() or "").strip()
        try:
            if self._try_single_skip_search_use_local_only(folder, vf):
                return
        except Exception:
            _FW_GATE_LOG.exception("pre-search single local skip")
        self._prefetched_local_rows = scan_local_firmware_archives(self._fw_root, folder, vf)
        loc_n = len(self._prefetched_local_rows)
        self._search_busy = True
        self._sync_nav()
        self._search_status.setText(
            f"Found {loc_n} archive(s) in local folder; searching Artifactory…"
            if loc_n
            else "No matching archives in local folder; searching Artifactory…"
        )
        self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED};"))
        self._search_thread = _SearchThread(
            self._base_url, self._token, self._username, vf, self._fw_search_models
        )
        self._search_thread.finished_search.connect(self._on_search_done)
        self._search_thread.start()

    def _on_search_done(self, ok: bool, flat: object, err: str) -> None:
        rows = flat if isinstance(flat, list) else []
        if self._stress_mode and self._stress_search_seq in ("a", "b"):
            if self._stress_search_seq == "a":
                pref_a = list(self._stress_prefetch_local_a or [])
                self._stress_prefetch_local_a = []
                art_a = [normalize_firmware_search_row(r) for r in rows] if ok else []
                merged_a = pref_a + art_a
                if not merged_a:
                    self._search_busy = False
                    self._stress_results_a = []
                    self._stress_search_seq = None
                    self._search_status.setText(
                        "Firmware A: no matching archives (local + Artifactory). "
                        "Adjust filters and press Next again."
                    )
                    self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
                    self._sync_nav()
                    return
                self._stress_results_a = merged_a
                fb = self._stress_server_folder_b
                vf_b = (self._fld_version_filter_b.text() or "").strip()
                skip_b = self._stress_local_rows_if_skip_artifactory(fb, vf_b)
                if skip_b is not None:
                    self._stress_results_b = skip_b
                    self._stress_search_seq = None
                    self._search_busy = False
                    self._stress_prefetch_local_b = []
                    if not ok:
                        self._search_status.setText(
                            f"Firmware A: Artifactory failed ({err or 'error'}). "
                            f"Using {len(pref_a)} local archive(s). "
                            f"Firmware B: filter matches local folder (skipped Artifactory)."
                        )
                        self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_AMBER};"))
                    else:
                        self._search_status.setText(
                            f"Firmware A: {len(merged_a)} match(es) (local + Artifactory). "
                            f"Firmware B: filter matches local folder (skipped Artifactory). "
                            "Select a row in each table."
                        )
                        self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_OK};"))
                    self._stress_show_select_version_step()
                    return
                self._stress_prefetch_local_b = scan_local_firmware_archives(self._fw_root, fb, vf_b)
                nlb = len(self._stress_prefetch_local_b)
                self._stress_search_seq = "b"
                self._sync_nav()
                if not ok:
                    self._search_status.setText(
                        f"Firmware A: Artifactory failed ({err or 'error'}). "
                        f"Using {len(pref_a)} local archive(s). "
                        f"Firmware B: scanned local ({nlb}); searching Artifactory…"
                    )
                    self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_AMBER};"))
                else:
                    self._search_status.setText(
                        f"Firmware A: {len(merged_a)} match(es) (local + Artifactory). "
                        f"Firmware B: scanned local ({nlb}); searching Artifactory…"
                    )
                    self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_MUTED};"))
                self._search_thread = _SearchThread(
                    self._base_url, self._token, self._username, vf_b, self._fw_search_models
                )
                self._search_thread.finished_search.connect(self._on_search_done)
                self._search_thread.start()
                return
            self._search_busy = False
            self._stress_search_seq = None
            pref_b = list(self._stress_prefetch_local_b or [])
            self._stress_prefetch_local_b = []
            art_b = [normalize_firmware_search_row(r) for r in rows] if ok else []
            merged_b = pref_b + art_b
            if not merged_b:
                self._stress_results_b = []
                self._search_status.setText(
                    "Firmware B: no matching archives (local + Artifactory). "
                    "Adjust filters and press Next again."
                )
                self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
                self._sync_nav()
                return
            self._stress_results_b = merged_b
            if not ok:
                self._search_status.setText(
                    f"Firmware A: {len(self._stress_results_a)} match(es). "
                    f"Firmware B: Artifactory failed ({err or 'error'}). "
                    f"Using {len(pref_b)} local archive(s)."
                )
                self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_AMBER};"))
            else:
                self._search_status.setText(
                    f"Firmware A: {len(self._stress_results_a)} match(es). "
                    f"Firmware B: {len(self._stress_results_b)} match(es) (local + Artifactory)."
                )
                self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_OK};"))
            self._stress_show_select_version_step()
            return

        self._search_busy = False
        pref = list(self._prefetched_local_rows or [])
        self._prefetched_local_rows = []
        art = [normalize_firmware_search_row(r) for r in rows] if ok else []
        merged = pref + art
        if not merged:
            self._search_results = []
            if not ok:
                self._search_status.setText(err or "Search failed.")
            else:
                self._search_status.setText(
                    "No matching firmware archives (.zip or env .tar.gz). "
                    "Adjust the version filter and press Next again."
                )
            self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
            self._sync_nav()
            return
        self._search_results = merged
        nl, na = len(pref), len(art)
        if not ok:
            self._search_status.setText(
                f"Artifactory search failed ({err or 'error'}). "
                f"Showing {nl} archive(s) from local folder only."
            )
            self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_AMBER};"))
        elif nl and na:
            self._search_status.setText(
                f"{len(merged)} match(es) ({nl} from local archive/, {na} from Artifactory)."
            )
            self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_OK};"))
        elif nl:
            self._search_status.setText(f"{nl} match(es) from local archive/ only (Artifactory returned none).")
            self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_OK};"))
        else:
            self._search_status.setText(f"{len(merged)} match(es).")
            self._search_status.setStyleSheet(_fw_qlabel_ss(f"color: {_OK};"))

        self._select_stack.setCurrentIndex(0)
        self._fill_results_table()
        self._current_step = 3
        self._stack.setCurrentIndex(3)
        self._update_sidebar()
        self._sync_nav()

    def _show_firmware_already_in_folder_skip_message(self, folder: str, loc: str) -> None:
        QMessageBox.information(
            self,
            "FW Wizard",
            f"This firmware is already in folder '{folder}' ({loc}).\n\n"
            "Skipping download and extract — continuing to Server & update.",
        )

    def _fill_results_table(self) -> None:
        self._select_version.set_search_rows(self._search_results)
        self._selected_filename = None
        self._selected_folder = None

    def _fill_one_table(self, table: QTableWidget, data: list[_FwSearchRow]) -> None:
        table.setSortingEnabled(False)
        table.setRowCount(0)
        for folder, fn, sz, md in data:
            r = table.rowCount()
            table.insertRow(r)
            table.setItem(r, 0, QTableWidgetItem(folder))
            table.setItem(r, 1, QTableWidgetItem(fn))
            table.setItem(r, 2, QTableWidgetItem(_format_fw_bytes(sz)))
            table.setItem(r, 3, QTableWidgetItem(_format_artifactory_ts(md)))
            ext = fn.lower().split(".")[-1] if "." in fn else fn
            table.setItem(r, 4, QTableWidgetItem(ext))
        table.setSortingEnabled(True)

    def _fill_stress_tables(self) -> None:
        self._fill_one_table(self._table_a, self._stress_results_a)
        self._fill_one_table(self._table_b, self._stress_results_b)
        self._stress_sel_a_folder = None
        self._stress_sel_a_file = None
        self._stress_sel_b_folder = None
        self._stress_sel_b_file = None

    def _on_select_version_selection_changed(self, row: object) -> None:
        if not isinstance(row, dict):
            self._selected_filename = None
            self._selected_folder = None
            self._sync_nav()
            return
        self._selected_folder = (row.get("path") or "").strip() or None
        self._selected_filename = (row.get("archive") or "").strip() or None
        self._version_path = self._selected_folder or ""
        self._sync_nav()

    def _on_table_selection_a(self) -> None:
        rows = self._table_a.selectionModel().selectedRows()
        if not rows:
            self._stress_sel_a_folder = None
            self._stress_sel_a_file = None
            self._sync_nav()
            return
        r = rows[0].row()
        fi0 = self._table_a.item(r, 0)
        fi1 = self._table_a.item(r, 1)
        if not fi0 or not fi1:
            return
        self._stress_sel_a_folder = fi0.text()
        self._stress_sel_a_file = fi1.text()
        self._sync_nav()

    def _on_table_selection_b(self) -> None:
        rows = self._table_b.selectionModel().selectedRows()
        if not rows:
            self._stress_sel_b_folder = None
            self._stress_sel_b_file = None
            self._sync_nav()
            return
        r = rows[0].row()
        fi0 = self._table_b.item(r, 0)
        fi1 = self._table_b.item(r, 1)
        if not fi0 or not fi1:
            return
        self._stress_sel_b_folder = fi0.text()
        self._stress_sel_b_file = fi1.text()
        self._sync_nav()

    @staticmethod
    def _binaries_dir_has_enc(path: str) -> bool:
        if not os.path.isdir(path):
            return False
        try:
            for _r, _, files in os.walk(path):
                for f in files:
                    if f.lower().endswith(".enc"):
                        return True
        except OSError:
            return False
        return False

    @staticmethod
    def _is_local_archive_row_path(folder_col: str | None) -> bool:
        return (folder_col or "").strip().lower().startswith("local/")

    def _finish_single_local_archive_only_download(self) -> None:
        folder = self._server_folder_name or sanitize_server_folder_name(
            self._current_server_folder_input()
        )
        if not folder:
            self._dl_status.setText("No server folder.")
            self._dl_status.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
            self._btn_retry_dl.show()
            return
        fn = (self._selected_filename or "").strip()
        if not fn:
            self._dl_status.setText("No archive selected.")
            self._dl_status.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
            self._btn_retry_dl.show()
            return
        archive_path = os.path.abspath(os.path.join(self._fw_root, folder, "archive", fn))
        if not os.path.isfile(archive_path):
            self._dl_status.setText(f"Local archive not found: {archive_path}")
            self._dl_status.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
            self._btn_retry_dl.show()
            return
        vmc = self._vmc_binaries_folder_name()
        ok_setup, msg_or_env, binaries_base, _pb, updaterules_dir, archive_dir = prepare_env_directories(
            self._fw_root, folder, vmc, self._fw_search_models
        )
        if not ok_setup:
            self._dl_status.setText(msg_or_env)
            self._dl_status.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
            self._btn_retry_dl.show()
            return
        chosen_binaries_dir = os.path.abspath(os.path.join(binaries_base, vmc))
        rules_dir = os.path.abspath(updaterules_dir)
        low = fn.lower()
        if not self._binaries_dir_has_enc(chosen_binaries_dir) and (
            low.endswith(".zip") or ".tar.gz" in low
        ):
            self._dl_status.setText("Extracting local archive…")
            ok_e, err_e = extract_firmware_archive(archive_path, chosen_binaries_dir, rules_dir)
            if not ok_e:
                self._dl_status.setText(err_e or "Extraction failed.")
                self._dl_status.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
                self._btn_retry_dl.show()
                return
        self._dl_progress.setRange(0, 100)
        self._dl_progress.setValue(100)
        self._dl_status.setText("Using firmware from local archive/.")
        self._dl_status.setStyleSheet(_fw_qlabel_ss(f"color: {_OK};"))
        QTimer.singleShot(400, self._auto_advance_serve)

    def _finish_stress_local_archive_leg(self, which: str) -> None:
        folder = self._stress_server_folder_a if which == "a" else self._stress_server_folder_b
        fn = ((self._stress_sel_a_file if which == "a" else self._stress_sel_b_file) or "").strip()
        lbl = self._dl_status_a if which == "a" else self._dl_status_b
        if not fn:
            lbl.setText("No archive selected.")
            lbl.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
            self._btn_retry_dl_stress.show()
            return
        archive_path = os.path.abspath(os.path.join(self._fw_root, folder, "archive", fn))
        if not os.path.isfile(archive_path):
            lbl.setText(f"Local archive not found: {archive_path}")
            lbl.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
            self._btn_retry_dl_stress.show()
            return
        vmc = self._vmc_binaries_folder_name()
        ok_setup, msg_or_env, binaries_base, _pb, updaterules_dir, archive_dir = prepare_env_directories(
            self._fw_root, folder, vmc, self._fw_search_models
        )
        if not ok_setup:
            lbl.setText(msg_or_env)
            lbl.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
            self._btn_retry_dl_stress.show()
            return
        chosen_binaries_dir = os.path.abspath(os.path.join(binaries_base, vmc))
        rules_dir = os.path.abspath(updaterules_dir)
        low = fn.lower()
        if not self._binaries_dir_has_enc(chosen_binaries_dir) and (
            low.endswith(".zip") or ".tar.gz" in low
        ):
            lbl.setText("Extracting local archive…")
            ok_e, err_e = extract_firmware_archive(archive_path, chosen_binaries_dir, rules_dir)
            if not ok_e:
                lbl.setText(err_e or "Extraction failed.")
                lbl.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
                self._btn_retry_dl_stress.show()
                return
        bar = self._dl_progress_a if which == "a" else self._dl_progress_b
        bar.setRange(0, 100)
        bar.setValue(100)
        lbl.setText("Using firmware from local archive/.")
        lbl.setStyleSheet(_fw_qlabel_ss(f"color: {_OK};"))
        if which == "a":
            QTimer.singleShot(200, lambda: self._start_stress_download_leg("b"))
        else:
            QTimer.singleShot(400, self._auto_advance_serve)

    def _prepare_download_page(self) -> None:
        vmc = self._vmc_binaries_folder_name()
        if self._stress_mode:
            self._download_stack.setCurrentIndex(1)
            pa = os.path.join(self._fw_root, self._stress_server_folder_a)
            pb = os.path.join(self._fw_root, self._stress_server_folder_b)
            self._dl_path_label_stress.setText(
                f"FW server root: {self._fw_root}\n"
                f"Firmware A → {pa}\n"
                f"Firmware B → {pb}\n"
                f"(binaries/{vmc}/ per folder)"
            )
            self._dl_lbl_a.setText(f"Firmware A ({self._stress_server_folder_a})")
            self._dl_lbl_b.setText(f"Firmware B ({self._stress_server_folder_b})")
            return
        self._download_stack.setCurrentIndex(0)
        path = os.path.join(self._fw_root, self._server_folder_name)
        self._dl_path_label.setText(
            f"FW server root: {self._fw_root}\n"
            f"Server folder: {path}  (archive/, binaries/{vmc}/, updaterules/)"
        )

    def _start_download(self) -> None:
        if self._download_thread and self._download_thread.isRunning():
            return
        if self._stress_mode:
            self._btn_retry_dl_stress.hide()
            self._dl_progress_a.setRange(0, 0)
            self._dl_progress_b.setRange(0, 0)
            self._dl_status_a.setText("")
            self._dl_status_b.setText("")
            self._dl_status_a.setStyleSheet(_QLABEL_STYLE_NEUTRAL)
            self._dl_status_b.setStyleSheet(_QLABEL_STYLE_NEUTRAL)
            self._start_stress_download_leg("a")
            return
        folder = self._server_folder_name or sanitize_server_folder_name(
            self._current_server_folder_input()
        )
        if not folder:
            self._dl_status.setText("Enter a valid server folder name.")
            self._dl_status.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
            self._btn_retry_dl.show()
            return
        self._btn_retry_dl.hide()
        self._dl_progress.setRange(0, 0)
        self._dl_status.setText("")
        self._dl_status.setStyleSheet(_QLABEL_STYLE_NEUTRAL)

        if self._is_local_archive_row_path(self._selected_folder):
            self._finish_single_local_archive_only_download()
            return

        vmc = self._vmc_binaries_folder_name()
        ok_setup, msg_or_env, binaries_base, _pb, updaterules_dir, archive_dir = prepare_env_directories(
            self._fw_root, folder, vmc, self._fw_search_models
        )
        if not ok_setup:
            self._dl_status.setText(msg_or_env)
            self._dl_status.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
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

    def _simulate_stress_leg_skip(self, which: str) -> None:
        bar = self._dl_progress_a if which == "a" else self._dl_progress_b
        lbl = self._dl_status_a if which == "a" else self._dl_status_b
        bar.setRange(0, 100)
        bar.setValue(100)
        lbl.setText("Already present in folder — skipped download.")
        lbl.setStyleSheet(_fw_qlabel_ss(f"color: {_OK};"))
        if which == "a":
            QTimer.singleShot(200, lambda: self._start_stress_download_leg("b"))
        else:
            QTimer.singleShot(400, self._auto_advance_serve)

    def _start_stress_download_leg(self, which: str) -> None:
        if which == "a" and self._stress_skip_download_a:
            self._simulate_stress_leg_skip("a")
            return
        if which == "b" and self._stress_skip_download_b:
            self._simulate_stress_leg_skip("b")
            return
        folder_key = self._stress_sel_a_folder if which == "a" else self._stress_sel_b_folder
        if self._is_local_archive_row_path(folder_key):
            self._finish_stress_local_archive_leg(which)
            return
        vmc = self._vmc_binaries_folder_name()
        folder = self._stress_server_folder_a if which == "a" else self._stress_server_folder_b
        version_path = self._stress_version_path_a if which == "a" else self._stress_version_path_b
        sel_file = self._stress_sel_a_file if which == "a" else self._stress_sel_b_file
        combo_art = self._combo_bin_target_a if which == "a" else self._combo_bin_target_b
        ok_setup, msg_or_env, binaries_base, _pb, updaterules_dir, archive_dir = prepare_env_directories(
            self._fw_root, folder, vmc, self._fw_search_models
        )
        if not ok_setup:
            lbl = self._dl_status_a if which == "a" else self._dl_status_b
            lbl.setText(msg_or_env)
            lbl.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
            self._btn_retry_dl_stress.show()
            return
        art_target = combo_art.currentData()
        if not isinstance(art_target, str) or not art_target.strip():
            art_target = self._primary_model_name
        download_model = compute_download_model(version_path, sel_file, art_target.strip())
        binaries_dir_for_download = os.path.join(msg_or_env, "binaries", vmc)
        chosen_binaries_dir = os.path.abspath(os.path.join(binaries_base, vmc))
        archive_path = os.path.abspath(os.path.join(archive_dir, sel_file or ""))
        rules_dir = os.path.abspath(updaterules_dir)

        def on_bytes(done: int, total: object) -> None:
            bar = self._dl_progress_a if which == "a" else self._dl_progress_b
            tot = int(total) if total is not None else None
            if tot and tot > 0:
                bar.setRange(0, tot)
                bar.setValue(min(done, tot))
            else:
                bar.setRange(0, 0)

        self._download_thread = _DownloadThread(
            self._token,
            download_model,
            version_path,
            binaries_dir_for_download,
            updaterules_dir,
            archive_dir,
            self._base_url,
            self._username,
            sel_file,
            archive_path,
            chosen_binaries_dir,
            rules_dir,
        )
        self._download_thread.byte_progress.connect(on_bytes)
        st_lbl = self._dl_status_a if which == "a" else self._dl_status_b
        self._download_thread.status_text.connect(st_lbl.setText)
        self._download_thread.finished_ok.connect(partial(self._on_stress_dl_leg_ok, which))
        self._download_thread.failed.connect(partial(self._on_stress_dl_leg_failed, which))
        self._download_thread.start()

    def _on_stress_dl_leg_ok(self, which: str) -> None:
        bar = self._dl_progress_a if which == "a" else self._dl_progress_b
        lbl = self._dl_status_a if which == "a" else self._dl_status_b
        bar.setRange(0, 100)
        bar.setValue(100)
        lbl.setText("Download and extraction complete.")
        lbl.setStyleSheet(_fw_qlabel_ss(f"color: {_OK};"))
        if which == "a":
            QTimer.singleShot(200, lambda: self._start_stress_download_leg("b"))
            return
        QTimer.singleShot(400, self._auto_advance_serve)

    def _on_stress_dl_leg_failed(self, which: str, msg: str) -> None:
        lbl = self._dl_status_a if which == "a" else self._dl_status_b
        lbl.setText(msg or "Download failed.")
        lbl.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
        self._btn_retry_dl_stress.show()

    def _on_dl_bytes(self, done: int, total: int | None) -> None:
        if total and total > 0:
            self._dl_progress.setRange(0, int(total))
            self._dl_progress.setValue(min(done, int(total)))
        else:
            self._dl_progress.setRange(0, 0)

    def _on_dl_status(self, text: str) -> None:
        self._dl_status.setText(text)

    def _on_dl_ok(self) -> None:
        if self._stress_mode:
            return
        self._dl_progress.setRange(0, 100)
        self._dl_progress.setValue(100)
        self._dl_status.setText("Download and extraction complete.")
        self._dl_status.setStyleSheet(_fw_qlabel_ss(f"color: {_OK};"))
        QTimer.singleShot(400, self._auto_advance_serve)

    def _on_dl_failed(self, msg: str) -> None:
        self._dl_status.setText(msg)
        self._dl_status.setStyleSheet(_fw_qlabel_ss(f"color: {_ERR};"))
        self._btn_retry_dl.show()

    def _auto_advance_serve(self) -> None:
        if self._current_step != 4:
            return
        self._current_step = 5
        self._stack.setCurrentIndex(5)
        self._update_sidebar()
        self._sync_nav()
        self._enter_serve_step()

    def _enter_serve_step(self) -> None:
        self._clear_step5_log()
        self._panel_onboarded.hide()
        self._btn_trigger_refresh.setEnabled(True)
        self._stress_initial_ok = False
        self._stress_initial_ran = False
        self._stress_initial_dispatch_started = False
        self._btn_open_local_server.hide()
        self._lbl_stress_ready.setText("")
        if self._stress_mode:
            self._serve_stress_wrap.show()
            self._serve_sub_hint.setText(
                "Stress test uses two folders on the same local server. Firmware A is applied first "
                "(update URL + reboot for an unclaimed camera)."
            )
            self._btn_push.hide()
            folder_a = self._stress_server_folder_a.strip()
            ok, err, cam_url_a = ensure_server_and_camera_url(self._fw_root, folder_a)
            if not ok:
                self._url_banner.setTextFormat(Qt.TextFormat.PlainText)
                self._url_banner.setOpenExternalLinks(False)
                self._url_banner.setText("Could not start or read server.")
                self._append_step5_log(err or "Server error.")
                return
            self._camera_url = cam_url_a
            ok_b, err_b, cam_url_b = build_camera_fota_url_for_folder(self._fw_root, self._stress_server_folder_b)
            if not ok_b:
                self._append_step5_log(err_b or "Could not build URL for Firmware B.")
                cam_url_b = "(unavailable)"
            self._url_banner.setTextFormat(Qt.TextFormat.RichText)
            self._url_banner.setOpenExternalLinks(True)
            eua = escape(cam_url_a)
            eub = escape(cam_url_b) if ok_b else cam_url_b
            self._url_banner.setText(
                f"<b>Firmware A ({escape(folder_a)})</b><br/><a href=\"{eua}\">{eua}</a><br/><br/>"
                f"<b>Firmware B ({escape(self._stress_server_folder_b)})</b><br/>"
                f"{('<a href=\"' + eub + '\">' + eub + '</a>') if ok_b else eub}"
            )
            self.server_started.emit(cam_url_a)
            self._refresh_server_footer()
            QTimer.singleShot(400, self._stress_send_initial_url_reboot)
            return
        self._serve_stress_wrap.hide()
        self._serve_sub_hint.setText(
            "The camera loads firmware from this URL (LAN IP so the device can reach your PC). "
            "The local server keeps running after you close this wizard."
        )
        self._btn_push.show()
        folder = self._server_folder_name.strip()
        ok, err, cam_url = ensure_server_and_camera_url(self._fw_root, folder)
        if not ok:
            self._url_banner.setTextFormat(Qt.TextFormat.PlainText)
            self._url_banner.setOpenExternalLinks(False)
            self._url_banner.setText("Could not start or read server.")
            self._append_step5_log(err or "Server error.")
            self._btn_push.setEnabled(False)
            return
        self._camera_url = cam_url
        self._url_banner.setTextFormat(Qt.TextFormat.RichText)
        self._url_banner.setOpenExternalLinks(True)
        eu = escape(cam_url)
        self._url_banner.setText(f'<a href="{eu}">{eu}</a>')
        self._btn_push.setEnabled(bool(self._device_shell_available))
        self._btn_push.setVisible(True)
        self.server_started.emit(cam_url)
        self._refresh_server_footer()

    def _stress_send_initial_url_reboot(self) -> None:
        if not self._stress_mode or self._stress_initial_dispatch_started:
            return
        self._stress_initial_dispatch_started = True
        if not self._camera_url:
            return
        self._append_step5_log("Stress test: sending arlocmd update_url (Firmware A)…")

        def after_url(ok: bool, msg: str) -> None:
            if ok:
                self._append_step5_log("arlocmd update_url: OK")
                self.update_sent.emit(True)
            else:
                self._append_step5_log(msg or "update_url failed.")
                self.update_sent.emit(False)
                self._stress_initial_ok = False
                return
            self._append_step5_log("Stress test: sending arlocmd reboot (camera should be unclaimed)…")

            def after_reboot(ok_r: bool, msg_r: str) -> None:
                if ok_r:
                    self._append_step5_log("arlocmd reboot: OK")
                else:
                    self._append_step5_log("arlocmd reboot failed: " + (msg_r or "error"))
                self._stress_initial_ok = ok and ok_r
                self._stress_initial_ran = True
                url_ok = "✓" if ok else "✗"
                reboot_ok = "✓" if ok_r else "✗"
                self._lbl_stress_ready.setTextFormat(Qt.TextFormat.RichText)
                self._lbl_stress_ready.setText(
                    f"<p style='color:{_MUTED}; font-size:12px;'>"
                    f"<b>Status:</b> update URL set {url_ok} &nbsp;·&nbsp; reboot sent {reboot_ok}<br/><br/>"
                    "<i>Both firmware versions are ready on the local server. Use the Local Server tool "
                    "to switch between folders.</i>"
                    "</p>"
                )
                self._btn_open_local_server.setVisible(True)
                self._btn_open_local_server.setEnabled(
                    bool(self._device_shell_available and self._stress_initial_ok)
                )

            self._shell_async("arlocmd reboot", [], after_reboot)

        self._shell_async("arlocmd update_url", [self._camera_url], after_url)

    def _on_open_local_server(self) -> None:
        if not self._stress_mode:
            return
        self.open_local_server_tool.emit()

    def _push_update_url(self) -> None:
        if not self._camera_url:
            return
        self._btn_push.setEnabled(False)
        self._append_step5_log("Sending arlocmd update_url…")

        def done(ok: bool, msg: str) -> None:
            if ok:
                self._update_url_succeeded = True
                self._append_step5_log("arlocmd update_url: OK")
                self.update_sent.emit(True)
                self._btn_push.setEnabled(False)
                self._finish_step5_after_update_url_success()
            else:
                self._append_step5_log(msg or "Command failed.")
                self.update_sent.emit(False)
                self._btn_push.setEnabled(self._device_shell_available)

        self._shell_async("arlocmd update_url", [self._camera_url], done)

    def _finish_step5_after_update_url_success(self) -> None:
        """Onboarded: show update_refresh UI. Not onboarded: auto-reboot for new URL on boot."""
        if self._is_onboarded is True:
            self._panel_onboarded.show()
            return
        self._append_step5_log("Sending arlocmd reboot…")
        self._append_step5_log(
            "The camera is rebooting; the connection may drop until it finishes booting.\n"
        )

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
            self._server_footer_dot.setStyleSheet(_fw_status_dot_qss(_OK))
        elif hint == "amber":
            self._server_footer_dot.setStyleSheet(_fw_status_dot_qss(_AMBER))
        else:
            self._server_footer_dot.setStyleSheet(_fw_status_dot_qss("#5c6570"))
        self._server_footer_text.setText(line)
        self._server_footer_text.setToolTip(tooltip)
        self._server_footer_dot.setToolTip(tooltip)
        if getattr(self, "_select_version", None) is not None:
            self._select_version.sync_server_footer(hint, line, tooltip)
