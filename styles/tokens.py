"""Design tokens for the command panel (use in Python; mirror values in styles/panel.qss)."""
from __future__ import annotations

BACKGROUND_BASE = "#12131a"
BACKGROUND_SURFACE = "#1a1c27"
BACKGROUND_HOVER = "#1e2030"
BORDER_SUBTLE = "#2a2d3a"
BORDER_DIVIDER = "#1e2030"

TEXT_PRIMARY = "#c8ccde"
TEXT_SECONDARY = "#8b90a8"
TEXT_MUTED = "#444760"

ACCENT_FW = "#5DCAA5"
ACCENT_DEV = "#5ba3e0"
ACCENT_LOG = "#EF9F27"
ACCENT_NET = "#AFA9EC"

BADGE_WARN_BG = "#2e2418"
BADGE_WARN_FG = "#EF9F27"
BADGE_DANGER_BG = "#2a1010"
BADGE_DANGER_FG = "#F09595"

CONNECTED_COLOR = "#1d9e75"

# Header status strip (shared with command panel chrome)
STATUS_DOT_DISCONNECTED = "#e05555"
STATUS_DOT_CONNECTING = "#e0a535"
# Prefer panel token when showing connected state in new UI
STATUS_DOT_CONNECTED = CONNECTED_COLOR

PANEL_FIXED_WIDTH = 260
CMD_ROW_HEIGHT_PX = 30
SECTION_ICON_PX = 18
FOOTER_PROMPT_FONT_PX = 11
BASE_FONT_PX = 12
SECTION_LABEL_FONT_PX = 9
