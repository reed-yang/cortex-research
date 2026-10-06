"""Single-image OCR: request shapes, flags and the GLM fallback, provider-free."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from cortex_research import image_ocr
from cortex_research.provider_http import Deadline, ProviderError

IMAGE = b"\x89PNG\r\n\x1a\n" + b"synthetic"
GROUNDED = (
    "<|ref|>title[[10, 20, 900, 80]]\nAttention Is All You Need\n"
    "text[[10, 100, 900, 300]]\n推荐阅读 arXiv:1706.03762 and W[[i]] stays\n"
    "image[[10, 400, 900, 900]]\n"
)


def deepseek_answer(content: str | None, finish_reason: str = "stop") -> dict:
    return {
        "id": "chatcmpl-synthetic",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }


def glm_answer(markdown: str) -> dict:
    return {"md_results": markdown, "layout_details": [[]], "data_info": {"pages": [{"width": 3, "height": 2}]},
            "usage": {"total_tokens": 5}}


def responses_answer(text: str) -> dict:
    return {"id": "resp-synthetic", "status": "completed", "model": "gpt-6-luna",
            "output": [{"type": "reasoning", "summary": []},
                       {"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 9, "output_tokens": 3}}


class Providers:
    """Answers per host, recording each request."""

    def __init__(self, novita=None, glm=None, responses=None) -> None:
        self.answers = {"api.novita.ai": novita, "open.bigmodel.cn": glm, "gpt.example": responses}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers[request.url.host]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error": "synthetic"})
        return httpx.Response(200, json=answer)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]


KEYS = {"novita_key": "nv-key", "glm_app_id": "glm-id", "glm_key": "glm-key"}
BACKUP = {"responses_key": "gpt-key", "responses_base": "https://gpt.example/v1",
          "responses_model": "gpt-6-luna"}


def test_deepseek_request_shape_and_grounding_removal() -> None:
    providers = Providers(novita=deepseek_answer(GROUNDED))
    result = image_ocr.ocr_image(IMAGE, "image/png", transport=providers.transport, **KEYS)
    assert providers.hosts() == ["api.novita.ai"]
    request = providers.requests[0]
    assert request.url.path == "/openai/chat/completions"
    assert request.headers["authorization"] == "Bearer nv-key"
    body = json.loads(request.content)
    assert body["model"] == "deepseek/deepseek-ocr-2"
    assert body["temperature"] == 0.0 and body["max_tokens"] == 8000
    content = body["messages"][0]["content"]
    assert content[1] == {"type": "text", "text": "<|grounding|>Convert the document to markdown."}
    assert content[0]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(IMAGE).decode()
    assert result.engine == "deepseek-ocr-2"
    assert result.markdown == "Attention Is All You Need\n\n推荐阅读 arXiv:1706.03762 and W[[i]] stays"
    assert result.flags == ()
    assert result.raw["deepseek-ocr-2"]["choices"][0]["message"]["content"] == GROUNDED
    assert [attempt.engine for attempt in result.attempts] == ["deepseek-ocr-2"]


def test_a_length_finish_is_kept_and_flagged_truncated() -> None:
    providers = Providers(novita=deepseek_answer("partial text", finish_reason="length"))
    result = image_ocr.ocr_image(IMAGE, "image/png", transport=providers.transport, **KEYS)
    assert providers.hosts() == ["api.novita.ai"]
    assert result.flags == ("truncated",) and result.markdown == "partial text"
    assert result.finish_reason == "length"


def test_an_empty_answer_falls_back_to_glm() -> None:
    providers = Providers(novita=deepseek_answer("  "), glm=glm_answer("# 标题\n\nGLM text"))
    result = image_ocr.ocr_image(IMAGE, "image/jpeg", transport=providers.transport, **KEYS)
    assert providers.hosts() == ["api.novita.ai", "open.bigmodel.cn"]
    glm_request = providers.requests[1]
    assert glm_request.headers["authorization"] == "glm-id.glm-key"
    assert json.loads(glm_request.content) == {
        "model": "glm-ocr",
        "file": "data:image/jpeg;base64," + base64.b64encode(IMAGE).decode(),
    }
    assert result.engine == "glm-ocr" and result.markdown == "# 标题\n\nGLM text"
    assert set(result.raw) == {"deepseek-ocr-2", "glm-ocr"}


def test_empty_from_both_engines_is_kept_and_flagged() -> None:
    providers = Providers(novita=deepseek_answer(""), glm=glm_answer(""))
    result = image_ocr.ocr_image(IMAGE, "image/png", transport=providers.transport, **KEYS)
    assert result.engine == "deepseek-ocr-2" and result.flags == ("empty",)


def test_an_empty_answer_survives_a_failed_fallback() -> None:
    providers = Providers(novita=deepseek_answer(""), glm=503)
    result = image_ocr.ocr_image(IMAGE, "image/png", transport=providers.transport, **KEYS)
    assert result.engine == "deepseek-ocr-2" and result.flags == ("empty",)
    assert [(a.engine, a.ok, a.category) for a in result.attempts] == [
        ("deepseek-ocr-2", True, None),
        ("glm-ocr", False, "transient"),
    ]


@pytest.mark.parametrize("failure", [500, 429, 402, httpx.ConnectError("refused")])
def test_a_deepseek_failure_falls_back_to_glm(failure) -> None:
    providers = Providers(novita=failure, glm=glm_answer("fallback text"))
    result = image_ocr.ocr_image(IMAGE, "image/png", transport=providers.transport, **KEYS)
    assert result.engine == "glm-ocr" and result.markdown == "fallback text"
    assert result.attempts[0].ok is False


def test_without_a_novita_key_glm_runs_alone() -> None:
    providers = Providers(glm=glm_answer("only glm"))
    result = image_ocr.ocr_image(
        IMAGE, "image/png", glm_app_id="glm-id", glm_key="glm-key", transport=providers.transport
    )
    assert providers.hosts() == ["open.bigmodel.cn"] and result.engine == "glm-ocr"


@pytest.mark.parametrize(
    ("novita", "glm", "category"),
    [
        (429, 401, "rate_limited"),
        (401, 402, "auth"),
        (401, 503, "transient"),
        (400, 400, "upstream_error"),
    ],
)
def test_a_failure_of_both_engines_keeps_one_category(novita, glm, category) -> None:
    providers = Providers(novita=novita, glm=glm)
    with pytest.raises(ProviderError) as caught:
        image_ocr.ocr_image(IMAGE, "image/png", transport=providers.transport, **KEYS)
    assert caught.value.category == category
    for secret in KEYS.values():
        assert secret not in caught.value.message


def test_a_deepseek_failure_uses_the_responses_backup_before_glm() -> None:
    providers = Providers(novita=429, responses=responses_answer("  backup text\n"), glm=glm_answer("never"))
    result = image_ocr.ocr_image(IMAGE, "image/png", transport=providers.transport, **KEYS, **BACKUP)
    assert providers.hosts() == ["api.novita.ai", "gpt.example"]
    request = providers.requests[1]
    assert request.url.path == "/v1/responses"
    assert request.headers["authorization"] == "Bearer gpt-key"
    body = json.loads(request.content)
    assert body["model"] == "gpt-6-luna" and body["stream"] is False
    assert body["reasoning"] == {"effort": "low"}
    content = body["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": image_ocr.RESPONSES_PROMPT}
    assert content[1] == {"type": "input_image", "detail": "high",
                          "image_url": "data:image/png;base64," + base64.b64encode(IMAGE).decode()}
    assert result.engine == "responses" and result.markdown == "backup text" and result.flags == ()
    assert [(a.engine, a.ok, a.category) for a in result.attempts] == [
        ("deepseek-ocr-2", False, "rate_limited"),
        ("responses", True, None),
    ]
    assert set(result.raw) == {"responses"}


def test_an_empty_deepseek_answer_uses_the_backup() -> None:
    providers = Providers(novita=deepseek_answer(""), responses=responses_answer("backup"))
    result = image_ocr.ocr_image(IMAGE, "image/png", transport=providers.transport, **KEYS, **BACKUP)
    assert providers.hosts() == ["api.novita.ai", "gpt.example"]
    assert result.engine == "responses" and set(result.raw) == {"deepseek-ocr-2", "responses"}


@pytest.mark.parametrize(
    ("backup", "category"),
    [
        (503, "transient"),
        ({"status": "incomplete", "output": []}, "invalid_response"),
        (responses_answer("   "), "invalid_response"),
    ],
)
def test_a_failed_backup_falls_back_to_glm(backup, category) -> None:
    providers = Providers(novita=500, responses=backup, glm=glm_answer("glm text"))
    result = image_ocr.ocr_image(IMAGE, "image/png", transport=providers.transport, **KEYS, **BACKUP)
    assert providers.hosts() == ["api.novita.ai", "gpt.example", "open.bigmodel.cn"]
    assert result.engine == "glm-ocr" and result.markdown == "glm text"
    assert [(a.engine, a.category) for a in result.attempts][1] == ("responses", category)


def test_the_backup_needs_its_key_base_and_model() -> None:
    for missing in BACKUP:
        providers = Providers(novita=500, glm=glm_answer("glm text"))
        partial = {name: value for name, value in BACKUP.items() if name != missing}
        result = image_ocr.ocr_image(IMAGE, "image/png", transport=providers.transport, **KEYS, **partial)
        assert providers.hosts() == ["api.novita.ai", "open.bigmodel.cn"] and result.engine == "glm-ocr"


def test_the_backup_alone_is_enough() -> None:
    providers = Providers(responses=responses_answer("only backup"))
    result = image_ocr.ocr_image(IMAGE, "image/png", transport=providers.transport, **BACKUP)
    assert providers.hosts() == ["gpt.example"] and result.engine == "responses"


def test_a_failure_of_all_three_engines_keeps_one_category_and_no_secret() -> None:
    providers = Providers(novita=401, responses=429, glm=400)
    with pytest.raises(ProviderError) as caught:
        image_ocr.ocr_image(IMAGE, "image/png", transport=providers.transport, **KEYS, **BACKUP)
    assert caught.value.category == "rate_limited"
    for secret in (*KEYS.values(), BACKUP["responses_key"]):
        assert secret not in caught.value.message


def test_no_credential_at_all_is_auth() -> None:
    providers = Providers()
    with pytest.raises(ProviderError) as caught:
        image_ocr.ocr_image(IMAGE, "image/png", glm_app_id="only-id", transport=providers.transport)
    assert caught.value.category == "auth" and providers.requests == []


def test_a_malformed_answer_is_invalid_and_falls_back() -> None:
    providers = Providers(novita={"choices": []}, glm={"no": "md"})
    with pytest.raises(ProviderError) as caught:
        image_ocr.ocr_image(IMAGE, "image/png", transport=providers.transport, **KEYS)
    assert caught.value.category == "invalid_response"
    assert providers.hosts() == ["api.novita.ai", "open.bigmodel.cn"]


def test_the_aggregate_deadline_bounds_the_fallback() -> None:
    now = [0.0]
    deadline = Deadline(300, clock=lambda: now[0])

    class Slow(Providers):
        def __call__(self, request):
            now[0] += 301  # the first engine used up the whole budget
            return super().__call__(request)

    providers = Slow(novita=500, glm=glm_answer("never"))
    with pytest.raises(ProviderError) as caught:
        image_ocr.ocr_image(
            IMAGE, "image/png", transport=providers.transport, deadline=deadline, **KEYS
        )
    assert providers.hosts() == ["api.novita.ai"]
    assert caught.value.category == "transient"


def test_request_timeouts_never_exceed_the_remaining_budget() -> None:
    now = [0.0]
    deadline = Deadline(300, clock=lambda: now[0])
    timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        if request.url.host == "api.novita.ai":
            now[0] += 250
            return httpx.Response(500)
        return httpx.Response(200, json=glm_answer("late"))

    result = image_ocr.ocr_image(
        IMAGE, "image/png", transport=httpx.MockTransport(handler), deadline=deadline, **KEYS
    )
    assert result.engine == "glm-ocr"
    assert timeouts == [180.0, 50.0]
