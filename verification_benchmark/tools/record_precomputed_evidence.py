#!/usr/bin/env python3
"""Record OCR/LLM evidence outside the deterministic evaluation process.

The recorder is intentionally not imported by ``evaluation_framework``. It is the only
new production path in this Gate that can perform HTTP, and it emits entries accepted by
``PrecomputedEvidenceStorage``. API keys are read from one authorized environment variable
and are never serialized, logged, accepted as CLI arguments, or included in exceptions.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Protocol, Tuple

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from verification_benchmark.evaluation_framework import (  # noqa: E402
    ContractRoiIR,
    EvidenceCacheKey,
    PrecomputedEvidenceStorage,
    RecordedEvidenceEntry,
    RecordedLlmDecision,
    RecordedLlmOutput,
    RecordedLlmRequestIR,
    RecordedOcrOutput,
    RecordedOcrRequestIR,
    RecordedProviderKind,
    RoiCoordinateSpace,
)


RECORDING_MANIFEST_SCHEMA_VERSION = "harmony-eval-recording-manifest-v1"
RECORDER_RECEIPT_SCHEMA_VERSION = "harmony-eval-recorder-receipt-v1"
RECORDER_VERSION = "harmony-eval-external-recorder-v1.1"
AUTHORIZED_BASE_URL = "https://api.horizon1123.top/v1"
AUTHORIZED_API_KEY_ENV = "MOBIAGENT_API_KEY"
AUTHORIZED_MODEL = "gpt-5.4-mini"
LLM_PROMPT_TEMPLATE_VERSION = "legacy-llm-prompt-v1"
OCR_PROMPT_TEMPLATE_VERSION = "recorded-ocr-json-v1"
# Horizon's private OpenAI-compatible gateway has a previously verified vision path that
# omits ``image_url.detail``.  The mode is still explicit in the receipt so a transport
# capability change cannot be mistaken for an identical recording protocol.
IMAGE_DETAIL = "provider_default_omitted"
TRANSPORT_USER_AGENT = "harmony-eval-external-recorder/1.1"
MAX_MANIFEST_JOBS = 1000
MAX_SCREENSHOT_BYTES = 25 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TIMEOUT_SECONDS = 300.0
MAX_ATTEMPTS = 3
_SHA256_LENGTH = 64


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_json(value: Any) -> str:
    return _digest_bytes(_canonical_bytes(value))


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_id(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{context} must be a canonical non-empty string")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{context} fields must be exactly {sorted(expected)}; got {sorted(actual)}"
        )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_bytes(value: bytes, context: str) -> Any:
    try:
        return json.loads(value.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{context} is not strict UTF-8 JSON") from exc


def _relative_ref(value: Any) -> str:
    reference = _canonical_id(value, "screenshot_ref")
    if "\\" in reference:
        raise ValueError("screenshot_ref must use POSIX separators")
    parsed = PurePosixPath(reference)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError("screenshot_ref must not escape screenshot_root")
    if parsed.suffix.casefold() not in {".jpg", ".jpeg"}:
        raise ValueError("recorder v1 accepts only JPEG screenshots")
    return reference


@dataclass(frozen=True)
class RecordingJob:
    job_id: str
    provider_kind: RecordedProviderKind
    screenshot_ref: str
    screenshot_sha256: str
    request: RecordedOcrRequestIR | RecordedLlmRequestIR

    def validate(self) -> None:
        _canonical_id(self.job_id, "recording job_id")
        if not isinstance(self.provider_kind, RecordedProviderKind):
            raise ValueError("recording provider_kind is invalid")
        _relative_ref(self.screenshot_ref)
        if not _is_sha256(self.screenshot_sha256):
            raise ValueError("recording screenshot_sha256 must be lowercase SHA-256")
        expected = (
            RecordedOcrRequestIR
            if self.provider_kind is RecordedProviderKind.OCR
            else RecordedLlmRequestIR
        )
        if not isinstance(self.request, expected):
            raise ValueError("recording request does not match provider_kind")
        self.request.validate()
        if isinstance(self.request, RecordedLlmRequestIR) and (
            self.request.prompt_template_version != LLM_PROMPT_TEMPLATE_VERSION
        ):
            raise ValueError("unsupported LLM prompt_template_version")

    @property
    def request_sha256(self) -> str:
        self.validate()
        return self.request.request_sha256

    def payload(self) -> dict[str, Any]:
        self.validate()
        if isinstance(self.request, RecordedOcrRequestIR):
            request = {
                "kind": "OCR_ROI",
                "coordinate_space": self.request.roi.coordinate_space.value,
                "bounds": list(self.request.roi.bounds),
                "reference_size": None
                if self.request.roi.reference_size is None
                else list(self.request.roi.reference_size),
            }
        else:
            request = {
                "kind": "LLM_PROMPT",
                "prompt_template_version": self.request.prompt_template_version,
                "prompt": self.request.prompt,
            }
        return {
            "job_id": self.job_id,
            "provider_kind": self.provider_kind.value,
            "screenshot_ref": self.screenshot_ref,
            "screenshot_sha256": self.screenshot_sha256,
            "request": request,
        }


@dataclass(frozen=True)
class RecordingManifest:
    model_version: str
    jobs: Tuple[RecordingJob, ...]
    schema_version: str = RECORDING_MANIFEST_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != RECORDING_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported recording manifest schema")
        if self.model_version != AUTHORIZED_MODEL:
            raise ValueError(f"recorder v1 model must be exactly {AUTHORIZED_MODEL}")
        if not isinstance(self.jobs, tuple) or not self.jobs:
            raise ValueError("recording manifest jobs must be a non-empty immutable tuple")
        if len(self.jobs) > MAX_MANIFEST_JOBS:
            raise ValueError(f"recording manifest exceeds {MAX_MANIFEST_JOBS} jobs")
        job_ids = []
        composite_keys = []
        for job in self.jobs:
            if not isinstance(job, RecordingJob):
                raise ValueError("recording manifest contains an invalid job")
            job.validate()
            job_ids.append(job.job_id)
            composite_keys.append(
                (
                    job.provider_kind,
                    job.screenshot_sha256,
                    self.model_version,
                    job.request_sha256,
                )
            )
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("recording job_id values must be unique")
        if len(composite_keys) != len(set(composite_keys)):
            raise ValueError("recording jobs must have unique composite cache keys")

    @property
    def manifest_sha256(self) -> str:
        self.validate()
        return _digest_json(
            {
                "schema_version": self.schema_version,
                "model_version": self.model_version,
                "jobs": [job.payload() for job in self.jobs],
            }
        )


def _parse_request(value: Any, provider_kind: RecordedProviderKind):
    if not isinstance(value, Mapping):
        raise ValueError("recording request must be a JSON object")
    kind = value.get("kind")
    if provider_kind is RecordedProviderKind.OCR:
        _exact_keys(
            value,
            {"kind", "coordinate_space", "bounds", "reference_size"},
            "OCR recording request",
        )
        if kind != "OCR_ROI":
            raise ValueError("OCR request kind must be OCR_ROI")
        try:
            coordinate_space = RoiCoordinateSpace(value["coordinate_space"])
        except (TypeError, ValueError) as exc:
            raise ValueError("OCR coordinate_space is invalid") from exc
        bounds = value["bounds"]
        if not isinstance(bounds, list) or len(bounds) != 4:
            raise ValueError("OCR bounds must be a four-item JSON array")
        reference_size = value["reference_size"]
        if reference_size is not None and (
            not isinstance(reference_size, list) or len(reference_size) != 2
        ):
            raise ValueError("OCR reference_size must be null or a two-item JSON array")
        roi = ContractRoiIR(
            roi_id="recording-roi",
            bounds=tuple(bounds),
            coordinate_space=coordinate_space,
            reference_size=None if reference_size is None else tuple(reference_size),
        )
        request = RecordedOcrRequestIR(roi)
    else:
        _exact_keys(
            value,
            {"kind", "prompt_template_version", "prompt"},
            "LLM recording request",
        )
        if kind != "LLM_PROMPT":
            raise ValueError("LLM request kind must be LLM_PROMPT")
        request = RecordedLlmRequestIR(
            prompt=value["prompt"],
            prompt_template_version=value["prompt_template_version"],
        )
    request.validate()
    return request


def load_recording_manifest(path: Path | str) -> RecordingManifest:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ValueError("recording manifest is unreadable") from exc
    value = _strict_json_bytes(raw, "recording manifest")
    if not isinstance(value, Mapping):
        raise ValueError("recording manifest must be a JSON object")
    _exact_keys(value, {"schema_version", "model_version", "jobs"}, "manifest")
    if value["schema_version"] != RECORDING_MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported recording manifest schema")
    raw_jobs = value["jobs"]
    if not isinstance(raw_jobs, list):
        raise ValueError("recording jobs must be a JSON array")
    jobs = []
    for index, raw_job in enumerate(raw_jobs):
        if not isinstance(raw_job, Mapping):
            raise ValueError(f"recording job {index} must be a JSON object")
        _exact_keys(
            raw_job,
            {"job_id", "provider_kind", "screenshot_ref", "screenshot_sha256", "request"},
            f"recording job {index}",
        )
        try:
            provider_kind = RecordedProviderKind(raw_job["provider_kind"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"recording job {index} provider_kind is invalid") from exc
        jobs.append(
            RecordingJob(
                job_id=raw_job["job_id"],
                provider_kind=provider_kind,
                screenshot_ref=_relative_ref(raw_job["screenshot_ref"]),
                screenshot_sha256=raw_job["screenshot_sha256"],
                request=_parse_request(raw_job["request"], provider_kind),
            )
        )
    manifest = RecordingManifest(value["model_version"], tuple(jobs))
    manifest.validate()
    return manifest


@dataclass(frozen=True)
class PreparedRecordingJob:
    job: RecordingJob
    key: EvidenceCacheKey
    request_image: bytes
    request_image_sha256: str


def _safe_screenshot(root: Path, reference: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / PurePosixPath(reference)).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("screenshot_ref escapes screenshot_root") from exc
    return candidate


def _pixel_bounds(roi: ContractRoiIR, size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = size
    x1, y1, x2, y2 = (float(value) for value in roi.bounds)
    if roi.coordinate_space is RoiCoordinateSpace.REFERENCE_PIXELS:
        if roi.reference_size is None:
            raise ValueError("reference-pixel ROI lacks reference_size")
        reference_width, reference_height = roi.reference_size
        x1, x2 = x1 / reference_width, x2 / reference_width
        y1, y2 = y1 / reference_height, y2 / reference_height
    pixel_x1 = min(width - 1, max(0, math.floor(x1 * width)))
    pixel_y1 = min(height - 1, max(0, math.floor(y1 * height)))
    pixel_x2 = min(width, max(pixel_x1 + 1, math.ceil(x2 * width)))
    pixel_y2 = min(height, max(pixel_y1 + 1, math.ceil(y2 * height)))
    return pixel_x1, pixel_y1, pixel_x2, pixel_y2


def _prepare_image(image_bytes: bytes, request: Any) -> bytes:
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            if image.format != "JPEG":
                raise ValueError("recorder v1 screenshot must decode as JPEG")
            if not isinstance(request, RecordedOcrRequestIR):
                return image_bytes
            bounds = _pixel_bounds(request.roi, image.size)
            if bounds == (0, 0, image.width, image.height):
                return image_bytes
            cropped = image.convert("RGB").crop(bounds)
            output = io.BytesIO()
            cropped.save(
                output,
                format="JPEG",
                quality=95,
                optimize=False,
                progressive=False,
                subsampling=0,
            )
            return output.getvalue()
    except (OSError, ValueError) as exc:
        raise ValueError("referenced screenshot is not a valid JPEG") from exc


def prepare_recording_jobs(
    manifest: RecordingManifest, screenshot_root: Path | str
) -> Tuple[PreparedRecordingJob, ...]:
    manifest.validate()
    root = Path(screenshot_root)
    if not root.is_dir():
        raise ValueError("screenshot_root does not exist")
    prepared = []
    for job in manifest.jobs:
        path = _safe_screenshot(root, job.screenshot_ref)
        try:
            image_bytes = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"recording screenshot is unreadable: {job.job_id}") from exc
        if len(image_bytes) > MAX_SCREENSHOT_BYTES:
            raise ValueError(f"recording screenshot exceeds size limit: {job.job_id}")
        if _digest_bytes(image_bytes) != job.screenshot_sha256:
            raise ValueError(f"recording screenshot SHA-256 mismatch: {job.job_id}")
        request_image = _prepare_image(image_bytes, job.request)
        prepared.append(
            PreparedRecordingJob(
                job=job,
                key=EvidenceCacheKey(
                    screenshot_sha256=job.screenshot_sha256,
                    model_version=manifest.model_version,
                    request_sha256=job.request_sha256,
                ),
                request_image=request_image,
                request_image_sha256=_digest_bytes(request_image),
            )
        )
    return tuple(prepared)


def validate_authorized_service(base_url: str, api_key_env: str, model: str) -> None:
    if not isinstance(base_url, str) or base_url.rstrip("/") != AUTHORIZED_BASE_URL:
        raise ValueError("unauthorized model endpoint")
    if api_key_env != AUTHORIZED_API_KEY_ENV:
        raise ValueError("unauthorized API key environment variable")
    if model != AUTHORIZED_MODEL:
        raise ValueError("unauthorized recorder model")


@dataclass(frozen=True)
class TransportResponse:
    body: bytes
    latency_ms: float


class RecorderTransport(Protocol):
    def complete(self, payload: Mapping[str, Any], *, timeout: float) -> TransportResponse:
        ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, code, "redirect forbidden", headers, fp)


class OpenAiCompatibleChatTransport:
    """Minimal authorized transport with redirects and environment proxies disabled."""

    __slots__ = ("_api_key", "_endpoint", "_opener")

    def __init__(self, *, base_url: str, api_key: str) -> None:
        validate_authorized_service(base_url, AUTHORIZED_API_KEY_ENV, AUTHORIZED_MODEL)
        if not isinstance(api_key, str) or not api_key.strip() or "\r" in api_key or "\n" in api_key:
            raise ValueError("authorized API key is missing or invalid")
        self._api_key = api_key
        self._endpoint = AUTHORIZED_BASE_URL + "/chat/completions"
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirectHandler()
        )

    def __repr__(self) -> str:
        return "OpenAiCompatibleChatTransport(endpoint='authorized', api_key=<redacted>)"

    def complete(self, payload: Mapping[str, Any], *, timeout: float) -> TransportResponse:
        request = urllib.request.Request(
            self._endpoint,
            data=_canonical_bytes(payload),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": TRANSPORT_USER_AGENT,
            },
            method="POST",
        )
        started = time.perf_counter()
        with self._opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("provider response exceeds recorder size limit")
        return TransportResponse(body, round((time.perf_counter() - started) * 1000, 3))


def _prompt_for(job: RecordingJob) -> str:
    if isinstance(job.request, RecordedOcrRequestIR):
        return (
            "Transcribe all legible text in this mobile screenshot region. Preserve reading "
            "order and visible punctuation. Do not infer hidden text. Return exactly one JSON "
            'object with this schema: {"text":"..."}. If no text is legible, return '
            '{"text":""}.'
        )
    return (
        "Evaluate the visible mobile screenshot against the instruction below. Return true only "
        "when visible evidence establishes it, false only when visible evidence establishes the "
        "opposite, and null when evidence is insufficient. Return exactly one JSON object with "
        'this schema: {"decision":true|false|null}.\n\nInstruction:\n'
        + job.request.prompt
    )


def build_chat_payload(prepared: PreparedRecordingJob, model: str) -> dict[str, Any]:
    encoded = base64.b64encode(prepared.request_image).decode("ascii")
    max_tokens = 2000 if prepared.job.provider_kind is RecordedProviderKind.OCR else 100
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _prompt_for(prepared.job)},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{encoded}",
                        },
                    },
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }


@dataclass(frozen=True)
class ParsedProviderResponse:
    output: RecordedOcrOutput | RecordedLlmOutput
    usage: Mapping[str, int]
    response_id_sha256: Optional[str]


def parse_provider_response(
    response: TransportResponse,
    *,
    provider_kind: RecordedProviderKind,
    expected_model: str,
) -> ParsedProviderResponse:
    if not isinstance(response, TransportResponse) or not isinstance(response.body, bytes):
        raise ValueError("transport returned an invalid response")
    body = _strict_json_bytes(response.body, "provider response")
    if not isinstance(body, Mapping):
        raise ValueError("provider response must be a JSON object")
    if body.get("model") != expected_model:
        raise ValueError("provider response model drift")
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("provider response must contain exactly one choice")
    choice = choices[0]
    if not isinstance(choice, Mapping) or not isinstance(choice.get("message"), Mapping):
        raise ValueError("provider response choice is malformed")
    content = choice["message"].get("content")
    if not isinstance(content, str):
        raise ValueError("provider response content must be textual JSON")
    recorded = _strict_json_bytes(content.encode("utf-8"), "recorded output")
    if not isinstance(recorded, Mapping):
        raise ValueError("recorded output must be a JSON object")
    response_sha256 = _digest_bytes(response.body)
    if provider_kind is RecordedProviderKind.OCR:
        _exact_keys(recorded, {"text"}, "recorded OCR output")
        output: RecordedOcrOutput | RecordedLlmOutput = RecordedOcrOutput(
            recorded["text"], response_sha256
        )
    else:
        _exact_keys(recorded, {"decision"}, "recorded LLM output")
        decision_value = recorded["decision"]
        if decision_value is True:
            decision = RecordedLlmDecision.TRUE
        elif decision_value is False:
            decision = RecordedLlmDecision.FALSE
        elif decision_value is None:
            decision = RecordedLlmDecision.UNKNOWN
        else:
            raise ValueError("recorded LLM decision must be boolean or null")
        output = RecordedLlmOutput(decision, response_sha256)
    output.validate()
    raw_usage = body.get("usage")
    usage = {
        str(key): value
        for key, value in raw_usage.items()
        if isinstance(raw_usage, Mapping)
        and isinstance(key, str)
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    } if isinstance(raw_usage, Mapping) else {}
    response_id = body.get("id")
    response_id_sha256 = (
        _digest_bytes(response_id.encode("utf-8"))
        if isinstance(response_id, str) and response_id
        else None
    )
    return ParsedProviderResponse(output, usage, response_id_sha256)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_cache(path: Path, entries: Tuple[RecordedEvidenceEntry, ...]) -> str:
    storage = PrecomputedEvidenceStorage(entries)
    text = "".join(
        json.dumps(entry.payload(), ensure_ascii=False, sort_keys=True) + "\n"
        for entry in entries
    )
    _atomic_write(path, text)
    return storage.storage_sha256


def _load_existing_entries(path: Path) -> Tuple[RecordedEvidenceEntry, ...]:
    storage = PrecomputedEvidenceStorage.from_jsonl(path)
    # Reparse through the public payload lookup domain without exposing storage internals.
    lines = path.read_text(encoding="utf-8").splitlines()
    entries = []
    for line in lines:
        value = json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
        key = EvidenceCacheKey(**value["key"])
        kind = RecordedProviderKind(value["provider_kind"])
        found = storage.lookup(kind, key)
        if found is None:
            raise ValueError("existing cache entry disappeared during strict reload")
        entries.append(found)
    return tuple(entries)


def _receipt_base(
    manifest: RecordingManifest,
    *,
    base_url: str,
    api_key_env: str,
) -> dict[str, Any]:
    return {
        "schema_version": RECORDER_RECEIPT_SCHEMA_VERSION,
        "recorder_version": RECORDER_VERSION,
        "manifest_sha256": manifest.manifest_sha256,
        "model_version": manifest.model_version,
        "base_url": base_url.rstrip("/"),
        "api_key_env": api_key_env,
        "api_key_recorded": False,
        "ocr_prompt_template_version": OCR_PROMPT_TEMPLATE_VERSION,
        "image_detail": IMAGE_DETAIL,
        "status": "PARTIAL",
        "cache_storage_sha256": None,
        "summary": {
            "recorded": 0,
            "cached": 0,
            "errors": 0,
            "requests": 0,
            "latency_ms_total": 0.0,
            "usage": {},
        },
        "sessions": [],
    }


def _load_receipt(path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    value = _strict_json_bytes(path.read_bytes(), "recorder receipt")
    if not isinstance(value, dict):
        raise ValueError("recorder receipt must be a JSON object")
    _exact_keys(
        value,
        {
            "schema_version",
            "recorder_version",
            "manifest_sha256",
            "model_version",
            "base_url",
            "api_key_env",
            "api_key_recorded",
            "ocr_prompt_template_version",
            "image_detail",
            "status",
            "cache_storage_sha256",
            "summary",
            "sessions",
        },
        "recorder receipt",
    )
    for field in (
        "schema_version",
        "recorder_version",
        "manifest_sha256",
        "model_version",
        "base_url",
        "api_key_env",
        "api_key_recorded",
        "ocr_prompt_template_version",
        "image_detail",
    ):
        if value.get(field) != expected[field]:
            raise ValueError(f"existing receipt {field} mismatch")
    if not isinstance(value.get("sessions"), list):
        raise ValueError("existing receipt sessions must be a list")
    if value.get("status") not in {"PARTIAL", "COMPLETE"}:
        raise ValueError("existing receipt status is invalid")
    cache_digest = value.get("cache_storage_sha256")
    if cache_digest is not None and not _is_sha256(cache_digest):
        raise ValueError("existing receipt cache_storage_sha256 is invalid")
    for session_index, session in enumerate(value["sessions"]):
        if not isinstance(session, Mapping):
            raise ValueError("existing receipt session must be an object")
        _exact_keys(
            session,
            {"session_index", "request_budget", "worst_case_requests", "items"},
            "recorder receipt session",
        )
        if session["session_index"] != session_index:
            raise ValueError("existing receipt session_index values must be contiguous")
        for field in ("request_budget", "worst_case_requests"):
            if (
                not isinstance(session[field], int)
                or isinstance(session[field], bool)
                or session[field] < 0
            ):
                raise ValueError(f"existing receipt {field} is invalid")
        if session["worst_case_requests"] > session["request_budget"]:
            raise ValueError("existing receipt session exceeded its request budget")
        if not isinstance(session["items"], list):
            raise ValueError("existing receipt session items must be a list")
        for item in session["items"]:
            _validate_receipt_item(item)
    if value.get("summary") != _receipt_summary(value["sessions"]):
        raise ValueError("existing receipt summary does not match its sessions")
    return value


def _validate_receipt_item(item: Any) -> None:
    if not isinstance(item, Mapping):
        raise ValueError("existing receipt item must be an object")
    _exact_keys(
        item,
        {
            "job_id",
            "provider_kind",
            "key",
            "request_image_sha256",
            "status",
            "attempts",
            "error_code",
            "latency_ms",
            "usage",
            "response_sha256",
            "response_id_sha256",
        },
        "recorder receipt item",
    )
    _canonical_id(item["job_id"], "receipt job_id")
    try:
        RecordedProviderKind(item["provider_kind"])
    except (TypeError, ValueError) as exc:
        raise ValueError("existing receipt provider_kind is invalid") from exc
    if not isinstance(item["key"], Mapping):
        raise ValueError("existing receipt cache key is invalid")
    _exact_keys(
        item["key"],
        {"screenshot_sha256", "model_version", "request_sha256"},
        "recorder receipt cache key",
    )
    EvidenceCacheKey(**item["key"]).validate()
    if not _is_sha256(item["request_image_sha256"]):
        raise ValueError("existing receipt request_image_sha256 is invalid")
    if item["status"] not in {"RECORDED", "CACHED", "ERROR"}:
        raise ValueError("existing receipt item status is invalid")
    attempts = item["attempts"]
    if (
        not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 0
        or attempts > MAX_ATTEMPTS
        or (item["status"] == "CACHED" and attempts != 0)
        or (item["status"] != "CACHED" and attempts == 0)
    ):
        raise ValueError("existing receipt attempts are invalid")
    allowed_errors = {
        "HTTP_ERROR",
        "TRANSPORT_ERROR",
        "MODEL_DRIFT",
        "INVALID_RECORDED_OUTPUT",
        "INVALID_PROVIDER_RESPONSE",
    }
    if (item["status"] == "ERROR") != (item["error_code"] in allowed_errors):
        raise ValueError("existing receipt error_code is inconsistent")
    latency = item["latency_ms"]
    if (
        not isinstance(latency, (int, float))
        or isinstance(latency, bool)
        or not math.isfinite(latency)
        or latency < 0
    ):
        raise ValueError("existing receipt latency_ms is invalid")
    if not isinstance(item["usage"], Mapping) or any(
        not isinstance(key, str)
        or not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        for key, value in item["usage"].items()
    ):
        raise ValueError("existing receipt usage is invalid")
    for field in ("response_sha256", "response_id_sha256"):
        if item[field] is not None and not _is_sha256(item[field]):
            raise ValueError(f"existing receipt {field} is invalid")
    if item["status"] == "RECORDED" and item["response_sha256"] is None:
        raise ValueError("recorded receipt item lacks response_sha256")
    if item["status"] == "ERROR" and (
        item["response_sha256"] is not None or item["response_id_sha256"] is not None
    ):
        raise ValueError("error receipt item must not retain response identities")


def _receipt_summary(sessions: list[Any]) -> dict[str, Any]:
    recorded = cached = errors = requests = 0
    latency = 0.0
    usage: dict[str, int] = {}
    for session in sessions:
        for item in session["items"]:
            status = item["status"]
            recorded += status == "RECORDED"
            cached += status == "CACHED"
            errors += status == "ERROR"
            requests += item["attempts"]
            latency += item["latency_ms"]
            for key, value in item["usage"].items():
                usage[key] = usage.get(key, 0) + value
    return {
        "recorded": recorded,
        "cached": cached,
        "errors": errors,
        "requests": requests,
        "latency_ms_total": round(latency, 3),
        "usage": dict(sorted(usage.items())),
    }


def _update_receipt_summary(receipt: dict[str, Any]) -> None:
    receipt["summary"] = _receipt_summary(receipt["sessions"])


def _error_code(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP_ERROR"
    if isinstance(exc, urllib.error.URLError):
        return "TRANSPORT_ERROR"
    text = str(exc).casefold()
    if "model drift" in text:
        return "MODEL_DRIFT"
    if "recorded" in text:
        return "INVALID_RECORDED_OUTPUT"
    return "INVALID_PROVIDER_RESPONSE"


@dataclass(frozen=True)
class RecordingRunResult:
    status: str
    recorded: int
    cached: int
    errors: int
    requests: int
    cache_storage_sha256: Optional[str]


def run_recording(
    manifest: RecordingManifest,
    *,
    screenshot_root: Path | str,
    cache_path: Path | str,
    receipt_path: Path | str,
    base_url: str,
    model: str,
    api_key_env: str = AUTHORIZED_API_KEY_ENV,
    timeout: float = 90.0,
    max_attempts: int = 1,
    request_budget: Optional[int] = None,
    resume: bool = False,
    dry_run: bool = False,
    transport: Optional[RecorderTransport] = None,
) -> RecordingRunResult:
    manifest.validate()
    validate_authorized_service(base_url, api_key_env, model)
    if model != manifest.model_version:
        raise ValueError("CLI model differs from recording manifest")
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout <= 0
        or timeout > MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(f"timeout must be within (0, {MAX_TIMEOUT_SECONDS}]")
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts <= 0
        or max_attempts > MAX_ATTEMPTS
    ):
        raise ValueError(f"max_attempts must be within [1, {MAX_ATTEMPTS}]")
    prepared = prepare_recording_jobs(manifest, screenshot_root)
    if dry_run:
        return RecordingRunResult("DRY_RUN", 0, 0, 0, 0, None)

    cache = Path(cache_path)
    receipt_file = Path(receipt_path)
    if cache.resolve() == receipt_file.resolve():
        raise ValueError("cache and receipt paths must differ")
    if not resume and (cache.exists() or receipt_file.exists()):
        raise ValueError("output already exists; use --resume or choose new paths")
    entries = list(_load_existing_entries(cache)) if resume and cache.is_file() else []
    expected = {(job.job.provider_kind, job.key): job for job in prepared}
    existing = {(entry.provider_kind, entry.key): entry for entry in entries}
    if any(key not in expected for key in existing):
        raise ValueError("existing cache contains entries outside this manifest")
    uncached_count = len(expected) - len(existing)
    worst_case_requests = uncached_count * max_attempts
    if (
        not isinstance(request_budget, int)
        or isinstance(request_budget, bool)
        or request_budget < 0
    ):
        raise ValueError("live recording requires a non-negative explicit request_budget")
    if worst_case_requests > request_budget:
        raise ValueError(
            "worst-case recording requests exceed explicit request_budget: "
            f"{worst_case_requests} > {request_budget}"
        )

    receipt = _receipt_base(manifest, base_url=base_url, api_key_env=api_key_env)
    if resume and receipt_file.is_file():
        receipt = _load_receipt(receipt_file, receipt)
    session: dict[str, Any] = {
        "session_index": len(receipt["sessions"]),
        "request_budget": request_budget,
        "worst_case_requests": worst_case_requests,
        "items": [],
    }
    receipt["sessions"].append(session)

    if transport is None:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ValueError(f"required API key environment variable is unset: {api_key_env}")
        transport = OpenAiCompatibleChatTransport(base_url=base_url, api_key=api_key)

    recorded = cached = errors = requests = 0
    storage_sha256 = PrecomputedEvidenceStorage(tuple(entries)).storage_sha256 if entries else None
    for item in prepared:
        identity = (item.job.provider_kind, item.key)
        common = {
            "job_id": item.job.job_id,
            "provider_kind": item.job.provider_kind.value,
            "key": item.key.payload(),
            "request_image_sha256": item.request_image_sha256,
        }
        if identity in existing:
            cached += 1
            output = existing[identity].output
            session["items"].append(
                {
                    **common,
                    "status": "CACHED",
                    "attempts": 0,
                    "error_code": None,
                    "latency_ms": 0.0,
                    "usage": {},
                    "response_sha256": output.response_sha256,
                    "response_id_sha256": None,
                }
            )
        else:
            parsed: Optional[ParsedProviderResponse] = None
            last_error: Optional[Exception] = None
            latency_total = 0.0
            attempts = 0
            for attempts in range(1, max_attempts + 1):
                requests += 1
                attempt_started = time.perf_counter()
                try:
                    response = transport.complete(
                        build_chat_payload(item, model), timeout=float(timeout)
                    )
                    latency_total += response.latency_ms
                    parsed = parse_provider_response(
                        response,
                        provider_kind=item.job.provider_kind,
                        expected_model=model,
                    )
                    last_error = None
                    break
                except Exception as exc:  # fail closed; response/error text is never retained
                    latency_total += (time.perf_counter() - attempt_started) * 1000
                    last_error = exc
            if parsed is None:
                errors += 1
                session["items"].append(
                    {
                        **common,
                        "status": "ERROR",
                        "attempts": attempts,
                        "error_code": _error_code(last_error or ValueError()),
                        "latency_ms": round(latency_total, 3),
                        "usage": {},
                        "response_sha256": None,
                        "response_id_sha256": None,
                    }
                )
            else:
                entry = RecordedEvidenceEntry(item.job.provider_kind, item.key, parsed.output)
                entry.validate()
                entries.append(entry)
                existing[identity] = entry
                recorded += 1
                storage_sha256 = _write_cache(cache, tuple(entries))
                session["items"].append(
                    {
                        **common,
                        "status": "RECORDED",
                        "attempts": attempts,
                        "error_code": None,
                        "latency_ms": round(latency_total, 3),
                        "usage": dict(parsed.usage),
                        "response_sha256": parsed.output.response_sha256,
                        "response_id_sha256": parsed.response_id_sha256,
                    }
                )
        receipt["status"] = "COMPLETE" if len(existing) == len(prepared) else "PARTIAL"
        receipt["cache_storage_sha256"] = storage_sha256
        _update_receipt_summary(receipt)
        _atomic_write(
            receipt_file,
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )

    status = "COMPLETE" if errors == 0 and len(existing) == len(prepared) else "PARTIAL"
    receipt["status"] = status
    receipt["cache_storage_sha256"] = storage_sha256
    _update_receipt_summary(receipt)
    _atomic_write(
        receipt_file,
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return RecordingRunResult(status, recorded, cached, errors, requests, storage_sha256)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--screenshot-root", required=True)
    parser.add_argument("--output-cache", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key-env", default=AUTHORIZED_API_KEY_ENV)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--request-budget", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_recording(
            load_recording_manifest(args.manifest),
            screenshot_root=args.screenshot_root,
            cache_path=args.output_cache,
            receipt_path=args.receipt,
            base_url=args.base_url,
            model=args.model,
            api_key_env=args.api_key_env,
            timeout=args.timeout,
            max_attempts=args.max_attempts,
            request_budget=args.request_budget,
            resume=args.resume,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"recorder refused to run: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": result.status,
                "recorded": result.recorded,
                "cached": result.cached,
                "errors": result.errors,
                "requests": result.requests,
            },
            sort_keys=True,
        )
    )
    return 0 if result.status in {"COMPLETE", "DRY_RUN"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
