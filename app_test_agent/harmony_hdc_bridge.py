"""Authenticated, session-bound HDC I/O bridge for a Harmony phone agent.

The bridge deliberately owns *only* device transport.  It neither receives
model credentials nor evaluates a testcase.  A phone-side AgentRuntime owns
the agent loop and remains the authority for its report.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import tempfile
import threading
import subprocess
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .harmony_hdc_device import HdcHarmonyDevice


BRIDGE_VERSION = "mobiagent-hdc-bridge-v1"


class BridgeError(RuntimeError):
    """An expected, structured bridge failure."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class BridgeSession:
    session_id: str
    serial: str


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgeError("INVALID_ARGUMENT", f"{name} must be numeric")
    return int(value)


def _visible_texts(value: object) -> list[str]:
    """Extract text fields from HDC dumpLayout without assuming one schema."""
    values: list[str] = []
    text_keys = {"text", "description", "hint", "contentdescription", "accessibilitytext"}

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                if key.lower() in text_keys and isinstance(child, str):
                    text = child.strip()
                    if text and text not in values:
                        values.append(text)
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return values


class HdcBridgeService:
    """Maps a fixed, authenticated RPC vocabulary to HDC primitives."""

    def __init__(
        self,
        *,
        serial: str,
        token: str,
        device_factory: Callable[[str], HdcHarmonyDevice] = HdcHarmonyDevice,
    ) -> None:
        if not _text(serial):
            raise ValueError("serial is required")
        if len(token) < 16:
            raise ValueError("bridge token must contain at least 16 characters")
        self._serial = serial
        self._token = token
        self._device_factory = device_factory
        self._device = device_factory(serial)
        self._sessions: dict[str, BridgeSession] = {}
        self._lock = threading.RLock()

    @property
    def serial(self) -> str:
        return self._serial

    def authorized(self, token: str | None) -> bool:
        return isinstance(token, str) and secrets.compare_digest(token, self._token)

    def create_session(self, requested_serial: object) -> dict[str, object]:
        serial = _text(requested_serial) or self._serial
        if serial != self._serial:
            raise BridgeError("SERIAL_MISMATCH", "requested device is not bound to this bridge", status=409)
        session = BridgeSession(secrets.token_urlsafe(24), self._serial)
        with self._lock:
            self._sessions[session.session_id] = session
        return {"session_id": session.session_id, "serial": session.serial, "bridge_version": BRIDGE_VERSION}

    def close_session(self, session_id: object) -> dict[str, object]:
        session = self._session(session_id)
        with self._lock:
            self._sessions.pop(session.session_id, None)
        return {"closed": True, "session_id": session.session_id}

    def execute(self, session_id: object, action: object, payload: object) -> dict[str, object]:
        self._session(session_id)
        name = _text(action).lower()
        arguments = payload if isinstance(payload, Mapping) else {}
        with self._lock:
            if name == "observe":
                return self._observe()
            if name == "open_app":
                package_name = _text(arguments.get("package_name"))
                if not package_name:
                    raise BridgeError("INVALID_ARGUMENT", "open_app requires package_name")
                self._device.app_start(package_name)
            elif name == "click":
                self._device.click(_number(arguments.get("x"), "x"), _number(arguments.get("y"), "y"))
            elif name == "input":
                text = arguments.get("text")
                if not isinstance(text, str):
                    raise BridgeError("INVALID_ARGUMENT", "input requires text")
                self._device.input(text)
            elif name == "swipe":
                self._device.swipe_with_coords(
                    _number(arguments.get("start_x"), "start_x"),
                    _number(arguments.get("start_y"), "start_y"),
                    _number(arguments.get("end_x"), "end_x"),
                    _number(arguments.get("end_y"), "end_y"),
                )
            elif name == "press_back":
                self._device.keyevent("BACK")
            elif name == "press_home":
                self._device.keyevent("HOME")
            elif name == "wait":
                # Waiting is local to the phone agent and intentionally does no PC work.
                return {"ok": True, "action": name}
            else:
                raise BridgeError("UNSUPPORTED_ACTION", f"unsupported bridge action: {name}")
        return {"ok": True, "action": name}

    def _session(self, value: object) -> BridgeSession:
        session_id = _text(value)
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise BridgeError("UNKNOWN_SESSION", "bridge session is missing or expired", status=409)
        return session

    def _observe(self) -> dict[str, object]:
        try:
            hierarchy_json = self._device.dump_hierarchy()
            hierarchy = json.loads(hierarchy_json)
        except Exception as error:
            raise BridgeError("OBSERVATION_FAILED", f"HDC hierarchy capture failed: {error}", status=502) from error
        with tempfile.TemporaryDirectory(prefix="mobiagent-hdc-observe-") as directory:
            image_path = Path(directory) / "screen.jpeg"
            try:
                self._device.screenshot(str(image_path))
                screenshot = base64.b64encode(image_path.read_bytes()).decode("ascii")
            except Exception as error:
                raise BridgeError("OBSERVATION_FAILED", f"HDC screenshot capture failed: {error}", status=502) from error
        return {
            "hierarchy_json": hierarchy_json,
            "visible_texts": _visible_texts(hierarchy),
            "screenshot_base64": screenshot,
            "source": "pc_hdc_bridge",
        }


class HdcBridgeServer:
    """Owns the HTTP server lifecycle; bind it before creating the HDC rport."""

    def __init__(self, service: HdcBridgeService, host: str = "127.0.0.1", port: int = 9125) -> None:
        self.service = service
        self._http = ThreadingHTTPServer((host, port), self._handler_type(service))
        # The bridge has no background jobs: request workers must not outlive
        # server shutdown.  This also prevents an in-flight rejected request
        # from making the test/process teardown race a client connection.
        self._http.daemon_threads = True
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    @property
    def port(self) -> int:
        return int(self._http.server_address[1])

    def start(self) -> None:
        if self._thread is None:
            def _run() -> None:
                self._ready.set()
                self._http.serve_forever()

            self._thread = threading.Thread(target=_run, name="mobiagent-hdc-bridge", daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout=5):
                raise RuntimeError("HDC bridge server did not become ready")

    def serve_forever(self) -> None:
        self._http.serve_forever()

    def close(self) -> None:
        self._http.shutdown()
        self._http.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._ready.clear()

    @staticmethod
    def _handler_type(service: HdcBridgeService) -> type[BaseHTTPRequestHandler]:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                # Never emit request payloads: they may contain user-entered text.
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/v1/health":
                    self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": {"code": "NOT_FOUND", "message": "not found"}})
                    return
                if not service.authorized(self.headers.get("X-MobiAgent-Bridge-Token")):
                    self._send(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "bridge authorization failed"}})
                    return
                self._send(HTTPStatus.OK, {"ok": True, "bridge_version": BRIDGE_VERSION, "serial": service.serial})

            def do_POST(self) -> None:  # noqa: N802
                if not service.authorized(self.headers.get("X-MobiAgent-Bridge-Token")):
                    self._send(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "bridge authorization failed"}})
                    return
                try:
                    request = self._body()
                    if self.path == "/v1/sessions":
                        result = service.create_session(request.get("serial"))
                    elif self.path == "/v1/sessions/close":
                        result = service.close_session(request.get("session_id"))
                    elif self.path == "/v1/io":
                        result = service.execute(request.get("session_id"), request.get("action"), request.get("payload"))
                    else:
                        raise BridgeError("NOT_FOUND", "not found", status=404)
                    self._send(HTTPStatus.OK, {"ok": True, "result": result})
                except BridgeError as error:
                    self._send(error.status, {"ok": False, "error": {"code": error.code, "message": str(error)}})
                except Exception:
                    self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": {"code": "INTERNAL", "message": "bridge execution failed"}})

            def _body(self) -> dict[str, object]:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 2_000_000:
                    raise BridgeError("INVALID_REQUEST", "request body is missing or too large")
                try:
                    value = json.loads(self.rfile.read(length))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise BridgeError("INVALID_REQUEST", "request is not valid JSON") from error
                if not isinstance(value, dict):
                    raise BridgeError("INVALID_REQUEST", "request must be a JSON object")
                return value

            def _send(self, status: int | HTTPStatus, body: dict[str, object]) -> None:
                encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        return Handler


def _hdc_rport(serial: str, phone_port: int, pc_port: int, *, remove: bool = False) -> None:
    """Map the phone-local bridge port to this PC process without shelling out."""
    command = ["hdc", "-t", serial, "rport"]
    if remove:
        command.append("rm")
    command.extend([f"tcp:{phone_port}", f"tcp:{pc_port}"])
    result = subprocess.run(command, text=True, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    if result.returncode != 0 and not remove:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "HDC rport failed")


def _hdc_provision_phone(
    serial: str,
    bundle: str,
    phone_port: int,
    token: str,
    *,
    readonly_smoke: bool = False,
    model_api_key: str | None = None,
    test_case_file: str | None = None,
) -> None:
    """Pass the current bridge session to Probe memory without persisting it."""
    command = [
        "hdc", "-t", serial, "shell", "aa", "start",
        "-a", "EntryAbility", "-b", bundle, "-m", "entry",
        "--ps", "mobiagent_bridge_url", f"http://127.0.0.1:{phone_port}",
        "--ps", "mobiagent_bridge_serial", serial,
        "--ps", "mobiagent_bridge_token", token,
    ]
    if readonly_smoke:
        command.extend(["--pb", "mobiagent_readonly_smoke", "true"])
    if model_api_key:
        command.extend(["--ps", "mobiagent_model_api_key", model_api_key])
    if test_case_file is not None:
        file_name = Path(test_case_file).name
        if file_name != test_case_file or not file_name:
            raise ValueError("test_case_file must be a sandbox filename")
        command.extend([
            "--pb", "mobiagent_phone_agent_run", "true",
            "--ps", "mobiagent_test_case_file", file_name,
        ])
    # Do not print arguments or subprocess output: the final argument is a
    # session credential.  The app retains it only in process memory.
    result = subprocess.run(command, text=True, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    if result.returncode != 0:
        raise RuntimeError("HDC bridge provisioning failed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="the one HDC serial bound by this server")
    parser.add_argument("--token-file", type=Path, required=True, help="local bridge-token file; its content is never logged")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9125)
    parser.add_argument("--phone-port", type=int, default=19125, help="phone-local TCP port used by HDC rport")
    parser.add_argument("--skip-rport", action="store_true", help="server only; manage the HDC reverse mapping externally")
    parser.add_argument("--provision-probe", action="store_true", help="provision the active bridge session into Probe memory via HDC")
    parser.add_argument("--readonly-smoke", action="store_true", help="with --provision-probe, run a no-model, no-action XHS observation smoke")
    parser.add_argument("--api-key-file", type=Path, help="temporary model credential file injected into Probe memory only with --provision-probe")
    parser.add_argument("--probe-bundle", default="com.zengxq.mobiagentprobe", help="Probe bundle used with --provision-probe")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.token_file.is_file():
        raise SystemExit("--token-file must name a readable regular file")
    if args.api_key_file is not None and not args.api_key_file.is_file():
        raise SystemExit("--api-key-file must name a readable regular file")
    if args.api_key_file is not None and not args.provision_probe:
        raise SystemExit("--api-key-file requires --provision-probe")
    token = args.token_file.read_text(encoding="utf-8").strip()
    model_api_key = args.api_key_file.read_text(encoding="utf-8").strip() if args.api_key_file is not None else None
    server = HdcBridgeServer(HdcBridgeService(serial=args.serial, token=token), args.host, args.port)
    mapped = False
    try:
        if not args.skip_rport:
            _hdc_rport(args.serial, args.phone_port, server.port, remove=True)
            _hdc_rport(args.serial, args.phone_port, server.port)
            mapped = True
        if args.provision_probe:
            _hdc_provision_phone(
                args.serial,
                args.probe_bundle,
                args.phone_port,
                token,
                readonly_smoke=args.readonly_smoke,
                model_api_key=model_api_key,
            )
        # Deliberately excludes the token.  This is safe to surface in a run log.
        print(json.dumps({"status": "READY", "serial": args.serial, "phone_url": f"http://127.0.0.1:{args.phone_port}", "pc_port": server.port}))
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        if mapped:
            _hdc_rport(args.serial, args.phone_port, server.port, remove=True)
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
