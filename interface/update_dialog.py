"""Update dialog: shows release notes, downloads the installer with progress, then launches it."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QProgressBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.updater import UpdateInfo, download, launch_installer
from core.updater_config import clear_postpone, postpone
from utils.version import __version__


class _DownloadWorker(QObject):
    """Runs the download in a QThread and emits progress/result via signals."""

    progress = Signal(int, int)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, info: UpdateInfo) -> None:
        super().__init__()
        self._info = info

    def run(self) -> None:
        try:
            path = download(self._info, progress=lambda d, t: self.progress.emit(d, t))
            self.finished.emit(str(path))
        except Exception as e:
            self.failed.emit(str(e))


class UpdateDialog(QDialog):
    """Modal dialog: 'Install now' downloads, verifies, launches installer, and quits the app.

    Clicking 'Later' records the version via core.updater_config.postpone(),
    which suppresses the auto-prompt for the next 24h while the same version is
    still the latest. A different (newer) version surfaces the dialog again.
    """

    def __init__(self, info: UpdateInfo, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._info = info
        self._thread: Optional[QThread] = None
        self._worker: Optional[_DownloadWorker] = None

        self.setWindowTitle("ArloHub Update")
        self.setModal(True)
        self.resize(560, 420)

        layout = QVBoxLayout(self)

        channel_suffix = "" if info.channel == "stable" else f"  ({info.channel})"
        layout.addWidget(QLabel(f"<b>New version available: {info.version}{channel_suffix}</b>"))
        layout.addWidget(QLabel(f"Current version: {__version__}"))

        notes = QTextEdit()
        notes.setReadOnly(True)
        notes.setMarkdown(info.notes or "_No release notes._")
        layout.addWidget(notes, 1)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        self._status = QLabel("")
        self._status.setVisible(False)
        layout.addWidget(self._status)

        buttons = QDialogButtonBox()
        self._install_btn = buttons.addButton(
            "Install now", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._later_btn = buttons.addButton(
            "Later", QDialogButtonBox.ButtonRole.RejectRole
        )
        self._install_btn.clicked.connect(self._start_download)
        self._later_btn.clicked.connect(self._on_later)
        layout.addWidget(buttons)

    def _on_later(self) -> None:
        try:
            postpone(self._info.version)
        except Exception:
            pass
        self.reject()

    def _start_download(self) -> None:
        self._install_btn.setEnabled(False)
        self._later_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._status.setVisible(True)
        self._status.setText("Downloading…")

        self._thread = QThread(self)
        self._worker = _DownloadWorker(self._info)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.start()

    def _on_progress(self, downloaded: int, total: int) -> None:
        mb_done = downloaded // (1024 * 1024)
        if total > 0:
            self._progress.setRange(0, 100)
            self._progress.setValue(int(downloaded * 100 / total))
            mb_total = total // (1024 * 1024)
            self._status.setText(f"Downloading… {mb_done} / {mb_total} MB")
        else:
            self._progress.setRange(0, 0)
            self._status.setText(f"Downloading… {mb_done} MB")

    def _on_finished(self, installer_path: str) -> None:
        self._status.setText("Starting installer… the app will restart in a few seconds.")
        try:
            launch_installer(Path(installer_path))
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to start installer:\n{e}")
            self._reset_buttons()
            return
        try:
            clear_postpone()
        except Exception:
            pass
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _on_failed(self, msg: str) -> None:
        QMessageBox.critical(self, "Download failed", msg)
        self._reset_buttons()

    def _reset_buttons(self) -> None:
        self._install_btn.setEnabled(True)
        self._later_btn.setEnabled(True)
        self._progress.setVisible(False)
        self._status.setVisible(False)


def show_no_update_message(parent: Optional[QWidget] = None, *, channel: str) -> None:
    """Used by the 'Help -> Check for updates' menu when no newer version is found."""
    suffix = "" if channel == "stable" else f" ({channel})"
    QMessageBox.information(
        parent,
        "ArloHub Update",
        f"You're on the latest version: <b>{__version__}</b>{suffix}.",
    )


def show_check_failed_message(parent: Optional[QWidget] = None) -> None:
    """Used by the manual check when the network/API failed."""
    QMessageBox.warning(
        parent,
        "ArloHub Update",
        "Could not reach GitHub. Check your connection and try again.",
    )
