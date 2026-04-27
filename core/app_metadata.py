"""Shared application name and version (no Qt imports).

The version is sourced from utils.version (single source of truth, rewritten by
the release workflow at tag-push time). This module re-exports it as APP_VERSION
to preserve the existing import surface.
"""
from utils.version import __version__

APP_NAME = "ArloHub"
APP_VERSION = __version__
