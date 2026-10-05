"""Responses API calls: request shape and `output_text` parsing, provider-free."""

from __future__ import annotations

import json

import httpx
import pytest

from cortex_research import responses_client
from cortex_research.provider_http import ProviderError

BASE = "https://gpt.example/v1"


def answer(*texts: str, status: str = "completed") -> dict:
    return {
        "id": "resp_synthetic",
        "status": status,
        "model": "gpt-6-luna",
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "web_search_call", "status": "completed"},
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    *({"type": "output_text", "text": text, "annotations": []} for text in texts),
                    {"type": "refusal", "refusal": "ignored"},
                ],
            },
        ],
        "usage": {"input_tokens": 12, "output_tokens": 34},
    }


def call(document=None, *, status: int = 200, seen: list | None = None, **kwargs):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=document if document is not None else {})

    arguments = {"base": BASE, "api_key": "gpt-key", "model": "gpt-6-luna", "effort": "xhigh"}
    arguments.update(kwargs)
    return responses_client.create_response(
        "input text", transport=httpx.MockTransport(handler), **arguments
    )


def test_the_request_is_non_streaming_with_effort_and_tools() -> None:
    seen: list[httpx.Request] = []
    result = call(
        answer('{"url": null, ', '"page_title": null}'),
        seen=seen,
        tools=[{"type": "web_search"}],
        instructions="find it",
    )
    request = seen[0]
    assert str(request.url) == BASE + "/responses"
    assert request.headers["authorization"] == "Bearer gpt-key"
    body = json.loads(request.content)
    assert body == {
        "model": "gpt-6-luna",
        "input": "input text",
        "stream": False,
        "reasoning": {"effort": "xhigh"},
        "instructions": "find it",
        "tools": [{"type": "web_search"}],
    }
    assert result.text == '{"url": null, "page_title": null}'
    assert result.response_id == "resp_synthetic" and result.usage["output_tokens"] == 34


def test_identification_sends_no_tools() -> None:
    seen: list[httpx.Request] = []
    call(answer('{"items": []}'), seen=seen)
    assert "tools" not in json.loads(seen[0].content)


@pytest.mark.parametrize(
    "document",
    [
        {"output": []},
        {"status": "completed"},
        answer("   "),
        answer("partial", status="incomplete"),
        {"output": [{"type": "message", "content": [{"type": "output_text", "text": 3}]}]},
        ["not", "an", "object"],
    ],
)
def test_a_malformed_answer_is_invalid(document) -> None:
    with pytest.raises(ProviderError) as caught:
        call(document)
    assert caught.value.category == "invalid_response"


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, "auth"), (402, "payment"), (429, "rate_limited"), (500, "transient"), (400, "upstream_error")],
)
def test_http_failures_map_to_categories(status: int, category: str) -> None:
    with pytest.raises(ProviderError) as caught:
        call({"error": {"message": "gpt-key leaked?"}}, status=status)
    assert caught.value.category == category
    assert "gpt-key" not in caught.value.message


@pytest.mark.parametrize("base", ["", "not a url", "ftp://gpt.example"])
def test_a_blank_base_fails_closed(base: str) -> None:
    seen: list[httpx.Request] = []
    with pytest.raises(ProviderError) as caught:
        call(answer("x"), seen=seen, base=base)
    assert caught.value.category == "auth" and seen == []


def test_a_missing_key_fails_closed() -> None:
    with pytest.raises(ProviderError) as caught:
        call(answer("x"), api_key="")
    assert caught.value.category == "auth"
