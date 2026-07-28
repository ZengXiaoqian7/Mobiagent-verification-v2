#!/usr/bin/env python3
"""Check OpenAI-compatible LLM connectivity for verification experiments.

The script intentionally avoids the OpenAI SDK so it can run before optional
project dependencies are installed. It reads credentials from CLI flags or
environment variables and never prints the full API key.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from PIL import Image, ImageDraw


def first_nonempty(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if value:
            return value
    return None


def mask_key(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def build_text_payload(model: str) -> Dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Return only JSON: {\"ok\": true}",
            }
        ],
        "temperature": 0,
        "max_tokens": 64,
    }


def image_bytes_for_request(image_path: Path, mask_below_ratio: Optional[float]) -> bytes:
    if mask_below_ratio is None:
        return image_path.read_bytes()

    ratio = max(0.0, min(1.0, mask_below_ratio))
    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    y = int(img.height * ratio)
    draw.rectangle([0, y, img.width, img.height], fill=(245, 245, 245))
    from io import BytesIO

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def build_vision_payload(model: str, image_path: Path, mask_below_ratio: Optional[float]) -> Dict[str, Any]:
    image_b64 = base64.b64encode(image_bytes_for_request(image_path, mask_below_ratio)).decode("ascii")
    suffix = image_path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "This is a connectivity check. Return only JSON: {\"vision\": true}",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{image_b64}",
                        },
                    },
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": 64,
    }


def post_chat(base_url: str, api_key: str, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    response = requests.post(
        chat_completions_url(base_url),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
    )
    result: Dict[str, Any] = {
        "status_code": response.status_code,
        "ok": response.ok,
    }
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = {"raw_text": response.text[:1000]}
    result["body"] = body
    if response.ok:
        try:
            result["message"] = body["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001
            result["message"] = None
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Check OpenAI-compatible LLM API connectivity.")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--vision-image", default=None, help="Optional local image path for a vision request.")
    parser.add_argument(
        "--mask-below-ratio",
        type=float,
        default=None,
        help="Optional safety diagnostic: mask image content below this height ratio before sending.",
    )
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    base_url = first_nonempty(
        args.base_url,
        os.getenv("MOBIAGENT_GROUNDER_BASE_URL"),
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
        os.getenv("MOBIAGENT_GROUNDER_MODEL"),
        os.getenv("MOBIAGENT_MODEL"),
        os.getenv("MOBIFLOW_LLM_MODEL"),
        os.getenv("OPENAI_MODEL"),
        "gpt-5.4",
    )

    if not base_url or not api_key:
        print("Missing LLM settings.")
        print("Set MOBIAGENT_BASE_URL and MOBIAGENT_API_KEY, or pass --base-url/--api-key.")
        return 2

    print("LLM API connectivity check")
    print(f"base_url: {base_url}")
    print(f"model: {model}")
    print(f"api_key: {mask_key(api_key)}")
    print(f"endpoint: {chat_completions_url(base_url)}")

    text_result = post_chat(base_url, api_key, build_text_payload(model), args.timeout)
    print("\n[text]")
    print(json.dumps(text_result, ensure_ascii=False, indent=2)[:4000])

    if args.vision_image:
        image_path = Path(args.vision_image)
        if not image_path.exists():
            print(f"\n[vision] image not found: {image_path}")
            return 1
        if args.mask_below_ratio is not None:
            print(f"\n[vision] masking image below height ratio {args.mask_below_ratio}")
        vision_result = post_chat(base_url, api_key, build_vision_payload(model, image_path, args.mask_below_ratio), args.timeout)
        print("\n[vision]")
        print(json.dumps(vision_result, ensure_ascii=False, indent=2)[:4000])
        return 0 if text_result["ok"] and vision_result["ok"] else 1

    return 0 if text_result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
