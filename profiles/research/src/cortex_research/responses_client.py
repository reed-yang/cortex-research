"""Non-streaming Responses API calls through the operator's sub2api endpoint.

Used for XHS recommendation identification (no tools) and blog link search
(`web_search`). The answer text is the concatenation of every
`output[*].content[*]` entry whose `type` is `output_text`; reasoning and
search-call entries are not answer text. Parsing that text as JSON belongs to
the caller, which knows the schema it asked for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from .provider_http import Deadline, ProviderError, client, request_json

REQUEST_TIMEOUT_SECONDS = 240.0
MAX_ANSWER_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ResponseText:
    text: str
    response_id: str | None
    status: str | None
    model: str | None
    usage: Mapping[str, Any] | None


def output_text(document: Any) -> str:
    """The answer text of one Responses document, or `invalid_response`."""

    if not isinstance(document, dict):
        raise ProviderError("invalid_response", "responses: answer is not an object")
    status = document.get("status")
    if status not in (None, "completed"):
        raise ProviderError("invalid_response", f"responses: status is {status!s:.32}")
    output = document.get("output")
    if not isinstance(output, list):
        raise ProviderError("invalid_response", "responses: answer has no output list")
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for entry in content:
            if isinstance(entry, dict) and entry.get("type") == "output_text":
                text = entry.get("text")
                if not isinstance(text, str):
                    raise ProviderError("invalid_response", "responses: output_text is not text")
                parts.append(text)
    text = "".join(parts)
    if not text.strip():
        raise ProviderError("invalid_response", "responses: answer has no output_text")
    return text


def _require_base(base: str) -> str:
    parts = urlsplit(base or "")
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ProviderError("auth", "responses: gpt_base is not configured")
    return base.rstrip("/")


def create_response(
    input_text: str,
    *,
    base: str,
    api_key: str,
    model: str,
    effort: str,
    tools: Sequence[Mapping[str, Any]] = (),
    instructions: str | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> ResponseText:
    """POST `<base>/responses` once with `stream: false` and read its text."""

    endpoint = _require_base(base) + "/responses"
    if not api_key:
        raise ProviderError("auth", "responses: no credential is configured")
    body: dict[str, Any] = {
        "model": model,
        "input": input_text,
        "stream": False,
        "reasoning": {"effort": effort},
    }
    if instructions:
        body["instructions"] = instructions
    if tools:
        body["tools"] = [dict(tool) for tool in tools]
    deadline = Deadline(timeout_seconds)
    with client(transport) as http:
        document = request_json(
            http,
            "POST",
            endpoint,
            provider="responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json_body=body,
            timeout=deadline.timeout(timeout_seconds, provider="responses"),
            max_bytes=MAX_ANSWER_BYTES,
            deadline=deadline,
        )
    text = output_text(document)
    usage = document.get("usage") if isinstance(document.get("usage"), dict) else None
    return ResponseText(
        text=text,
        response_id=document.get("id") if isinstance(document.get("id"), str) else None,
        status=document.get("status") if isinstance(document.get("status"), str) else None,
        model=document.get("model") if isinstance(document.get("model"), str) else None,
        usage=usage,
    )
