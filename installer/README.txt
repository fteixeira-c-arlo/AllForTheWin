Installer assets (Inno Setup 6)
================================

Files here
----------
  ArloCameraControl.iss  — installer script (paths assume repo layout: dist\ from repo root).
  InstallWizardIntro.txt — text shown on the wizard’s first page.
  README.txt             — this file.

Build the Windows installer
---------------------------
From the repository root (parent of this folder):

  powershell -ExecutionPolicy Bypass -File .\build_installer.ps1

Requires Python 3.10+, pip packages from requirements.txt + requirements-build.txt,
and Inno Setup 6 for the single-file Setup .exe.

Full maintainer steps: ..\docs\BUILD_AND_RELEASE.txt
