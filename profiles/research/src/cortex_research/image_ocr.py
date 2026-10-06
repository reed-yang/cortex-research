"""Single-image OCR: Novita DeepSeek-OCR-2, then the Responses model, then GLM-OCR.

This is first-party image OCR for XHS carousel images. PDF OCR stays the
operator skill's (`paper_ingest.py`). The DeepSeek and GLM request shapes match
the skill's recorded ones: DeepSeek through Novita's OpenAI-compatible chat
endpoint with the grounding prompt, GLM through `layout_parsing` with the image
as a data URL and no prompt. The backup between them is the operator's
Responses endpoint (`[xhs] gpt_base` and `gpt_model`) with the image as an
`input_image`.

One call has an aggregate deadline (300 s by default). Each engine gets one
request inside it and there is no retry loop: a later attempt is the task
queue's decision, never this module's.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import httpx

from .provider_http import RETRYABLE, Deadline, ProviderError, client, request_json
from .responses_client import output_text

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
ENGINE_RESPONSES = "responses"
ENGINE_GLM = "glm-ocr"
# Low effort: in a blind comparison on carousel images, higher effort read
# illegible small text more eagerly and invented more of it; low skipped it.
RESPONSES_EFFORT = "low"
RESPONSES_TIMEOUT_SECONDS = 120.0
RESPONSES_PROMPT = (
    "Transcribe every piece of visible text in this image exactly as written. Keep the "
    "original language, reading order and line breaks. Render headings, lists and tables "
    "as Markdown and equations as LaTeX. Copy titles, author names, URLs and arXiv IDs "
    "character for character. Leave out text inside charts, plots and diagrams. Do not "
    "summarize, translate, correct, complete or add anything that is not visible; write "
    "[?] for a glyph you cannot read. Output only the transcription."
)

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


def _responses(
    http: httpx.Client,
    data: bytes,
    media_type: str,
    *,
    api_key: str,
    base: str,
    model: str,
    deadline: Deadline,
) -> OcrResult:
    document = request_json(
        http,
        "POST",
        base.rstrip("/") + "/responses",
        provider="responses",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": RESPONSES_PROMPT},
                        {"type": "input_image", "image_url": _data_url(data, media_type), "detail": "high"},
                    ],
                }
            ],
            "stream": False,
            "reasoning": {"effort": RESPONSES_EFFORT},
        },
        timeout=deadline.timeout(RESPONSES_TIMEOUT_SECONDS, provider="responses"),
        max_bytes=MAX_ANSWER_BYTES,
        deadline=deadline,
    )
    # An incomplete or textless answer is `invalid_response`, so GLM runs next.
    markdown = output_text(document).strip()
    usage = document.get("usage") if isinstance(document.get("usage"), dict) else None
    return OcrResult(
        engine=ENGINE_RESPONSES,
        markdown=markdown,
        flags=(),
        finish_reason=None,
        usage=usage,
        raw={ENGINE_RESPONSES: document},
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
    responses_key: str | None = None,
    responses_base: str | None = None,
    responses_model: str | None = None,
    novita_base: str = NOVITA_BASE,
    glm_url: str = GLM_URL,
    deadline_seconds: float = DEADLINE_SECONDS,
    transport: httpx.BaseTransport | None = None,
    deadline: Deadline | None = None,
) -> OcrResult:
    """Transcribe one image verbatim.

    The engines run in order, each only when configured: DeepSeek, then the
    Responses model, then GLM. The next one runs when the previous failed or
    answered empty text. A truncated DeepSeek answer is kept and flagged
    rather than replaced. When every later engine fails or is empty too, the
    first empty answer is the result, flagged `empty`.
    """

    if not data:
        raise ValueError("image bytes are empty")
    deadline = deadline or Deadline(deadline_seconds)
    engines: list[tuple[str, Callable[[httpx.Client], OcrResult]]] = []
    if novita_key:
        engines.append((ENGINE_DEEPSEEK, lambda http: _deepseek(
            http, data, media_type, api_key=novita_key, base=novita_base, deadline=deadline
        )))
    if responses_key and responses_base and responses_model:
        engines.append((ENGINE_RESPONSES, lambda http: _responses(
            http, data, media_type, api_key=responses_key, base=responses_base,
            model=responses_model, deadline=deadline,
        )))
    if glm_app_id and glm_key:
        engines.append((ENGINE_GLM, lambda http: _glm(
            http, data, media_type, app_id=str(glm_app_id), api_key=str(glm_key), url=glm_url,
            deadline=deadline,
        )))
    if not engines:
        raise ProviderError("auth", "ocr: no novita, responses or glm credential is configured")
    attempts: list[OcrAttempt] = []
    failures: list[ProviderError] = []
    raw: dict[str, Any] = {}
    empty: OcrResult | None = None
    with client(transport) as http:
        for engine, run in engines:
            try:
                result = run(http)
            except ProviderError as error:
                failures.append(error)
                attempts.append(OcrAttempt(engine, False, error.category, error.message))
                continue
            raw.update(result.raw)
            attempts.append(OcrAttempt(engine, True))
            if "empty" not in result.flags:
                return _with(result, raw, attempts)
            empty = empty or result
    if empty is not None:
        return _with(empty, raw, attempts)
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
