from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from bombamod.providers import OpenAIProvider, OpenRouterProvider, ProviderError


class CaptureHTTP:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response_data = response
        self.last_request: dict[str, Any] | None = None

    async def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, Any]
    ) -> httpx.Response:
        self.last_request = {"url": url, "headers": headers, "json": json}
        request = httpx.Request("POST", url)
        return httpx.Response(200, json=self.response_data, request=request)


@pytest.mark.asyncio
async def test_omni_uses_supported_model_and_multimodal_payload() -> None:
    capture = CaptureHTTP(
        {
            "results": [
                {
                    "flagged": True,
                    "categories": {"sexual": True},
                    "category_scores": {"sexual": 0.9},
                }
            ]
        }
    )
    result = await OpenAIProvider("secret", capture, max_image_bytes=100000).moderate(
        "hello", ["data:image/png;base64,AA=="]
    )  # type: ignore[arg-type]
    assert result.flagged
    assert capture.last_request is not None
    payload = capture.last_request["json"]
    assert payload["model"] == "omni-moderation-latest"
    assert payload["input"][0] == {"type": "text", "text": "hello"}
    assert payload["input"][1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_openrouter_receives_no_message_text_without_explicit_opt_in() -> None:
    capture = CaptureHTTP(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "action": "escalate",
                                "reason": "Review required",
                                "timeout_minutes": 0,
                                "confidence": 0.8,
                            }
                        )
                    }
                }
            ]
        }
    )
    provider = OpenRouterProvider("secret", "nvidia/nemotron-3-super-120b-a12b:free", capture)  # type: ignore[arg-type]
    decision = await provider.decide(
        rules="Be kind",
        categories={"harassment": True},
        scores={"harassment": 0.9},
        message_text=None,
        strike_count=1,
        minimum_confidence=0.8,
    )
    assert decision.action.value == "escalate"
    assert capture.last_request is not None
    request_message = capture.last_request["json"]["messages"][1]["content"]
    assert "flagged_message_text" in request_message
    assert '"flagged_message_text": null' in request_message


@pytest.mark.asyncio
async def test_openrouter_redacts_opted_in_message_text() -> None:
    capture = CaptureHTTP(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"action": "delete", "reason": "Rule violation", "confidence": 0.99}
                        )
                    }
                }
            ]
        }
    )
    provider = OpenRouterProvider("secret", "model", capture)  # type: ignore[arg-type]
    await provider.decide(
        rules="Be kind",
        categories={"harassment": True},
        scores={"harassment": 0.9},
        message_text="contact user@example.com <@123456789012345678>",
        strike_count=0,
        minimum_confidence=0.8,
    )
    assert capture.last_request is not None
    serialized = json.dumps(capture.last_request["json"])
    assert "user@example.com" not in serialized
    assert "123456789012345678" not in serialized


@pytest.mark.asyncio
async def test_openrouter_rejects_malformed_decision() -> None:
    capture = CaptureHTTP({"choices": [{"message": {"content": "{}"}}]})
    provider = OpenRouterProvider("secret", "model", capture)  # type: ignore[arg-type]
    with pytest.raises(ProviderError):
        await provider.decide(
            rules="rules",
            categories={},
            scores={},
            message_text=None,
            strike_count=0,
            minimum_confidence=0.8,
        )


@pytest.mark.asyncio
async def test_image_fetch_rejects_non_discord_host() -> None:
    class NeverHTTP:
        async def stream(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("must not fetch unapproved URLs")

    provider = OpenAIProvider("key", NeverHTTP(), max_image_bytes=100000)  # type: ignore[arg-type]
    with pytest.raises(ProviderError, match="approved"):
        await provider.fetch_image_data_url("https://example.com/payload.png")
