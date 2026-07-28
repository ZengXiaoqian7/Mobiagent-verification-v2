"""Probe whether the API gateway accepts MobiAgent Runner Decider requests.

This script does not control the phone. It sends controlled chat/completions
payloads to compare a simple vision request with the actual qwen_json Decider
message shape used by runner/mobiagent/mobiagent.py.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def first_nonempty(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if value:
            return value
    return None


def mask_key(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def endpoint(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def image_b64(path: Path, resize_factor: float, mask_below_ratio: Optional[float]) -> str:
    if resize_factor == 1.0 and mask_below_ratio is None:
        return base64.b64encode(path.read_bytes()).decode("ascii")

    try:
        from PIL import Image, ImageDraw
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Pillow is required for --resize-factor or --mask-below-ratio") from exc

    img = Image.open(path).convert("RGB")
    if resize_factor != 1.0:
        img = img.resize(
            (max(1, int(img.width * resize_factor)), max(1, int(img.height * resize_factor))),
            Image.Resampling.LANCZOS,
        )
    if mask_below_ratio is not None:
        ratio = max(0.0, min(1.0, mask_below_ratio))
        y = int(img.height * ratio)
        ImageDraw.Draw(img).rectangle([0, y, img.width, img.height], fill=(245, 245, 245))

    output = io.BytesIO()
    img.save(output, format="JPEG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def post_case(base_url: str, api_key: str, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    try:
        response = requests.post(
            endpoint(base_url),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status_code": None,
            "ok": False,
            "exception": f"{type(exc).__name__}: {exc}",
        }
    try:
        body: Any = response.json()
    except Exception:  # noqa: BLE001
        body = response.text[:1000]
    message = None
    if response.ok:
        try:
            message = body["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001
            message = None
    return {
        "status_code": response.status_code,
        "ok": response.ok,
        "body": body,
        "message": message,
    }


def post_case_openai_sdk(base_url: str, api_key: str, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        response = client.chat.completions.create(
            model=payload["model"],
            messages=payload["messages"],
            temperature=payload.get("temperature", 0),
            max_tokens=payload.get("max_tokens", 256),
        )
        message = response.choices[0].message.content
        return {
            "status_code": 200,
            "ok": True,
            "body": response.model_dump(mode="json"),
            "message": message,
        }
    except Exception as exc:  # noqa: BLE001
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        body: Any = None
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                body = response.json()
            except Exception:  # noqa: BLE001
                body = getattr(response, "text", "")[:1000]
        return {
            "status_code": status_code,
            "ok": False,
            "exception": f"{type(exc).__name__}: {exc}",
            "body": body,
        }


def summarize_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summary: List[Dict[str, Any]] = []
    for idx, message in enumerate(messages):
        item: Dict[str, Any] = {"index": idx, "role": message.get("role")}
        content = message.get("content")
        if isinstance(content, str):
            item["content_type"] = "text"
            item["text_chars"] = len(content)
        elif isinstance(content, list):
            parts = []
            for part in content:
                part_type = part.get("type") if isinstance(part, dict) else type(part).__name__
                part_info: Dict[str, Any] = {"type": part_type}
                if isinstance(part, dict) and part_type == "text":
                    part_info["text_chars"] = len(str(part.get("text") or ""))
                elif isinstance(part, dict) and part_type == "image_url":
                    url = ((part.get("image_url") or {}).get("url") or "")
                    part_info["url_chars"] = len(url)
                    part_info["url_prefix"] = url[:30]
                parts.append(part_info)
            item["content_type"] = "parts"
            item["parts"] = parts
        else:
            item["content_type"] = type(content).__name__
        summary.append(item)
    return summary


def remove_images(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    copied = json.loads(json.dumps(messages, ensure_ascii=False))
    for message in copied:
        content = message.get("content")
        if isinstance(content, list):
            message["content"] = [part for part in content if part.get("type") != "image_url"]
    return copied


def text_only_string_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            text = str(content)
        converted.append({"role": message.get("role", "user"), "content": text})
    return converted


def build_payloads(
    model: str,
    image_path: Path,
    task: str,
    resize_factor: float,
    mask_below_ratio: Optional[float],
) -> Dict[str, Dict[str, Any]]:
    from runner.mobiagent.decider_adapters.qwen import get_adapter

    img = image_b64(image_path, resize_factor, mask_below_ratio)
    simple_image_url = f"data:image/jpeg;base64,{img}"
    runner_messages = get_adapter().build_messages(
        task=task,
        history=[],
        screenshot_b64=img,
        use_e2e=True,
        device_type="Android",
    )

    return {
        "simple_text": {
            "model": model,
            "messages": [
                {"role": "user", "content": "Return only JSON: {\"ok\": true}"}
            ],
            "temperature": 0,
            "max_tokens": 64,
        },
        "simple_vision": {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Return only JSON: {\"vision\": true}"},
                        {"type": "image_url", "image_url": {"url": simple_image_url}},
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": 64,
        },
        "simple_system_user_text": {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Return only JSON: {\"ok\": true}"},
            ],
            "temperature": 0,
            "max_tokens": 64,
        },
        "runner_qwen_system_simple_user": {
            "model": model,
            "messages": [
                runner_messages[0],
                {"role": "user", "content": "Return only JSON: {\"ok\": true}"},
            ],
            "temperature": 0,
            "max_tokens": 64,
        },
        "simple_system_runner_user_text": {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                text_only_string_messages(remove_images(runner_messages))[1],
            ],
            "temperature": 0,
            "max_tokens": 256,
        },
        "runner_qwen_text_as_strings": {
            "model": model,
            "messages": text_only_string_messages(remove_images(runner_messages)),
            "temperature": 0,
            "max_tokens": 256,
        },
        "runner_qwen_text_only": {
            "model": model,
            "messages": remove_images(runner_messages),
            "temperature": 0,
            "max_tokens": 256,
        },
        "runner_qwen_full": {
            "model": model,
            "messages": runner_messages,
            "temperature": 0,
            "max_tokens": 256,
        },
        "runner_minimal_action_vision": {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "You are controlling an Android phone. Based on the screenshot, "
                                "return JSON with reasoning, action, and parameters. Use one of "
                                "click, input, swipe, wait, done."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": simple_image_url}},
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": 256,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Runner Decider API compatibility.")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--image", required=True, help="Screenshot image used for vision requests.")
    parser.add_argument("--task", default="在淘宝搜索机械键盘，并进入搜索结果页")
    parser.add_argument("--resize-factor", type=float, default=0.5, help="Match Runner screenshot resize factor.")
    parser.add_argument("--mask-below-ratio", type=float, default=None, help="Match MOBIAGENT_SCREEN_MASK_BELOW_RATIO.")
    parser.add_argument(
        "--cases",
        default=None,
        help="Comma-separated case names to run. Defaults to all cases.",
    )
    parser.add_argument(
        "--client",
        choices=["requests", "openai", "both"],
        default="requests",
        help="HTTP client implementation to use for probes.",
    )
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    base_url = first_nonempty(
        args.base_url,
        os.getenv("MOBIAGENT_DECIDER_BASE_URL"),
        os.getenv("MOBIAGENT_BASE_URL"),
        os.getenv("MOBIFLOW_LLM_BASE_URL"),
        os.getenv("OPENAI_BASE_URL"),
    )
    api_key = first_nonempty(
        args.api_key,
        os.getenv("MOBIAGENT_API_KEY"),
        os.getenv("MOBIFLOW_LLM_API_KEY"),
        os.getenv("OPENAI_API_KEY"),
    )
    model = first_nonempty(
        args.model,
        os.getenv("MOBIAGENT_DECIDER_MODEL"),
        os.getenv("MOBIAGENT_MODEL"),
        os.getenv("MOBIFLOW_LLM_MODEL"),
        os.getenv("OPENAI_MODEL"),
        "gpt-5.4",
    )

    if not base_url or not api_key:
        print("Missing API settings. Set MOBIAGENT_BASE_URL and MOBIAGENT_API_KEY, or pass arguments.")
        return 2

    image_path = Path(args.image)
    if not image_path.exists():
        print(f"image not found: {image_path}")
        return 2

    print("Runner Decider API probe")
    print("base_url:", base_url)
    print("endpoint:", endpoint(base_url))
    print("model:", model)
    print("api_key:", mask_key(api_key))
    print("image:", str(image_path), image_path.stat().st_size, "bytes")
    print("resize_factor:", args.resize_factor)
    print("mask_below_ratio:", args.mask_below_ratio)

    payloads = build_payloads(model, image_path, args.task, args.resize_factor, args.mask_below_ratio)
    if args.cases:
        selected = {case.strip() for case in args.cases.split(",") if case.strip()}
        unknown = selected - set(payloads)
        if unknown:
            print("unknown cases:", ", ".join(sorted(unknown)))
            print("available cases:", ", ".join(payloads))
            return 2
        payloads = {name: payload for name, payload in payloads.items() if name in selected}

    any_failed = False
    for name, payload in payloads.items():
        print(f"\n[{name}]")
        print("message_summary:", json.dumps(summarize_messages(payload["messages"]), ensure_ascii=False))
        clients = ["requests", "openai"] if args.client == "both" else [args.client]
        for client_name in clients:
            print(f"client: {client_name}")
            if client_name == "requests":
                result = post_case(base_url, api_key, payload, args.timeout)
            else:
                result = post_case_openai_sdk(base_url, api_key, payload, args.timeout)
            print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])
            any_failed = any_failed or not result["ok"]

    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
