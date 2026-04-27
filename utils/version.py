"""Single source of truth for the ArloHub version.

Read by:
  - build_installer.ps1 (passes to Inno Setup as MyAppVersion)
  - core/updater.py (compares against the latest GitHub Release)

The GitHub Actions release workflow rewrites this file from the pushed git tag
(e.g. tag `v1.0.5` -> `__version__ = "1.0.5"`) before building, so the value
committed here only matters for local builds.
"""
__version__ = "1.0.0"
