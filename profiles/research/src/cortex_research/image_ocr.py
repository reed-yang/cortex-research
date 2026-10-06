"""Single-image OCR: Novita DeepSeek-OCR-2, with GLM-OCR as the fallback.

This is first-party image OCR for XHS carousel images. PDF OCR stays the
operator skill's (`paper_ingest.py`). The request shapes match the skill's
recorded ones: DeepSeek through Novita's OpenAI-compatible chat endpoint with
the grounding prompt, GLM through `layout_parsing` with the image as a data
URL and no prompt.

One call has an aggregate deadline (300 s by default). Each engine gets one
request inside it and there is no retry loop: a later attempt is the task
queue's decision, never this module's.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from .provider_http import RETRYABLE, Deadline, ProviderError, client, request_json

NOVITA_BASE = "https://api.novita.ai/openai"
NOVITA_MODEL = "deepseek/deepseek-ocr-2"
NOVITA_PROMPT = "<|grounding|>Convert the document to markdown."
NOVITA_MAX_TOKENS = 8000
GLM_URL = "https://open.bigmodel.cn/api/paas/v4/layout_parsing"
GLM_MODEL = "glm-ocr"
DEADLINE_SECONDS = 300.0
NOVITA_TIMEOUT_SECONDS = 180.0
GLM_TIMEOUT_SECONDS = 120.0
MAX_ANSWER_BYTES = 32 * 1024 * 1024
ENGINE_DEEPSEEK = "deepseek-ocr-2"
ENGINE_GLM = "glm-ocr"

# DeepSeek grounding headers: `label[[x1, y1, x2, y2]]`, optionally several
# boxes. Anchored on four integers, so `W[[i]]` in body text is never a header.
_INT4 = r"\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*\d+"
_BLOCK_RE = re.compile(
    r"([A-Za-z_]+)\[\[\s*(" + _INT4 + r"(?:\s*\]\s*,\s*\[\s*" + _INT4 + r")*)\s*\]\]"
)
_ECHO_RE = re.compile(r"<\|?/?(?:image|grounding|ref|det)\|?>|<image>")


@dataclass(frozen=True)
class OcrAttempt:
    engine: str
    ok: bool
    category: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "ok": self.ok,
            "category": self.category,
            "message": self.message,
        }


@dataclass(frozen=True)
class OcrResult:
    """The text one engine produced, and every provider answer seen."""

    engine: str
    markdown: str
    flags: tuple[str, ...]
    finish_reason: str | None
    usage: Mapping[str, Any] | None
    raw: Mapping[str, Any] = field(default_factory=dict)
    attempts: tuple[OcrAttempt, ...] = ()


def grounding_to_markdown(content: str) -> str:
    """Drop DeepSeek's block headers and echoed control tokens, keep the text.

    Figure blocks carry no text and are dropped; the image itself is kept
    beside the transcription, so nothing is lost.
    """

    content = content or ""
    matches = list(_BLOCK_RE.finditer(content))
    if not matches:
        return _ECHO_RE.sub("", content).strip()
    parts: list[str] = []
    leading = _ECHO_RE.sub("", content[: matches[0].start()]).strip()
    if leading:
        parts.append(leading)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        text = _ECHO_RE.sub("", content[match.end() : end]).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _data_url(data: bytes, media_type: str) -> str:
    return f"data:{media_type};base64," + base64.b64encode(data).decode("ascii")


def _deepseek(
    http: httpx.Client, data: bytes, media_type: str, *, api_key: str, base: str, deadline: Deadline
) -> OcrResult:
    document = request_json(
        http,
        "POST",
        base.rstrip("/") + "/chat/completions",
        provider="novita",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "model": NOVITA_MODEL,
            "temperature": 0.0,
            "max_tokens": NOVITA_MAX_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _data_url(data, media_type)}},
                        {"type": "text", "text": NOVITA_PROMPT},
                    ],
                }
            ],
        },
        timeout=deadline.timeout(NOVITA_TIMEOUT_SECONDS, provider="novita"),
        max_bytes=MAX_ANSWER_BYTES,
        deadline=deadline,
    )
    try:
        choice = document["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ProviderError("invalid_response", "novita: answer has no message content") from error
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise ProviderError("invalid_response", "novita: message content is not text")
    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    markdown = grounding_to_markdown(content)
    flags: list[str] = []
    if finish_reason == "length":
        flags.append("truncated")
    if not markdown.strip():
        flags.append("empty")
    usage = document.get("usage") if isinstance(document.get("usage"), dict) else None
    return OcrResult(
        engine=ENGINE_DEEPSEEK,
        markdown=markdown,
        flags=tuple(flags),
        finish_reason=finish_reason if isinstance(finish_reason, str) else None,
        usage=usage,
        raw={ENGINE_DEEPSEEK: document},
    )


def _glm(
    http: httpx.Client,
    data: bytes,
    media_type: str,
    *,
    app_id: str,
    api_key: str,
    url: str,
    deadline: Deadline,
) -> OcrResult:
    document = request_json(
        http,
        "POST",
        url,
        provider="glm",
        # GLM's own scheme: `<id>.<key>`, no `Bearer` prefix.
        headers={"Authorization": f"{app_id}.{api_key}"},
        json_body={"model": GLM_MODEL, "file": _data_url(data, media_type)},
        timeout=deadline.timeout(GLM_TIMEOUT_SECONDS, provider="glm"),
        max_bytes=MAX_ANSWER_BYTES,
        deadline=deadline,
    )
    if not isinstance(document, dict) or "md_results" not in document:
        raise ProviderError("invalid_response", "glm: answer has no md_results")
    markdown = document.get("md_results")
    if markdown is None:
        markdown = ""
    if not isinstance(markdown, str):
        raise ProviderError("invalid_response", "glm: md_results is not text")
    usage = document.get("usage") if isinstance(document.get("usage"), dict) else None
    return OcrResult(
        engine=ENGINE_GLM,
        markdown=markdown.strip(),
        flags=() if markdown.strip() else ("empty",),
        finish_reason=None,
        usage=usage,
        raw={ENGINE_GLM: document},
    )


def _combined_failure(failures: list[ProviderError]) -> ProviderError:
    """One category for an image no engine could read.

    A retryable failure wins, so a later attempt is scheduled; then an
    operator-facing one (`auth`, `payment`); otherwise the last engine's.
    """

    for preferred in (RETRYABLE, frozenset({"auth", "payment"})):
        for failure in failures:
            if failure.category in preferred:
                return ProviderError(failure.category, "; ".join(f.message for f in failures))
    last = failures[-1]
    return ProviderError(last.category, "; ".join(f.message for f in failures))


def ocr_image(
    data: bytes,
    media_type: str,
    *,
    novita_key: str | None = None,
    glm_app_id: str | None = None,
    glm_key: str | None = None,
    novita_base: str = NOVITA_BASE,
    glm_url: str = GLM_URL,
    deadline_seconds: float = DEADLINE_SECONDS,
    transport: httpx.BaseTransport | None = None,
    deadline: Deadline | None = None,
) -> OcrResult:
    """Transcribe one image verbatim.

    DeepSeek runs first when its key is present. GLM runs when DeepSeek failed,
    answered empty text, or has no key. A truncated DeepSeek answer is kept
    and flagged rather than replaced. When GLM also fails after an empty
    DeepSeek answer, the empty answer is the result, flagged `empty`.
    """

    if not data:
        raise ValueError("image bytes are empty")
    have_glm = bool(glm_app_id and glm_key)
    if not novita_key and not have_glm:
        raise ProviderError("auth", "ocr: no novita or glm credential is configured")
    deadline = deadline or Deadline(deadline_seconds)
    attempts: list[OcrAttempt] = []
    failures: list[ProviderError] = []
    raw: dict[str, Any] = {}
    primary: OcrResult | None = None
    with client(transport) as http:
        if novita_key:
            try:
                primary = _deepseek(
                    http, data, media_type, api_key=novita_key, base=novita_base, deadline=deadline
                )
                raw.update(primary.raw)
                attempts.append(OcrAttempt(ENGINE_DEEPSEEK, True))
                if "empty" not in primary.flags:
                    return _with(primary, raw, attempts)
            except ProviderError as error:
                failures.append(error)
                attempts.append(OcrAttempt(ENGINE_DEEPSEEK, False, error.category, error.message))
        if have_glm:
            try:
                fallback = _glm(
                    http,
                    data,
                    media_type,
                    app_id=str(glm_app_id),
                    api_key=str(glm_key),
                    url=glm_url,
                    deadline=deadline,
                )
                raw.update(fallback.raw)
                attempts.append(OcrAttempt(ENGINE_GLM, True))
                if "empty" not in fallback.flags or primary is None:
                    return _with(fallback, raw, attempts)
            except ProviderError as error:
                failures.append(error)
                attempts.append(OcrAttempt(ENGINE_GLM, False, error.category, error.message))
    if primary is not None:
        return _with(primary, raw, attempts)
    raise _combined_failure(failures)


def _with(result: OcrResult, raw: Mapping[str, Any], attempts: list[OcrAttempt]) -> OcrResult:
    return OcrResult(
        engine=result.engine,
        markdown=result.markdown,
        flags=result.flags,
        finish_reason=result.finish_reason,
        usage=result.usage,
        raw=dict(raw),
        attempts=tuple(attempts),
    )
