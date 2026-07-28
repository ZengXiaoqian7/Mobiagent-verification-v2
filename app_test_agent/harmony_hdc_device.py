"""Minimal HarmonyOS hdc device adapter for App-test execution.

This fallback keeps App-test real-device runs independent from the full legacy
MobiAgent import graph.  It intentionally exposes only the primitive operations
used by the step executor and the read-only verification runner.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import time


class HdcHarmonyDevice:
    def __init__(self, serial: str | None = None):
        self.serial = serial
        result = self._run("list", "targets")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        if serial and serial not in result.stdout:
            raise RuntimeError(f"Harmony device serial is not connected: {serial}")

    def app_start(self, package_name: str) -> None:
        result = self._shell("aa", "start", "-b", package_name, "-a", "EntryAbility")
        if result.returncode != 0:
            result = self._shell("aa", "start", "-b", package_name)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        time.sleep(1.5)

    def start_app(self, app: str) -> None:
        self.app_start(app)

    def screenshot(self, path: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        remote = f"/data/local/tmp/app_test_{int(time.time() * 1000)}.jpeg"
        result = self._shell("snapshot_display", "-f", remote)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        recv = self._run("file", "recv", remote, str(target))
        if recv.returncode != 0:
            raise RuntimeError(recv.stderr.strip() or recv.stdout.strip())

    def click(self, x: int, y: int) -> None:
        result = self._shell("uitest", "uiInput", "click", str(int(x)), str(int(y)))
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        time.sleep(1.0)

    def input(self, text: str) -> None:
        result = self._shell("uitest", "uiInput", "text", text)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        time.sleep(0.5)

    def swipe(self, direction: str, scale: float = 0.5) -> None:
        del scale
        width, height = 1080, 2444
        if direction.lower() == "up":
            coords = (width // 2, int(height * 0.78), width // 2, int(height * 0.28))
        elif direction.lower() == "down":
            coords = (width // 2, int(height * 0.28), width // 2, int(height * 0.78))
        elif direction.lower() == "left":
            coords = (int(width * 0.78), height // 2, int(width * 0.22), height // 2)
        elif direction.lower() == "right":
            coords = (int(width * 0.22), height // 2, int(width * 0.78), height // 2)
        else:
            raise RuntimeError(f"unsupported swipe direction: {direction}")
        self.swipe_with_coords(*coords)

    def swipe_with_coords(self, start_x: int, start_y: int, end_x: int, end_y: int) -> None:
        result = self._shell(
            "uitest",
            "uiInput",
            "swipe",
            str(int(start_x)),
            str(int(start_y)),
            str(int(end_x)),
            str(int(end_y)),
            "1000",
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        time.sleep(1.0)

    def keyevent(self, key: str | int) -> None:
        result = self._shell("uitest", "uiInput", "keyEvent", str(key))
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        time.sleep(0.5)

    def dump_hierarchy(self) -> str:
        result = self._shell("uitest", "dumpLayout")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        match = re.search(r"(/data/local/tmp/layout_[^\s]+\.json)", result.stdout)
        if not match:
            raise RuntimeError("uitest dumpLayout did not report a layout JSON path")
        remote = match.group(1)
        cat = self._shell("cat", remote)
        if cat.returncode != 0:
            raise RuntimeError(cat.stderr.strip() or cat.stdout.strip())
        return cat.stdout

    def _shell(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self._run("shell", *args)

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = ["hdc"]
        if self.serial:
            command.extend(["-t", self.serial])
        command.extend(args)
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
