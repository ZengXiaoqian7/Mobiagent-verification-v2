"""Small OpenAI-compatible model client helpers for App-test execution."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import requests


PLACEHOLDER_MARKERS = (
    "YOUR_",
    "YOUR-",
    "your_",
    "your-",
    "your_model_endpoint",
    "your-model-endpoint",
    "YOUR_MODEL_ENDPOINT",
    "YOUR_MODEL_NAME",
    "YOUR_KEY",
)


class ModelConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    api_key: str
    wire_api: str = "chat_completions"
    reasoning_effort: str | None = None
    store: bool = False


def model_config_from_env(
    *,
    base_url_names: Iterable[str],
    model_names: Iterable[str],
    api_key_names: Iterable[str] = ("MOBIAGENT_API_KEY",),
) -> ModelConfig:
    base_url_names = tuple(base_url_names)
    model_names = tuple(model_names)
    api_key_names = tuple(api_key_names)
    base_url_name, base_url = _first_env(base_url_names)
    model_name, model = _first_env(model_names)
    api_key_name, api_key = _first_env(api_key_names)
    if not api_key and "MOBIAGENT_API_KEY" in api_key_names:
        api_key_name, api_key = _api_key_from_file()
    missing = [
        label
        for label, value in (
            ("/".join(base_url_names), base_url),
            ("/".join(model_names), model),
            ("/".join(api_key_names), api_key),
        )
        if not value
    ]
    if missing:
        raise ModelConfigError("missing model configuration: " + ", ".join(missing))
    for name, value in (
        (base_url_name or "base_url", base_url),
        (model_name or "model", model),
        (api_key_name or "api_key", api_key),
    ):
        if _looks_like_placeholder(value):
            raise ModelConfigError(
                f"{name} still contains a placeholder value; replace it with a real model service setting"
            )
    return ModelConfig(
        base_url=str(base_url),
        model=str(model),
        api_key=str(api_key),
        wire_api=_wire_api_from_env(),
        reasoning_effort=os.getenv("MOBIAGENT_REASONING_EFFORT") or None,
        store=not _env_flag("MOBIAGENT_DISABLE_RESPONSE_STORAGE", default=True),
    )


def chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def post_chat_completion(
    config: ModelConfig,
    *,
    messages: list[Mapping[str, Any]],
    temperature: float = 0,
    max_tokens: int = 256,
    timeout: int = 90,
) -> str:
    if config.wire_api == "responses":
        return _post_responses(
            config,
            messages=messages,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    response = requests.post(
        chat_completions_url(config.base_url),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        data=json.dumps(
            {
                "model": config.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    payload = response.json()
    return str(payload["choices"][0]["message"]["content"])


def responses_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/responses"):
        return base
    return base + "/responses"


def _post_responses(
    config: ModelConfig,
    *,
    messages: list[Mapping[str, Any]],
    max_tokens: int,
    timeout: int,
) -> str:
    payload: dict[str, Any] = {
        "model": config.model,
        "input": [
            {
                "role": str(message.get("role") or "user"),
                "content": _responses_content(message.get("content")),
            }
            for message in messages
        ],
        "max_output_tokens": max_tokens,
        "store": config.store,
    }
    if config.reasoning_effort:
        payload["reasoning"] = {"effort": config.reasoning_effort}
    response = requests.post(
        responses_url(config.base_url),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    return _responses_output_text(response.json())


def _responses_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    converted = []
    for item in content or ():
        if not isinstance(item, Mapping):
            raise ValueError("Responses message content items must be objects")
        item_type = str(item.get("type") or "").strip().lower()
        if item_type in {"text", "input_text"}:
            converted.append({"type": "input_text", "text": str(item.get("text") or "")})
            continue
        if item_type in {"image_url", "input_image"}:
            image_url = item.get("image_url")
            detail = item.get("detail")
            if isinstance(image_url, Mapping):
                detail = image_url.get("detail", detail)
                image_url = image_url.get("url")
            if not isinstance(image_url, str) or not image_url:
                raise ValueError("Responses image content requires a non-empty image_url")
            converted_image = {"type": "input_image", "image_url": image_url}
            if detail:
                converted_image["detail"] = detail
            converted.append(converted_image)
            continue
        raise ValueError(f"unsupported Responses message content type {item_type!r}")
    return converted


def _responses_output_text(payload: Mapping[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    texts = []
    for output in payload.get("output", ()):
        if not isinstance(output, Mapping):
            continue
        for content in output.get("content", ()):
            if not isinstance(content, Mapping):
                continue
            if content.get("type") in {"output_text", "text"}:
                text = content.get("text")
                if isinstance(text, str) and text:
                    texts.append(text)
    if texts:
        return "\n".join(texts)
    raise ValueError("Responses API result did not contain output text")


def _wire_api_from_env() -> str:
    value = os.getenv("MOBIAGENT_WIRE_API", "chat_completions")
    normalized = value.strip().lower().replace("-", "_")
    if normalized in {"chat", "chat_completion", "chat_completions"}:
        return "chat_completions"
    if normalized in {"response", "responses"}:
        return "responses"
    raise ModelConfigError(
        f"unsupported MOBIAGENT_WIRE_API {value!r}; choose chat_completions or responses"
    )


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ModelConfigError(f"{name} must be true or false")


def extract_json_object(text: str) -> Mapping[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, Mapping):
        raise ValueError("model response root is not an object")
    return value


def _first_env(names: Iterable[str]) -> tuple[str | None, str | None]:
    for name in names:
        value = os.getenv(name)
        if value:
            return name, value
    return None, None


def _api_key_from_file() -> tuple[str | None, str | None]:
    key_file = os.getenv("MOBIAGENT_API_KEY_FILE", "").strip()
    if not key_file:
        return None, None
    try:
        text = Path(key_file).expanduser().read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ModelConfigError("MOBIAGENT_API_KEY_FILE is unreadable") from exc
    value = _credential_value_from_file_text(text)
    if not value:
        raise ModelConfigError("MOBIAGENT_API_KEY_FILE is empty")
    return "MOBIAGENT_API_KEY_FILE", value


def _credential_value_from_file_text(text: str) -> str:
    """Read either a bare key or a small JSON credential object."""

    value = text.strip()
    if not value.startswith("{"):
        return value
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ModelConfigError("MOBIAGENT_API_KEY_FILE contains invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ModelConfigError("MOBIAGENT_API_KEY_FILE JSON must be an object")
    for name in ("MOBIAGENT_API_KEY", "OPENAI_API_KEY", "api_key"):
        candidate = payload.get(name)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    raise ModelConfigError(
        "MOBIAGENT_API_KEY_FILE JSON must contain MOBIAGENT_API_KEY, OPENAI_API_KEY, or api_key"
    )


def _looks_like_placeholder(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return any(marker in text for marker in PLACEHOLDER_MARKERS)
