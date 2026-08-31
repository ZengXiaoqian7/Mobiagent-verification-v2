"""Dependency and command diagnostics for PC verifier runtime profiles."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import shutil
from typing import Any


@dataclass(frozen=True)
class EnvironmentRequirement:
    name: str
    kind: str
    available: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "available": self.available,
        }


@dataclass(frozen=True)
class EnvironmentReport:
    profile: str
    ready: bool
    requirements: tuple[EnvironmentRequirement, ...]

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.requirements if not item.available)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "pc-verifier-environment-report-v1",
            "profile": self.profile,
            "ready": self.ready,
            "missing": list(self.missing),
            "requirements": [item.as_dict() for item in self.requirements],
        }


_CORE_MODULES = ("PIL", "requests", "yaml")
_PROFILE_MODULES = {
    "core": _CORE_MODULES,
    "test": (*_CORE_MODULES, "pytest", "numpy", "cv2"),
    "package": (*_CORE_MODULES, "PyInstaller", "numpy", "cv2"),
    "android": (*_CORE_MODULES, "openai", "uiautomator2", "cv2", "dotenv"),
    "harmony": (*_CORE_MODULES, "openai", "hmdriver2", "cv2", "dotenv"),
}
_PROFILE_COMMANDS = {
    "core": (),
    "test": (),
    "package": (),
    "android": ("adb",),
    "harmony": ("hdc",),
}
ENVIRONMENT_PROFILES = tuple(sorted(_PROFILE_MODULES))


def _module_available(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:  # noqa: BLE001 - any import-time failure means the runtime is unusable.
        return False


def check_environment(profile: str) -> EnvironmentReport:
    normalized = str(profile).strip().lower()
    if normalized not in _PROFILE_MODULES:
        supported = ", ".join(sorted(_PROFILE_MODULES))
        raise ValueError(f"unsupported environment profile {profile!r}; choose: {supported}")
    requirements = [
        EnvironmentRequirement(name, "python_module", _module_available(name))
        for name in _PROFILE_MODULES[normalized]
    ]
    requirements.extend(
        EnvironmentRequirement(name, "command", shutil.which(name) is not None)
        for name in _PROFILE_COMMANDS[normalized]
    )
    frozen = tuple(requirements)
    return EnvironmentReport(
        profile=normalized,
        ready=all(item.available for item in frozen),
        requirements=frozen,
    )


__all__ = [
    "ENVIRONMENT_PROFILES",
    "EnvironmentReport",
    "EnvironmentRequirement",
    "check_environment",
]
