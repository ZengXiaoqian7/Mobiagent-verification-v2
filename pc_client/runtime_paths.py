"""Runtime paths shared by source and PyInstaller launches."""

from __future__ import annotations

from pathlib import Path
import sys


SOURCE_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT)).resolve()
INSTALL_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else SOURCE_ROOT
)
DEFAULT_CASE_PATH = BUNDLE_ROOT / "examples" / "post_create_app_test.json"
DEFAULT_OUTPUT_ROOT = Path.home() / "MobiAgentVerifierPC" / "runs"
DEFAULT_RUNNER_ROOT = BUNDLE_ROOT


__all__ = [
    "BUNDLE_ROOT",
    "DEFAULT_CASE_PATH",
    "DEFAULT_OUTPUT_ROOT",
    "DEFAULT_RUNNER_ROOT",
    "INSTALL_ROOT",
    "SOURCE_ROOT",
]
