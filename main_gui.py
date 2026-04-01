"""Arlo Camera Control — graphical UI (PySide6)."""
import sys


def main() -> None:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    from PySide6.QtWidgets import QApplication

    from ui.gui_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Arlo Camera Control")
    app.setOrganizationName("Arlo")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
