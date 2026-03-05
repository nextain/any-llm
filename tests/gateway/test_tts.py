"""Tests for /v1/audio/speech TTS endpoint."""
import base64
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient


def test_tts_requires_auth(client: TestClient) -> None:
    """TTS endpoint should require authentication."""
    response = client.post("/v1/audio/speech", json={"input": "hello"})
    assert response.status_code in (401, 422)


def test_tts_returns_audio(
    client: TestClient,
    api_key_header: dict[str, str],
    monkeypatch,
) -> None:
    """TTS should return base64 audio when GCP TTS API succeeds."""
    from any_llm.gateway.routes import tts as tts_route

    fake_audio = base64.b64encode(b"fake-mp3-bytes").decode()
    fake_response = httpx.Response(
        status_code=200,
        json={"audioContent": fake_audio},
    )

    monkeypatch.setattr(tts_route, "validate_user_credit", lambda _db, _user_id: None)
    monkeypatch.setattr(tts_route, "_resolve_tts_api_key", lambda _config: "test-api-key")

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_response):
        response = client.post(
            "/v1/audio/speech",
            json={"input": "안녕하세요", "voice": "ko-KR-Neural2-A"},
            headers=api_key_header,
        )

    assert response.status_code == 200
    data = response.json()
    assert data["audio_content"] == fake_audio
    assert data["character_count"] == 5
    assert data["cost_usd"] > 0


def test_tts_no_api_key_returns_500(
    client: TestClient,
    api_key_header: dict[str, str],
    monkeypatch,
) -> None:
    """TTS should return 500 when no GCP API key is configured."""
    from any_llm.gateway.routes import tts as tts_route

    monkeypatch.setattr(tts_route, "validate_user_credit", lambda _db, _user_id: None)
    monkeypatch.setattr(tts_route, "_resolve_tts_api_key", lambda _config: None)

    response = client.post(
        "/v1/audio/speech",
        json={"input": "hello"},
        headers=api_key_header,
    )

    assert response.status_code == 500
    assert "not configured" in response.json()["detail"]


def test_tts_gcp_error_returns_502(
    client: TestClient,
    api_key_header: dict[str, str],
    monkeypatch,
) -> None:
    """TTS should return 502 when GCP TTS API returns error."""
    from any_llm.gateway.routes import tts as tts_route

    fake_response = httpx.Response(
        status_code=400,
        text="Invalid voice name",
    )

    monkeypatch.setattr(tts_route, "validate_user_credit", lambda _db, _user_id: None)
    monkeypatch.setattr(tts_route, "_resolve_tts_api_key", lambda _config: "test-api-key")

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=fake_response):
        response = client.post(
            "/v1/audio/speech",
            json={"input": "hello", "voice": "invalid-voice"},
            headers=api_key_header,
        )

    assert response.status_code == 502


def test_tts_input_validation(client: TestClient, api_key_header: dict[str, str]) -> None:
    """TTS should reject empty input."""
    response = client.post(
        "/v1/audio/speech",
        json={"input": ""},
        headers=api_key_header,
    )
    assert response.status_code == 422


def test_voice_tier_detection() -> None:
    """Voice tier detection should classify voices correctly."""
    from any_llm.gateway.routes.tts import _voice_tier

    assert _voice_tier("ko-KR-Neural2-A") == "neural2"
    assert _voice_tier("ko-KR-Wavenet-B") == "wavenet"
    assert _voice_tier("ko-KR-Standard-C") == "standard"
    assert _voice_tier("en-US-Neural2-D") == "neural2"


def test_derive_language_code() -> None:
    """Language code derivation from voice name."""
    from any_llm.gateway.routes.tts import _derive_language_code

    assert _derive_language_code("ko-KR-Neural2-A") == "ko-KR"
    assert _derive_language_code("en-US-Wavenet-B") == "en-US"
    assert _derive_language_code("ja-JP-Standard-A") == "ja-JP"
