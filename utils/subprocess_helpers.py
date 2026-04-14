"""Windows: avoid flashing console windows when spawning CLI tools from a GUI / frozen exe."""
from __future__ import annotations

import subprocess
import sys
from typing import Any


def win_subprocess_kwargs() -> dict[str, Any]:
    """Spread into subprocess.run(..., **win_subprocess_kwargs()) and subprocess.Popen(...)."""
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
