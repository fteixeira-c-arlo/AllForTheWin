"""ArloShell — graphical UI (PySide6)."""
from __future__ import annotations

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

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from interface.gui_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("ArloShell")
    app.setOrganizationName("Arlo")
    icon_path = _app_icon_path()
    if icon_path is not None:
        icon = QIcon(str(icon_path))
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
