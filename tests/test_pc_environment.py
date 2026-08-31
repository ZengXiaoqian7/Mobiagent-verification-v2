import pytest

import pc_client.environment as environment


def test_core_environment_is_ready_in_test_runtime():
    report = environment.check_environment("core")

    assert report.profile == "core"
    assert report.ready is True
    assert report.missing == ()


def test_device_environment_reports_all_missing_requirements(monkeypatch):
    monkeypatch.setattr(
        environment,
        "_module_available",
        lambda name: name not in {"openai", "uiautomator2"},
    )
    monkeypatch.setattr(environment.shutil, "which", lambda _name: None)

    report = environment.check_environment("android")

    assert report.ready is False
    assert report.missing == ("openai", "uiautomator2", "adb")
    assert report.as_dict()["schema_version"] == "pc-verifier-environment-report-v1"


def test_environment_rejects_unknown_profile():
    with pytest.raises(ValueError, match="unsupported environment profile"):
        environment.check_environment("unknown")


def test_environment_profiles_are_stable_and_include_real_devices():
    assert environment.ENVIRONMENT_PROFILES == (
        "android",
        "core",
        "harmony",
        "package",
        "test",
    )


def test_module_check_requires_a_successful_import(monkeypatch):
    def broken_import(_name):
        raise OSError("dependent DLL is missing")

    monkeypatch.setattr(environment.importlib, "import_module", broken_import)

    assert environment._module_available("present_but_broken") is False
