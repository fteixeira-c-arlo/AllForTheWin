"""Global Qt styles: shared dark-theme tokens and application-wide QSS.

Qt Style Sheets are NOT full CSS. For QComboBox::drop-down, subcontrol-position must use
Qt keywords (e.g. top right) — values like \"center right\" are invalid and break the whole sheet.

Unsupported / unreliable in QSS includes:
  var(), cursor, opacity (as a property), gap, display:flex, align-items,
  justify-content, and many margin-* / letter-spacing combinations on QLabel
  (use layout spacing + QLabel { padding } only; prefer px over em for QSS lengths).

Use real hex/rgba colors, setCursor() in Python, and rgba() for translucency.
"""
from __future__ import annotations

import base64
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication, QPushButton, QWidget

# Aligned with gui_window / FW tooling
ARLO_ACCENT = "#00897B"
_MUTED_FG = "#8b95a5"
_TEXT = "#c5ced9"
_SURFACE = "#161a20"
_BORDER_SUBTLE = "rgba(255, 255, 255, 0.10)"


def install_stylesheet_debug() -> None:
    """Monkey-patch setStyleSheet on QWidget and QApplication (diagnostics).

    Enable by setting env ``ARLO_HUB_DEBUG_STYLESHEET`` (or legacy ``ARLO_SHELL_DEBUG_STYLESHEET``)
    to 1/true/yes before launch.
    """
    from PySide6.QtWidgets import QApplication, QWidget

    def _wrap(orig):
        def _debug_setStyleSheet(self, styleSheet: str) -> None:
            s = styleSheet or ""
            head = s[:100].replace("\n", " ").replace("\r", " ")
            print(f"DEBUG setStyleSheet {type(self).__name__}: {head}")
            orig(self, s)

        return _debug_setStyleSheet

    for cls in (QWidget, QApplication):
        cls.setStyleSheet = _wrap(cls.setStyleSheet)  # type: ignore[method-assign, assignment]


# SVG chevron for QComboBox::down-arrow (visible on dark themes). Use base64, not percent-encoding:
# raw quotes in url(...) break parsing; '%' in percent-encoded payloads is mishandled by Qt QSS.
_COMBO_DROPDOWN_CHEVRON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
    '<path d="M2 4l4 4 4-4" fill="none" stroke="#a0aec0" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
_COMBO_DROPDOWN_CHEVRON_SVG_URL = (
    "data:image/svg+xml;base64,"
    + base64.b64encode(_COMBO_DROPDOWN_CHEVRON_SVG.encode("utf-8")).decode("ascii")
)


def qcombobox_dark_stylesheet(
    *,
    border_radius: int = 6,
    padding: str = "5px 10px",
    min_height: int = 22,
    dropdown_width: int = 22,
    font_size: str = "13px",
    include_dropdown_chevron: bool = False,
) -> str:
    """
    QComboBox + popup list (QAbstractItemView) for dark theme.
    Per-widget setStyleSheet replaces QApplication rules for that combo; include popup rules here.

    When include_dropdown_chevron is True, extra right padding and a ::down-arrow image are set so
    the control reads as a dropdown (global QSS often hides the native indicator on Windows).
    """
    a = ARLO_ACCENT
    br = int(border_radius)
    mh = int(min_height)
    dw = int(dropdown_width)
    pad = padding
    if include_dropdown_chevron:
        pad = "5px 28px 5px 10px"
        dw = max(dw, 24)
    drop_down = (
        f"QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right; "
        f"border: none; width: {dw}px; background-color: transparent; }}"
        if include_dropdown_chevron
        else f"QComboBox::drop-down {{ border: none; width: {dw}px; }}"
    )
    base = (
        f"QComboBox {{ background-color: #1a1f26; color: #e8eef4; "
        f"border: 1px solid {_BORDER_SUBTLE}; border-radius: {br}px; "
        f"padding: {pad}; min-height: {mh}px; font-size: {font_size}; }}"
        f"{drop_down}"
        f"QComboBox QAbstractItemView {{ background-color: #1a1f26; color: #e8eef4; "
        f"selection-background-color: {a}; selection-color: #e8eef4; "
        f"border: 1px solid {_BORDER_SUBTLE}; border-radius: 4px; padding: 4px; }}"
        "QComboBox QAbstractItemView::item { padding: 6px 10px; border-radius: 4px; }"
        "QComboBox QAbstractItemView::item:hover { background-color: rgba(255, 255, 255, 0.08); }"
        f"QComboBox QAbstractItemView::item:selected {{ background-color: {a}; color: #e8eef4; }}"
    )
    if not include_dropdown_chevron:
        return base
    u = _COMBO_DROPDOWN_CHEVRON_SVG_URL
    return (
        base
        + f'QComboBox::down-arrow {{ image: url("{u}"); width: 12px; height: 12px; }}'
    )


def polish_dynamic_properties(widget: QWidget) -> None:
    """Call after changing QObject dynamic properties used in QSS selectors."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def prepare_qframe_for_qss(frame: QWidget) -> None:
    """Before ``setStyleSheet`` on a ``QFrame``: plain frame + ``WA_StyledBackground`` (Windows parse/paint)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QFrame

    if isinstance(frame, QFrame):
        frame.setFrameShape(QFrame.Shape.NoFrame)
        frame.setFrameShadow(QFrame.Shadow.Plain)
        frame.setLineWidth(0)
        frame.setMidLineWidth(0)
    frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


def apply_qframe_stylesheet(frame: QWidget, stylesheet: str) -> None:
    """Run :func:`prepare_qframe_for_qss` then apply QSS."""
    prepare_qframe_for_qss(frame)
    frame.setStyleSheet(stylesheet)


def set_arlo_pushbutton_variant(
    btn: QPushButton,
    *,
    variant: str | None = None,
    compact: bool = False,
    nav: bool = False,
) -> None:
    """
    variant: None = outline secondary (global default).
    Use \"primary\" (teal), \"blue\" (indigo), or \"destructive\" for filled / special actions.
    """
    if variant:
        btn.setProperty("arloStyle", variant)
    else:
        btn.setProperty("arloStyle", None)
    btn.setProperty("arloCompact", compact)
    btn.setProperty("arloNav", nav)
    polish_dynamic_properties(btn)


def read_panel_qss() -> str:
    """Load command-panel QSS from disk (PyInstaller bundle or source tree)."""
    from interface.resources import resource_path

    return resource_path("styles", "panel.qss").read_text(encoding="utf-8")


def load_stylesheet(app: QApplication) -> None:
    """Apply global QSS plus ``styles/panel.qss`` once at startup."""
    app.setStyleSheet(global_application_stylesheet() + "\n" + read_panel_qss())


def global_application_stylesheet() -> str:
    """Application-wide QSS; keep widget-specific setStyleSheet for non-button widgets."""
    a = ARLO_ACCENT
    return f"""
    QPushButton {{
        background-color: transparent;
        color: {_TEXT};
        border: 1px solid {_BORDER_SUBTLE};
        border-radius: 6px;
        padding: 6px 14px;
        font-size: 13px;
    }}
    QPushButton:hover {{
        background-color: rgba(255, 255, 255, 0.07);
        border-color: rgba(0, 137, 123, 0.42);
        color: #e8eef4;
    }}
    QPushButton:pressed {{
        background-color: rgba(0, 0, 0, 0.28);
        border-color: rgba(0, 137, 123, 0.55);
        color: #f0f4f8;
    }}
    QPushButton:disabled {{
        color: rgba(197, 206, 217, 0.36);
        border-color: rgba(255, 255, 255, 0.06);
        background-color: rgba(255, 255, 255, 0.03);
    }}

    QPushButton[arloStyle="primary"] {{
        background-color: #0a6b63;
        color: #ffffff;
        border: 1px solid rgba(0, 137, 123, 0.55);
    }}
    QPushButton[arloStyle="primary"]:hover {{
        background-color: {a};
        border-color: rgba(0, 200, 180, 0.45);
    }}
    QPushButton[arloStyle="primary"]:pressed {{
        background-color: #005a54;
        border-color: rgba(0, 137, 123, 0.7);
    }}
    QPushButton[arloStyle="primary"]:disabled {{
        background-color: #252b33;
        color: rgba(255, 255, 255, 0.35);
        border-color: rgba(255, 255, 255, 0.06);
    }}

    QPushButton[arloStyle="blue"] {{
        background-color: #303f9f;
        color: #e8eaf6;
        border: 1px solid rgba(120, 140, 220, 0.35);
    }}
    QPushButton[arloStyle="blue"]:hover {{
        background-color: #3949ab;
        border-color: rgba(159, 168, 238, 0.45);
    }}
    QPushButton[arloStyle="blue"]:pressed {{
        background-color: #283593;
    }}
    QPushButton[arloStyle="blue"]:disabled {{
        background-color: #252b33;
        color: rgba(255, 255, 255, 0.35);
        border-color: rgba(255, 255, 255, 0.06);
    }}

    QPushButton[arloStyle="destructive"] {{
        background-color: transparent;
        color: #e57373;
        border: 1px solid rgba(229, 115, 115, 0.35);
    }}
    QPushButton[arloStyle="destructive"]:hover {{
        background-color: rgba(229, 115, 115, 0.12);
        border-color: rgba(229, 115, 115, 0.5);
        color: #ff8a80;
    }}
    QPushButton[arloStyle="destructive"]:pressed {{
        background-color: rgba(229, 115, 115, 0.2);
    }}
    QPushButton[arloStyle="destructive"]:disabled {{
        color: rgba(229, 115, 115, 0.35);
        border-color: rgba(229, 115, 115, 0.15);
    }}

    QPushButton[arloCompact="true"] {{
        padding: 4px 10px;
        font-size: 11px;
        border-radius: 5px;
    }}
    QPushButton[arloNav="true"] {{
        padding: 8px 20px;
        font-size: 13px;
    }}

    QPushButton:flat {{
        background-color: transparent;
        border: none;
    }}
    QPushButton:flat:hover {{
        background-color: rgba(255, 255, 255, 0.05);
        border: none;
    }}
    QPushButton:flat:pressed {{
        background-color: rgba(255, 255, 255, 0.08);
        border: none;
    }}
    QPushButton:flat:disabled {{
        background-color: transparent;
        color: rgba(122, 132, 148, 0.45);
    }}

    QComboBox {{
        background-color: #1a1f26;
        color: #e8eef4;
        border: 1px solid {_BORDER_SUBTLE};
        border-radius: 6px;
        padding: 5px 28px 5px 10px;
        min-height: 22px;
        font-size: 13px;
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        border: none;
        width: 24px;
        background-color: transparent;
    }}
    QComboBox::down-arrow {{
        image: url("{_COMBO_DROPDOWN_CHEVRON_SVG_URL}");
        width: 12px;
        height: 12px;
    }}
    QComboBox QAbstractItemView {{
        background-color: #1a1f26;
        color: #e8eef4;
        selection-background-color: {a};
        selection-color: #e8eef4;
        border: 1px solid {_BORDER_SUBTLE};
        border-radius: 4px;
        padding: 4px;
    }}
    QComboBox QAbstractItemView::item {{
        padding: 6px 10px;
        border-radius: 4px;
    }}
    QComboBox QAbstractItemView::item:hover {{
        background-color: rgba(255, 255, 255, 0.08);
    }}
    QComboBox QAbstractItemView::item:selected {{
        background-color: {a};
        color: #e8eef4;
    }}
    """
