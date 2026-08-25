from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from app_test_agent.harmony_hdc_bridge import BridgeError, HdcBridgeServer, HdcBridgeService, _hdc_provision_phone, _hdc_rport


class FakeDevice:
    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.calls: list[tuple[object, ...]] = []

    def dump_hierarchy(self) -> str:
        return '{"attributes":{"text":"Root"},"children":[{"attributes":{"description":"Target"}}]}'

    def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"jpeg")

    def app_start(self, package_name: str) -> None:
        self.calls.append(("open_app", package_name))

    def click(self, x: int, y: int) -> None:
        self.calls.append(("click", x, y))

    def input(self, text: str) -> None:
        self.calls.append(("input", text))

    def swipe_with_coords(self, *coords: int) -> None:
        self.calls.append(("swipe", *coords))

    def keyevent(self, value: str) -> None:
        self.calls.append(("key", value))


def _service() -> tuple[HdcBridgeService, FakeDevice]:
    holder: dict[str, FakeDevice] = {}

    def factory(serial: str) -> FakeDevice:
        holder["device"] = FakeDevice(serial)
        return holder["device"]

    service = HdcBridgeService(serial="device-1", token="t" * 16, device_factory=factory)
    return service, holder["device"]


def test_bridge_binds_session_to_configured_serial_and_observes():
    service, _ = _service()
    session = service.create_session("device-1")["session_id"]
    observation = service.execute(session, "observe", {})
    assert observation["source"] == "pc_hdc_bridge"
    assert observation["visible_texts"] == ["Root", "Target"]
    assert base64.b64decode(str(observation["screenshot_base64"])) == b"jpeg"
    with pytest.raises(BridgeError, match="not bound"):
        service.create_session("other-device")


def test_bridge_allows_only_primitive_io_and_never_exposes_token():
    service, device = _service()
    session = service.create_session("")["session_id"]
    service.execute(session, "click", {"x": 100.9, "y": 200})
    service.execute(session, "input", {"text": "hello"})
    assert device.calls == [("click", 100, 200), ("input", "hello")]
    with pytest.raises(BridgeError, match="unsupported bridge action"):
        service.execute(session, "execute_decider_action", {})
    assert "t" * 16 not in json.dumps(service.create_session(""))


def test_rport_uses_fixed_serial_and_never_shells_out(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return type('Result', (), {'returncode': 0, 'stderr': '', 'stdout': ''})()

    monkeypatch.setattr('app_test_agent.harmony_hdc_bridge.subprocess.run', fake_run)
    _hdc_rport('device-1', 19125, 9125)
    _hdc_rport('device-1', 19125, 9125, remove=True)
    assert calls[0][0] == ['hdc', '-t', 'device-1', 'rport', 'tcp:19125', 'tcp:9125']
    assert calls[1][0] == ['hdc', '-t', 'device-1', 'rport', 'rm', 'tcp:19125', 'tcp:9125']


def test_provision_phone_passes_current_session_without_console_output(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return type('Result', (), {'returncode': 0, 'stderr': '', 'stdout': ''})()

    monkeypatch.setattr('app_test_agent.harmony_hdc_bridge.subprocess.run', fake_run)
    _hdc_provision_phone('device-1', 'com.example.probe', 19125, 't' * 24)
    assert calls[0][0] == [
        'hdc', '-t', 'device-1', 'shell', 'aa', 'start', '-a', 'EntryAbility', '-b', 'com.example.probe', '-m', 'entry',
        '--ps', 'mobiagent_bridge_url', 'http://127.0.0.1:19125',
        '--ps', 'mobiagent_bridge_serial', 'device-1',
        '--ps', 'mobiagent_bridge_token', 't' * 24,
    ]


def test_provision_phone_can_request_readonly_smoke(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return type('Result', (), {'returncode': 0, 'stderr': '', 'stdout': ''})()

    monkeypatch.setattr('app_test_agent.harmony_hdc_bridge.subprocess.run', fake_run)
    _hdc_provision_phone('device-1', 'com.example.probe', 19125, 't' * 24, readonly_smoke=True)
    assert calls[0][0][-3:] == ['--pb', 'mobiagent_readonly_smoke', 'true']


def test_provision_phone_keeps_model_credential_out_of_output(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return type('Result', (), {'returncode': 0, 'stderr': '', 'stdout': ''})()

    monkeypatch.setattr('app_test_agent.harmony_hdc_bridge.subprocess.run', fake_run)
    _hdc_provision_phone('device-1', 'com.example.probe', 19125, 't' * 24, model_api_key='k' * 24)
    assert calls[0][0][-3:] == ['--ps', 'mobiagent_model_api_key', 'k' * 24]
    assert calls[0][1]['capture_output'] is True


def test_http_bridge_requires_token_and_session_before_io():
    service, device = _service()
    server = HdcBridgeServer(service, port=0)
    server.start()
    url = f'http://127.0.0.1:{server.port}'
    try:
        with pytest.raises(HTTPError) as unauthorized:
            urlopen(Request(f'{url}/v1/sessions', data=b'{}', method='POST'), timeout=5)
        assert unauthorized.value.code == 401
        request = Request(
            f'{url}/v1/sessions',
            data=b'{"serial":"device-1"}',
            method='POST',
            headers={'Content-Type': 'application/json', 'X-MobiAgent-Bridge-Token': 't' * 16},
        )
        with urlopen(request, timeout=5) as response:
            session = json.loads(response.read())['result']['session_id']
        io = Request(
            f'{url}/v1/io',
            data=json.dumps({'session_id': session, 'action': 'press_home', 'payload': {}}).encode(),
            method='POST',
            headers={'Content-Type': 'application/json', 'X-MobiAgent-Bridge-Token': 't' * 16},
        )
        with urlopen(io, timeout=5) as response:
            assert json.loads(response.read())['ok'] is True
        assert device.calls == [('key', 'HOME')]
    finally:
        server.close()
