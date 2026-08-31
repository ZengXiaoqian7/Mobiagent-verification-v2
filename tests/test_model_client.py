import json

import pytest

from app_test_agent import model_client


def test_model_config_selects_responses_reasoning_and_no_storage(monkeypatch):
    monkeypatch.setenv("MOBIAGENT_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("MOBIAGENT_MODEL", "gpt-5.5")
    monkeypatch.setenv("MOBIAGENT_API_KEY", "test-only-key")
    monkeypatch.setenv("MOBIAGENT_WIRE_API", "responses")
    monkeypatch.setenv("MOBIAGENT_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("MOBIAGENT_DISABLE_RESPONSE_STORAGE", "true")

    config = model_client.model_config_from_env(
        base_url_names=("MOBIAGENT_BASE_URL",),
        model_names=("MOBIAGENT_MODEL",),
    )

    assert config.wire_api == "responses"
    assert config.reasoning_effort == "xhigh"
    assert config.store is False


def test_verifier_model_client_posts_responses_vision_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"status":"ok"}'}],
                    }
                ]
            }

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(model_client.requests, "post", fake_post)
    config = model_client.ModelConfig(
        base_url="https://api.example.test/v1",
        model="gpt-5.5",
        api_key="test-only-key",
        wire_api="responses",
        reasoning_effort="xhigh",
        store=False,
    )

    result = model_client.post_chat_completion(
        config,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "verify"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                ],
            }
        ],
        temperature=0.8,
        max_tokens=64,
    )

    payload = json.loads(captured["data"].decode("utf-8"))
    assert result == '{"status":"ok"}'
    assert captured["url"] == "https://api.example.test/v1/responses"
    assert payload["reasoning"] == {"effort": "xhigh"}
    assert payload["store"] is False
    assert "temperature" not in payload
    assert payload["input"][0]["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,AA==",
    }


def test_model_config_rejects_unknown_wire_api(monkeypatch):
    monkeypatch.setenv("MOBIAGENT_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("MOBIAGENT_MODEL", "gpt-5.5")
    monkeypatch.setenv("MOBIAGENT_API_KEY", "test-only-key")
    monkeypatch.setenv("MOBIAGENT_WIRE_API", "not-an-api")

    with pytest.raises(model_client.ModelConfigError, match="unsupported MOBIAGENT_WIRE_API"):
        model_client.model_config_from_env(
            base_url_names=("MOBIAGENT_BASE_URL",),
            model_names=("MOBIAGENT_MODEL",),
        )


def test_chat_completions_remains_the_default_transport(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "chat response"}}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(model_client.requests, "post", fake_post)
    config = model_client.ModelConfig(
        base_url="https://api.example.test/v1",
        model="existing-model",
        api_key="test-only-key",
    )

    result = model_client.post_chat_completion(
        config,
        messages=[{"role": "user", "content": "hello"}],
        temperature=0.4,
        max_tokens=32,
    )

    payload = json.loads(captured["data"].decode("utf-8"))
    assert result == "chat response"
    assert captured["url"] == "https://api.example.test/v1/chat/completions"
    assert payload["temperature"] == 0.4
    assert payload["max_tokens"] == 32
