"""Artifactory download dialog launched from Tools → Local Server."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.fw_setup_service import (
    classify_local_firmware_vs_selection,
    compute_download_model,
    default_artifactory_url,
    default_fw_server_root,
    download_firmware_to_layout,
    extract_firmware_archive,
    firmware_folder_model_label,
    firmware_folder_version_label,
    folder_has_firmware_artifacts,
    list_environment_folders,
    normalize_firmware_search_row,
    prepare_env_directories,
    sanitize_server_folder_name,
    search_firmware_archives,
    vmc_binaries_folder_name_for_device,
)
from core.camera_models import get_model_by_name, get_models
from core.local_server import DEFAULT_PORT, start_http_server
from utils.config_manager import decode_token, get_config_path, load_config_file

from interface.app_styles import ARLO_ACCENT, qcombobox_dark_stylesheet, set_arlo_pushbutton_variant

_MUTED = "#8b95a5"
_TEXT = "#c5ced9"
_SECTION = "#7a8494"
_AMBER = "#c9a227"
_BLUE_INFO = "#90caf9"
_BG = "#12161c"


def _ql(decl: str) -> str:
    d = decl.strip()
    if not d.endswith(";"):
        d += ";"
    return f"QLabel {{ {d} }}"


def _le_ss() -> str:
    return (
        "QLineEdit { background-color: #1a1f26; color: #e8eef4; "
        "border: 1px solid rgba(255,255,255,0.10); border-radius: 8px; padding: 7px 10px; font-size: 12px; "
        f"selection-background-color: {ARLO_ACCENT}; }}"
    )


def _combo_ss() -> str:
    return qcombobox_dark_stylesheet(
        border_radius=8,
        padding="6px 10px",
        min_height=24,
        dropdown_width=24,
        font_size="12px",
    )


def _list_ss() -> str:
    return (
        "QListWidget { background-color: #1a1f26; color: #e8eef4; "
        "border: 1px solid rgba(255,255,255,0.10); border-radius: 8px; padding: 4px; font-size: 12px; }"
        "QListWidget::item { padding: 8px 10px; border-radius: 4px; }"
        "QListWidget::item:selected { background-color: rgba(0, 137, 123, 0.38); color: #e8eef4; }"
        "QListWidget::item:hover:!selected { background-color: rgba(255,255,255,0.06); }"
    )


def _dialog_ss() -> str:
    return f"QDialog {{ background-color: {_BG}; }}"


def _progress_ss() -> str:
    return (
        "QProgressBar { border: 1px solid rgba(255,255,255,0.12); border-radius: 6px; "
        "background-color: #1a1f26; height: 14px; text-align: center; color: #c5ced9; font-size: 11px; }"
        f"QProgressBar::chunk {{ background-color: {ARLO_ACCENT}; border-radius: 5px; }}"
    )


def _repo_path_hint(repo_path: str) -> str:
    parts = [p for p in (repo_path or "").replace("\\", "/").split("/") if p]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1] if parts else "—"


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


def _row_display_line(
    repo_folder_path: str,
    filename: str,
    size_b: int | None,
    modified_raw: str | None,
) -> str:
    """Primary label is the archive name (build id); path/size/date follow."""
    loc = _repo_path_hint(repo_folder_path)
    return (
        f"{filename}  ·  {loc}  ·  {_format_fw_bytes(size_b)}  ·  {_format_artifactory_ts(modified_raw)}"
    )


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


class LocalServerDownloadDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None,
        *,
        fw_root: str,
        camera_connected: bool,
        device_model: str,
        fw_search_models: list[str],
        on_complete: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Download firmware")
        self.setMinimumSize(560, 460)
        self.resize(620, 500)
        self.setStyleSheet(_dialog_ss())

        self._fw_root = os.path.abspath(fw_root)
        self._camera_connected = bool(camera_connected)
        self._device_model = (device_model or "").strip()
        self._fw_search_models = list(fw_search_models) if fw_search_models else []
        if not self._fw_search_models:
            m = get_model_by_name(self._device_model)
            if m:
                self._fw_search_models = list(m.get("fw_search_models") or [m["name"]])
            else:
                groups = get_models()
                if groups:
                    self._fw_search_models = list(
                        groups[0].get("fw_search_models") or [groups[0]["name"]]
                    )
                else:
                    self._fw_search_models = ["VMC3070"]

        self._on_complete = on_complete
        self._token = ""
        self._username: str | None = None
        self._base_url = default_artifactory_url()
        self._search_thread: _SearchThread | None = None
        self._download_thread: _DownloadThread | None = None
        self._results: list[tuple[str, str, int | None, str | None]] = []
        self._sel_folder: str | None = None
        self._sel_file: str | None = None
        self._existing_folder_names: set[str] = set()

        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(20, 16, 20, 18)

        top = QHBoxLayout()
        top.setSpacing(12)
        title = QLabel("Download firmware")
        title.setStyleSheet(_ql(f"color: {_TEXT}; font-size: 15px; font-weight: 500;"))
        top.addWidget(title)
        top.addStretch(1)
        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setFlat(True)
        set_arlo_pushbutton_variant(self._btn_cancel, variant=None, compact=True)
        self._btn_cancel.clicked.connect(self.reject)
        top.addWidget(self._btn_cancel)
        lay.addLayout(top)

        hint = QLabel(
            "Search Artifactory with saved credentials. Pick or type a server folder, then choose a build."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(_ql(f"color: {_MUTED}; font-size: 11px;"))
        lay.addWidget(hint)

        inputs_row = QHBoxLayout()
        inputs_row.setSpacing(14)

        col_folder = QVBoxLayout()
        col_folder.setSpacing(4)
        _sf = QLabel("Server folder")
        _sf.setStyleSheet(_ql(f"color: {_MUTED}; font-size: 11px; font-weight: 500;"))
        col_folder.addWidget(_sf)
        self._combo_folder = QComboBox()
        self._combo_folder.setStyleSheet(_combo_ss())
        self._combo_folder.setEditable(True)
        self._combo_folder.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo_folder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._combo_folder.setToolTip(
            "Choose an existing folder from the list, or type a new folder name to create it."
        )
        col_folder.addWidget(self._combo_folder)
        inputs_row.addLayout(col_folder, stretch=3)

        col_ver = QVBoxLayout()
        col_ver.setSpacing(4)
        _vf = QLabel("Version filter")
        _vf.setStyleSheet(_ql(f"color: {_MUTED}; font-size: 11px; font-weight: 500;"))
        col_ver.addWidget(_vf)
        self._fld_version = QLineEdit()
        self._fld_version.setPlaceholderText("e.g. 1.300 (optional)")
        self._fld_version.setStyleSheet(_le_ss())
        self._fld_version.setMaximumWidth(200)
        col_ver.addWidget(self._fld_version)
        inputs_row.addLayout(col_ver, stretch=1)

        lay.addLayout(inputs_row)

        self._lbl_folder_hint = QLabel("")
        self._lbl_folder_hint.setWordWrap(True)
        self._lbl_folder_hint.setStyleSheet(_ql(f"color: {_MUTED}; font-size: 11px;"))
        lay.addWidget(self._lbl_folder_hint)

        self._btn_search = QPushButton("Search")
        set_arlo_pushbutton_variant(self._btn_search, variant="primary", compact=False)
        self._btn_search.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._btn_search.clicked.connect(self._on_search)
        lay.addWidget(self._btn_search)

        hdr = QLabel("Archive  ·  Location  ·  Size  ·  Date")
        hdr.setStyleSheet(_ql(f"color: {_SECTION}; font-size: 11px; font-weight: 500;"))
        lay.addWidget(hdr)

        self._list = QListWidget()
        self._list.setStyleSheet(_list_ss())
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setMaximumHeight(240)
        self._list.itemSelectionChanged.connect(self._on_result_selection)
        lay.addWidget(self._list, stretch=1)

        self._btn_download = QPushButton("Download")
        self._btn_download.setEnabled(False)
        set_arlo_pushbutton_variant(self._btn_download, variant="blue", compact=False)
        self._btn_download.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._btn_download.clicked.connect(self._on_download)
        lay.addWidget(self._btn_download)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setStyleSheet(_progress_ss())
        lay.addWidget(self._progress)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(_ql(f"color: {_MUTED}; font-size: 11px;"))
        lay.addWidget(self._status)

        self._populate_folder_combo()
        self._combo_folder.currentTextChanged.connect(self._on_folder_text_changed)
        self._sync_folder_hint()

    def _resolved_existing_folder_path(self, sanitized_name: str) -> str | None:
        want = sanitized_name.lower()
        for name in list_environment_folders(self._fw_root):
            if name.lower() == want:
                return os.path.join(self._fw_root, name)
        return None

    def _on_folder_text_changed(self, _text: str) -> None:
        self._sync_folder_hint()
        self._update_download_button_label()

    def _populate_folder_combo(self) -> None:
        cur = (self._combo_folder.currentText() or "").strip()
        self._combo_folder.blockSignals(True)
        self._combo_folder.clear()
        names = list_environment_folders(self._fw_root)
        self._existing_folder_names = {n.lower() for n in names}
        for name in names:
            self._combo_folder.addItem(name)
        self._combo_folder.blockSignals(False)
        if cur:
            self._combo_folder.setEditText(cur)
        elif self._combo_folder.count():
            self._combo_folder.setCurrentIndex(0)
        self._sync_folder_hint()

    def _sync_folder_hint(self) -> None:
        raw = (self._combo_folder.currentText() or "").strip()
        sn = sanitize_server_folder_name(raw)
        if not raw or not sn:
            self._lbl_folder_hint.clear()
            self._lbl_folder_hint.setStyleSheet(_ql(f"color: {_MUTED}; font-size: 11px;"))
            return
        exists = sn.lower() in self._existing_folder_names
        if exists:
            path = self._resolved_existing_folder_path(sn)
            shown = os.path.basename(path) if path else sn
            if path and folder_has_firmware_artifacts(path):
                ver = firmware_folder_version_label(path)
                model = firmware_folder_model_label(path)
                if ver != "—" and model != "—":
                    msg = f"Folder '{shown}' contains v{ver} ({model}) — will be replaced"
                elif ver != "—":
                    msg = f"Folder '{shown}' contains v{ver} — will be replaced"
                elif model != "—":
                    msg = f"Folder '{shown}' contains firmware ({model}) — will be replaced"
                else:
                    msg = f"Folder '{shown}' contains firmware — will be replaced"
                self._lbl_folder_hint.setText(msg)
                self._lbl_folder_hint.setStyleSheet(_ql(f"color: {_AMBER}; font-size: 11px;"))
            else:
                self._lbl_folder_hint.setText(f"Folder '{shown}' exists but is empty")
                self._lbl_folder_hint.setStyleSheet(_ql(f"color: {_MUTED}; font-size: 11px;"))
        else:
            self._lbl_folder_hint.setText(f"New folder '{sn}' will be created")
            self._lbl_folder_hint.setStyleSheet(_ql(f"color: {_BLUE_INFO}; font-size: 11px;"))

    def _artifactory_download_model(self) -> str:
        """VMC / product folder for Artifactory download — from camera when connected."""
        if self._camera_connected and self._device_model:
            return vmc_binaries_folder_name_for_device(self._device_model)
        if self._fw_search_models:
            return (self._fw_search_models[0] or "VMC3070").strip().upper()
        return "VMC3070"

    def _layout_vmc(self) -> str:
        """Binaries subdirectory under server folder (same as wizard)."""
        return self._artifactory_download_model()

    def _selected_version_label(self) -> str:
        if not self._sel_file:
            return "…"
        fn = self._sel_file.strip()
        return fn if len(fn) <= 48 else fn[:45] + "…"

    def _update_download_button_label(self) -> None:
        folder = sanitize_server_folder_name((self._combo_folder.currentText() or "").strip())
        if not folder or not self._sel_folder or not self._sel_file:
            self._btn_download.setText("Download")
            return
        ver = self._selected_version_label()
        self._btn_download.setText(f"Download v{ver} to “{folder}”")

    def _load_credentials(self) -> tuple[bool, str]:
        try:
            config = load_config_file()
        except ValueError as e:
            return False, f"Config file is corrupted: {e}\nFile: {get_config_path()}"
        if not config:
            return False, ""
        art = config["artifactory"]
        username = (art.get("username") or "").strip() or None
        try:
            token = decode_token(art.get("access_token") or "")
        except Exception:
            return False, ""
        token = (token or "").strip()
        if not token:
            return False, ""
        base_url = (art.get("base_url") or "").strip() or default_artifactory_url()
        self._username = username
        self._token = token
        self._base_url = base_url
        return True, ""

    @Slot()
    def _on_search(self) -> None:
        self._status.clear()
        folder = sanitize_server_folder_name((self._combo_folder.currentText() or "").strip())
        if not folder:
            QMessageBox.warning(self, "Download firmware", "Enter a valid server folder name.")
            return
        ok_c, err_c = self._load_credentials()
        if not ok_c:
            if err_c:
                QMessageBox.warning(self, "Download firmware", err_c)
            else:
                QMessageBox.information(
                    self,
                    "Download firmware",
                    "No Artifactory credentials configured. Set them up in the FW Wizard first.",
                )
            return

        env_dir = os.path.abspath(os.path.join(self._fw_root, folder))
        if folder_has_firmware_artifacts(env_dir):
            r = QMessageBox.question(
                self,
                "Download firmware",
                f"Folder '{folder}' already contains firmware files. "
                "Downloading may overwrite files in that tree. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return

        vf = (self._fld_version.text() or "").strip()
        self._btn_search.setEnabled(False)
        self._btn_download.setEnabled(False)
        self._list.clear()
        self._results = []
        self._sel_folder = None
        self._sel_file = None
        self._update_download_button_label()

        self._search_thread = _SearchThread(
            self._base_url,
            self._token,
            self._username,
            vf,
            self._fw_search_models,
        )
        self._search_thread.finished_search.connect(self._on_search_done)
        self._search_thread.start()

    @Slot(bool, object, str)
    def _on_search_done(self, ok: bool, flat: object, err: str) -> None:
        self._btn_search.setEnabled(True)
        if not ok:
            self._status.setText(err or "Search failed.")
            return
        rows = flat if isinstance(flat, list) else []
        self._results = [normalize_firmware_search_row(r) for r in rows]
        self._list.clear()
        for folder, fn, sz, md in self._results:
            line = _row_display_line(folder, fn, sz, md)
            it = QListWidgetItem(line)
            it.setData(Qt.ItemDataRole.UserRole, (folder, fn))
            it.setToolTip(f"{folder}\n{fn}")
            self._list.addItem(it)
        self._status.setText(f"{len(self._results)} build(s) found." if self._results else "No matching builds.")
        self._on_result_selection()

    @Slot()
    def _on_result_selection(self) -> None:
        items = self._list.selectedItems()
        if not items:
            self._sel_folder = None
            self._sel_file = None
            self._btn_download.setEnabled(False)
            self._update_download_button_label()
            return
        data = items[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, (tuple, list)) or len(data) != 2:
            self._sel_folder = None
            self._sel_file = None
            self._btn_download.setEnabled(False)
            self._update_download_button_label()
            return
        self._sel_folder, self._sel_file = str(data[0]), str(data[1])
        self._btn_download.setEnabled(True)
        self._update_download_button_label()

    @Slot()
    def _on_download(self) -> None:
        folder = sanitize_server_folder_name((self._combo_folder.currentText() or "").strip())
        if not folder:
            QMessageBox.warning(self, "Download firmware", "Enter a valid server folder name.")
            return
        if not self._sel_folder or not self._sel_file:
            QMessageBox.warning(self, "Download firmware", "Select a row in the results list.")
            return

        ok_c, err_c = self._load_credentials()
        if not ok_c:
            if err_c:
                QMessageBox.warning(self, "Download firmware", err_c)
            else:
                QMessageBox.information(
                    self,
                    "Download firmware",
                    "No Artifactory credentials configured. Set them up in the FW Wizard first.",
                )
            return

        env_dir = os.path.abspath(os.path.join(self._fw_root, folder))
        try:
            cls = classify_local_firmware_vs_selection(
                env_dir, self._sel_folder, self._sel_file
            )
        except Exception:
            cls = "different_present"
        if cls == "exact_match":
            try:
                loc = firmware_folder_version_label(env_dir)
            except Exception:
                loc = folder
            QMessageBox.information(
                self,
                "Download firmware",
                f"Firmware {loc} already exists in folder '{folder}'.\n\nSkipping download.",
            )
            self._on_complete()
            self._maybe_prompt_start_server()
            return

        vmc = self._layout_vmc()
        ok_setup, msg_or_env, binaries_base, _pb, updaterules_dir, archive_dir = prepare_env_directories(
            self._fw_root, folder, vmc, self._fw_search_models
        )
        if not ok_setup:
            QMessageBox.warning(self, "Download firmware", msg_or_env)
            return

        art_target = self._artifactory_download_model()
        download_model = compute_download_model(
            self._sel_folder, self._sel_file, art_target.strip()
        )
        binaries_dir_for_download = os.path.join(msg_or_env, "binaries", vmc)
        chosen_binaries_dir = os.path.abspath(os.path.join(binaries_base, vmc))
        archive_path = os.path.abspath(os.path.join(archive_dir, self._sel_file))
        rules_dir = os.path.abspath(updaterules_dir)

        self._btn_download.setEnabled(False)
        self._btn_search.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)
        self._status.setText("Downloading…")

        self._download_thread = _DownloadThread(
            self._token,
            download_model,
            self._sel_folder,
            binaries_dir_for_download,
            updaterules_dir,
            archive_dir,
            self._base_url,
            self._username,
            self._sel_file,
            archive_path,
            chosen_binaries_dir,
            rules_dir,
        )
        self._download_thread.byte_progress.connect(self._on_dl_bytes)
        self._download_thread.status_text.connect(self._on_dl_status)
        self._download_thread.finished_ok.connect(self._on_dl_ok)
        self._download_thread.failed.connect(self._on_dl_failed)
        self._download_thread.start()

    @Slot(int, object)
    def _on_dl_bytes(self, done: int, total: object) -> None:
        tot = int(total) if total is not None else None
        if tot and tot > 0:
            self._progress.setRange(0, tot)
            self._progress.setValue(min(done, tot))
        else:
            self._progress.setRange(0, 0)

    @Slot(str)
    def _on_dl_status(self, text: str) -> None:
        self._status.setText(text)

    @Slot()
    def _on_dl_ok(self) -> None:
        self._progress.setRange(0, 100)
        self._progress.setValue(100)
        self._status.setText("Download and extraction complete.")
        self._btn_search.setEnabled(True)
        self._btn_download.setEnabled(True)
        self._on_complete()
        self._maybe_prompt_start_server()

    @Slot(str)
    def _on_dl_failed(self, msg: str) -> None:
        self._progress.setVisible(False)
        self._status.setText(msg)
        self._btn_search.setEnabled(True)
        self._btn_download.setEnabled(True)
        QMessageBox.warning(self, "Download firmware", msg)

    def _maybe_prompt_start_server(self) -> None:
        from core.local_server import check_server_status, get_base_url_if_serving_root

        root_abs = os.path.abspath(self._fw_root)
        running_here, _ = check_server_status()
        if running_here or get_base_url_if_serving_root(root_abs):
            return
        r = QMessageBox.question(
            self,
            "Local Server",
            "Start the server to serve this firmware?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        ok, msg = start_http_server(root_abs, DEFAULT_PORT)
        if not ok:
            QMessageBox.warning(self, "Local Server", msg or "Could not start server.")
            return
        # Refresh runs before this prompt; starting the server here left the toolbar stale.
        self._on_complete()
