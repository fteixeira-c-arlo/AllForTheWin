"""ArloHub — graphical UI (PySide6)."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _maybe_reexec_with_project_venv() -> None:
    """Use .venv when the script was started with a Python that lacks deps (e.g. double-click)."""
    if getattr(sys, "frozen", False):
        return
    here = Path(__file__).resolve()
    root = here.parent
    venv_python = (
        root / ".venv" / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else root / ".venv" / "bin" / "python"
    )
    if not venv_python.is_file():
        return
    try:
        if Path(sys.executable).resolve() == venv_python.resolve():
            return
    except OSError:
        return
    try:
        import PySide6  # noqa: F401
    except ModuleNotFoundError:
        os.execv(str(venv_python), [str(venv_python), str(here), *sys.argv[1:]])


def _app_icon_path() -> Path | None:
    """PNG next to source, or under PyInstaller bundle (datas → assets/)."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        base = Path(__file__).resolve().parent
    p = base / "assets" / "ArloShell_icon.png"
    return p if p.is_file() else None


def main() -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    from PySide6.QtCore import QEvent, QObject, Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QPushButton

    from interface.app_styles import install_stylesheet_debug, load_stylesheet
    from interface.gui_window import MainWindow, _load_icon

    _dbg = (
        os.environ.get("ARLO_HUB_DEBUG_STYLESHEET", "").strip()
        or os.environ.get("ARLO_SHELL_DEBUG_STYLESHEET", "").strip()
    )
    if _dbg.lower() in ("1", "true", "yes"):
        install_stylesheet_debug()

    class ArloHubApplication(QApplication):
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

    app = ArloHubApplication(sys.argv)
    load_stylesheet(app)
    # Windows / Fusion may leave default QFont with pointSize -1 (pixel-only); that propagates setPointSize(-1) warnings.
    _af = app.font()
    if _af.pointSize() <= 0 and _af.pixelSize() <= 0:
        _nf = QFont(_af)
        _nf.setPointSize(10)
        app.setFont(_nf)
    app.setApplicationName("ArloHub")
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
    return app.exec()


def _fatal_startup(exc: BaseException) -> None:
    """When the app is started by double-click, the console may vanish; surface the error."""
    import traceback

    msg = traceback.format_exc()
    base = Path(__file__).resolve().parent
    log_path = base / "arlohub_last_error.txt"
    try:
        log_path.write_text(msg, encoding="utf-8")
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            hint = ""
            if isinstance(exc, ModuleNotFoundError) and exc.name == "PySide6":
                hint = (
                    "\n\nRun setup_dependencies.bat once, then start with run_arlohub.bat "
                    "(or double-click main_gui.py again if .venv exists)."
                )
            ctypes.windll.user32.MessageBoxW(
                0,
                f"ArloHub failed to start.\n\n{exc!s}{hint}\n\nFull traceback:\n{log_path}",
                "ArloHub",
                0x10,
            )
        except Exception:
            pass
    else:
        print(msg, file=sys.stderr)


if __name__ == "__main__":
    _maybe_reexec_with_project_venv()
    try:
        rc = main()
    except Exception as e:
        _fatal_startup(e)
        sys.exit(1)
    sys.exit(rc)
