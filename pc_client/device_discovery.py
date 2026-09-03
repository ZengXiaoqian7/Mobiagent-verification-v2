"""Read-only discovery of Android and HarmonyOS devices for the desktop client."""

from __future__ import annotations

from dataclasses import dataclass
import shutil
import subprocess
from typing import Callable, Sequence


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ConnectedDevice:
    """A device target that can be passed to the canonical execution pipeline."""

    platform: str
    serial: str
    state: str = "device"

    @property
    def label(self) -> str:
        return f"{self.platform} · {self.serial}"


@dataclass(frozen=True)
class DeviceDiscoveryResult:
    devices: tuple[ConnectedDevice, ...]
    diagnostics: tuple[str, ...]


def discover_connected_devices(
    *,
    command_runner: CommandRunner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    timeout_seconds: float = 5.0,
) -> DeviceDiscoveryResult:
    """Return currently connected devices without sending any device mutation.

    ``hdc list targets`` and ``adb devices`` are connection-list commands only.
    Command failures are returned as diagnostics so a desktop client can remain
    usable for a manually entered serial or an offline replay.
    """

    devices: list[ConnectedDevice] = []
    diagnostics: list[str] = []
    devices.extend(
        _discover_with_command(
            platform="Harmony",
            executable="hdc",
            arguments=("list", "targets"),
            parser=_parse_hdc_targets,
            command_runner=command_runner,
            which=which,
            timeout_seconds=timeout_seconds,
            diagnostics=diagnostics,
        )
    )
    devices.extend(
        _discover_with_command(
            platform="Android",
            executable="adb",
            arguments=("devices",),
            parser=_parse_adb_devices,
            command_runner=command_runner,
            which=which,
            timeout_seconds=timeout_seconds,
            diagnostics=diagnostics,
        )
    )
    unique = {(item.platform, item.serial): item for item in devices}
    return DeviceDiscoveryResult(
        devices=tuple(sorted(unique.values(), key=lambda item: (item.platform, item.serial))),
        diagnostics=tuple(diagnostics),
    )


def _discover_with_command(
    *,
    platform: str,
    executable: str,
    arguments: Sequence[str],
    parser: Callable[[str], tuple[ConnectedDevice, ...]],
    command_runner: CommandRunner,
    which: Callable[[str], str | None],
    timeout_seconds: float,
    diagnostics: list[str],
) -> tuple[ConnectedDevice, ...]:
    if which(executable) is None:
        diagnostics.append(f"{executable} is not available on PATH")
        return ()
    try:
        completed = command_runner(
            [executable, *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        diagnostics.append(f"{executable} discovery failed: {exc}")
        return ()
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        diagnostics.append(f"{executable} discovery returned {completed.returncode}: {detail}")
        return ()
    return parser(completed.stdout or "")


def _parse_hdc_targets(output: str) -> tuple[ConnectedDevice, ...]:
    serials = [
        line.strip()
        for line in output.splitlines()
        if line.strip() and line.strip() not in {"[Empty]", "[empty]"}
    ]
    return tuple(ConnectedDevice(platform="Harmony", serial=serial) for serial in serials)


def _parse_adb_devices(output: str) -> tuple[ConnectedDevice, ...]:
    devices: list[ConnectedDevice] = []
    for line in output.splitlines():
        fields = line.strip().split()
        if len(fields) < 2 or fields[0].lower() == "list":
            continue
        serial, state = fields[0], fields[1]
        if state == "device":
            devices.append(ConnectedDevice(platform="Android", serial=serial, state=state))
    return tuple(devices)


__all__ = [
    "ConnectedDevice",
    "DeviceDiscoveryResult",
    "discover_connected_devices",
]
