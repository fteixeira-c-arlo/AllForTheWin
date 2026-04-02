"""ArloShell — graphical UI (PySide6)."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _app_icon_path() -> Path | None:
    """PNG next to source, or under PyInstaller bundle (datas → assets/)."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        base = Path(__file__).resolve().parent
    p = base / "assets" / "ArloShell_icon.png"
    return p if p.is_file() else None


def main() -> None:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    from PySide6.QtCore import QEvent, QObject, Qt
    from PySide6.QtWidgets import QApplication, QPushButton

    from interface.app_styles import global_application_stylesheet, install_stylesheet_debug
    from interface.gui_window import MainWindow, _load_icon

    if os.environ.get("ARLO_SHELL_DEBUG_STYLESHEET", "").strip() in ("1", "true", "yes"):
        install_stylesheet_debug()

    class ArloShellApplication(QApplication):
        """Apply pointing-hand cursor to all QPushButtons (Qt QSS has no reliable cursor: pointer)."""

        def notify(self, receiver: QObject, event: QEvent) -> bool:  # type: ignore[override]
            if isinstance(receiver, QPushButton):
                et = event.type()
                if et in (
                    QEvent.Type.Show,
                    QEvent.Type.EnabledChange,
                    QEvent.Type.Hide,
                ):
                    if receiver.isEnabled():
                        receiver.setCursor(Qt.CursorShape.PointingHandCursor)
                    else:
                        receiver.setCursor(Qt.CursorShape.ForbiddenCursor)
            return super().notify(receiver, event)

    app = ArloShellApplication(sys.argv)
    app.setStyleSheet(global_application_stylesheet())
    app.setApplicationName("ArloShell")
    app.setOrganizationName("Arlo")
    icon_path = _app_icon_path()
    if icon_path is not None:
        icon = _load_icon(str(icon_path))
        app.setWindowIcon(icon)
        win = MainWindow()
        win.setWindowIcon(icon)
    else:
        win = MainWindow()
    win.setMinimumSize(900, 600)
    win.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
