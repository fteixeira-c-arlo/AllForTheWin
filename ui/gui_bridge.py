"""Cross-thread GUI bridge: log stream + blocking prompts from worker via BlockingQueuedConnection."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal, QMetaObject, Qt

_SELECT_CANCELLED = object()


class GuiBridge(QObject):
    """Emits log lines from any thread; blocking ask_* methods must run from a non-GUI thread."""

    append_log = Signal(str)
    tail_live_start = Signal(str, str)
    tail_live_stop = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._main: Any = None
        # Dialog scratch (read/written on GUI thread in slots; worker sets before invokeMethod)
        self._text_prompt = ""
        self._text_default = ""
        self._text_result: str | None = None
        self._pwd_prompt = ""
        self._pwd_result: str | None = None
        self._confirm_message = ""
        self._confirm_default = False
        self._confirm_result = False
        self._select_title = ""
        self._select_labels: list[str] = []
        self._select_values: list[Any] = []
        self._select_result: Any = None

    def set_main_window(self, main: Any) -> None:
        self._main = main

    def ask_text(self, prompt: str, default: str = "") -> str | None:
        if not self._main:
            return None
        self._text_prompt = prompt
        self._text_default = default
        self._text_result = None
        QMetaObject.invokeMethod(
            self._main,
            "guiBlockingAskText",
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        return self._text_result

    def ask_password(self, prompt: str) -> str | None:
        if not self._main:
            return None
        self._pwd_prompt = prompt
        self._pwd_result = None
        QMetaObject.invokeMethod(
            self._main,
            "guiBlockingAskPassword",
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        return self._pwd_result

    def ask_confirm(self, message: str, default: bool = False) -> bool:
        if not self._main:
            return False
        self._confirm_message = message
        self._confirm_default = default
        QMetaObject.invokeMethod(
            self._main,
            "guiBlockingAskConfirm",
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        return self._confirm_result

    def ask_select(self, title: str, items: list[tuple[str, Any]]) -> Any | None:
        """items: (label, value). Returns selected value or None if cancelled."""
        if not self._main or not items:
            return None
        if len(items) == 1:
            return items[0][1]
        self._select_title = title
        self._select_labels = [x[0] for x in items]
        self._select_values = [x[1] for x in items]
        self._select_result = _SELECT_CANCELLED
        QMetaObject.invokeMethod(
            self._main,
            "guiBlockingAskSelect",
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        if self._select_result is _SELECT_CANCELLED:
            return None
        return self._select_result

    def log_plain(self, text: str) -> None:
        """Thread-safe: emit log from worker or GUI thread."""
        if text:
            self.append_log.emit(text)
