"""Small OpenAI-compatible model client helpers for App-test execution."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
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


def model_config_from_env(
    *,
    base_url_names: Iterable[str],
    model_names: Iterable[str],
    api_key_names: Iterable[str] = ("MOBIAGENT_API_KEY",),
) -> ModelConfig:
    base_url_name, base_url = _first_env(base_url_names)
    model_name, model = _first_env(model_names)
    api_key_name, api_key = _first_env(api_key_names)
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
    return ModelConfig(base_url=str(base_url), model=str(model), api_key=str(api_key))


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


def _looks_like_placeholder(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return any(marker in text for marker in PLACEHOLDER_MARKERS)
