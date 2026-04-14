"""Qt dialog: create firmware server root when missing."""
from __future__ import annotations

import os

from PySide6.QtWidgets import QFileDialog, QMessageBox, QPushButton, QWidget

from core.fw_server_prefs import (
    create_fw_server_root_directory,
    recommended_user_fw_server_root,
    save_fw_server_root,
    uses_env_fw_server_root,
)
def qt_ensure_fw_server_root(parent: QWidget | None, configured_root: str) -> str | None:
    """
    If ``configured_root`` exists as a directory, return its absolute path.
    Otherwise offer to create the recommended folder, the configured path, or browse.
    """
    cur = (configured_root or "").strip()
    if cur and os.path.isdir(cur):
        return os.path.abspath(cur)

    env_on = uses_env_fw_server_root()
    rec = recommended_user_fw_server_root()

    box = QMessageBox(parent)
    box.setWindowTitle("Firmware server folder")
    box.setIcon(QMessageBox.Icon.Question)
    if env_on:
        box.setText(
            "FW_SERVER_ROOT points to a folder that does not exist yet:\n"
            f"{cur or rec}\n\nWhat would you like to do?"
        )
    else:
        box.setText(
            "No firmware server folder yet.\n\n"
            f"Configured path:\n{cur or '(none)'}\n\n"
            f"Recommended first-time location:\n{rec}"
        )
    btn_rec = box.addButton("Create recommended", QMessageBox.ButtonRole.AcceptRole)
    btn_browse = box.addButton("Choose folder…", QMessageBox.ButtonRole.ActionRole)
    btn_cfg: QPushButton | None = None
    if cur:
        btn_cfg = box.addButton("Create configured path", QMessageBox.ButtonRole.ActionRole)
    box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.exec()

    clicked = box.clickedButton()

    def _save(path: str) -> str | None:
        ok, err = create_fw_server_root_directory(path)
        if not ok:
            QMessageBox.warning(parent, "Firmware server folder", err or "Could not create folder.")
            return None
        if not env_on:
            save_fw_server_root(path)
        return os.path.abspath(path)

    if clicked is None or box.buttonRole(clicked) == QMessageBox.ButtonRole.RejectRole:
        return None
    if clicked is btn_rec:
        return _save(rec if not env_on else (cur or rec))
    if btn_cfg is not None and clicked is btn_cfg and cur:
        return _save(cur)
    if clicked is btn_browse:
        start = rec if not env_on else (cur or rec)
        parent_dir = os.path.dirname(start)
        start = start if parent_dir and os.path.isdir(parent_dir) else rec
        picked = QFileDialog.getExistingDirectory(
            parent,
            "Select firmware server root (empty folder is OK)",
            start,
        )
        if not picked:
            return None
        return _save(picked)
    return None
