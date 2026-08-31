from app_test_agent.model_client import ModelConfig
from verification_benchmark.tools import probe_model_service


def test_probe_reports_sanitized_success(monkeypatch):
    config = ModelConfig(
        base_url="https://api.example.test",
        model="gpt-5.4",
        api_key="never-report-this-secret",
        wire_api="responses",
        reasoning_effort="high",
        store=False,
    )
    monkeypatch.setattr(probe_model_service, "model_config_from_env", lambda **kwargs: config)
    monkeypatch.setattr(
        probe_model_service,
        "post_chat_completion",
        lambda *args, **kwargs: '{"status":"ok"}',
    )

    result = probe_model_service.run_probe(timeout=1)

    assert result["status"] == "PASS"
    assert result["device_interaction"] == "NONE"
    assert result["store"] is False
    assert result["response_character_count"] == 15
    assert "api_key" not in result
    assert "response_text" not in result


def test_probe_sanitizes_provider_failure(monkeypatch):
    config = ModelConfig(
        base_url="https://api.example.test",
        model="gpt-5.4",
        api_key="never-report-this-secret",
        wire_api="responses",
    )
    monkeypatch.setattr(probe_model_service, "model_config_from_env", lambda **kwargs: config)

    def fail(*args, **kwargs):
        raise RuntimeError("HTTP 403: response body may be sensitive")

    monkeypatch.setattr(probe_model_service, "post_chat_completion", fail)

    result = probe_model_service.run_probe(timeout=1)

    assert result["status"] == "FAIL"
    assert result["error_type"] == "RuntimeError"
    assert result["http_status"] == 403
    assert "message" not in result
