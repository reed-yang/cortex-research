"""The XHS and blog child operations: dispatch, credentials and write roots.

Provider-free. Handlers run in-process against `httpx.MockTransport`; the real
child runs only against a loopback fake TikHub or against URLs the fetch
policy refuses before any request. Every value is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from cortex_platform.product.control.xhs_store import XHS_FAILURE_CATEGORIES
from cortex_platform.product.engine import child
from cortex_platform.product.engine.bindings import operation_secret_scope
from cortex_platform.product.engine.protocol import (
    ARXIV_OPERATIONS,
    OPERATIONS,
    PROVIDER_FAILURE_CATEGORIES,
    PROVIDER_OPERATIONS,
    PROVIDER_WRITE_ROOTS,
    EffectRequest,
)
from cortex_platform.product.engine.supervisor import ResearchEffectSupervisor
from cortex_platform.product.secrets import SecretValue
from cortex_platform.product.xhs.results import ENGINE_SHAPES, validate_engine

from .conftest import ActivationGate

USER = "5f0e1d2c3b4a596877665544"
NOTE = "6a0b1c2d3e4f5a6b7c8d9e0f"
CREDENTIAL_VARIABLES = {
    "CORTEX_TIKHUB_API_KEY": "dummy-tikhub",
    "CORTEX_GPT_API_KEY": "dummy-gpt",
    "CORTEX_JINA_API_KEY": "dummy-jina",
    "NOVITA_API_KEY": "dummy-novita",
    "GLM_API_ID": "dummy-glm-id",
    "GLM_API_KEY": "dummy-glm",
    "OPENROUTER_API_KEY": "dummy-embedding",
}


def png(width: int = 2, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
        )

    rows = b"".join(b"\x00" + b"\x00\x80\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def list_document() -> dict[str, Any]:
    note = {
        "id": NOTE,
        "type": "normal",
        "sticky": False,
        "title": "Papers this week",
        "desc": "本周论文 papers of the week",
        "create_time": 1_759_600_000,
        "cursor": "cursor-1",
        "user": {"userid": USER, "nickname": "Synthetic Curator"},
        "images_list": [
            {"fileid": "spectrum/a", "width": 2, "height": 2, "original": "",
             "url_size_large": "https://cdn.example/a?sign=s1&t=1", "url": ""}
        ],
    }
    return {"code": 200, "data": {"code": 0, "success": True,
                                  "data": {"has_more": True, "notes": [note]}}}


def responses_answer(text: str) -> dict[str, Any]:
    return {
        "id": "resp_synthetic",
        "status": "completed",
        "model": "gpt-6-luna",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


def request_for(operation: str, payload: dict[str, Any], write_roots=("/nonexistent",)):
    return EffectRequest(
        operation=operation,
        payload=payload,
        marker="m0",
        result_path="/nonexistent/result.json",
        research_db="",
        state_dir="/nonexistent",
        write_roots=tuple(str(root) for root in write_roots),
    )


class Recorder:
    def __init__(self, respond) -> None:
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in CREDENTIAL_VARIABLES.items():
        monkeypatch.setenv(name, value)


def fake_http(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    """Route every provider client's own HTTP client through `recorder`."""

    from cortex_research import blog_fetch, image_ocr, provider_http, responses_client, xhs_client

    def client(transport=None):
        # A transport a test injected for one call still wins.
        return httpx.Client(
            transport=transport or httpx.MockTransport(recorder),
            trust_env=False,
            follow_redirects=False,
        )

    for module in (blog_fetch, image_ocr, provider_http, responses_client, xhs_client):
        monkeypatch.setattr(module, "client", client)


def public_resolver(host: str, port: int, type: int = 0):  # noqa: A002
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def dispatch(operation: str, payload: dict[str, Any], **kwargs) -> dict[str, Any]:
    outcome = child._HANDLERS[operation](request_for(operation, payload, **kwargs))
    # Every successful handler result is what the parent's check accepts,
    # and survives the JSON round trip the result file makes.
    validate_engine(operation, json.loads(json.dumps(outcome["engine"])))
    return outcome


def refusal(operation: str, payload: dict[str, Any], **kwargs) -> child._Refusal:
    with pytest.raises(child._Refusal) as caught:
        dispatch(operation, payload, **kwargs)
    return caught.value


# -- the contract -------------------------------------------------------------


def test_every_operation_has_a_handler_and_a_secret_scope() -> None:
    assert set(child._HANDLERS) == OPERATIONS
    assert OPERATIONS == ARXIV_OPERATIONS | PROVIDER_OPERATIONS
    assert not ARXIV_OPERATIONS & PROVIDER_OPERATIONS
    assert set(PROVIDER_WRITE_ROOTS) <= PROVIDER_OPERATIONS
    assert set(PROVIDER_WRITE_ROOTS.values()) == {"xhs-notes", "blogs"}


def test_provider_categories_are_the_task_store_allowlist() -> None:
    from cortex_research.provider_http import CATEGORIES

    assert PROVIDER_FAILURE_CATEGORIES == XHS_FAILURE_CATEGORIES
    assert CATEGORIES | {"outcome_unknown"} == PROVIDER_FAILURE_CATEGORIES


def test_every_provider_operation_has_a_result_shape() -> None:
    assert set(ENGINE_SHAPES) == PROVIDER_OPERATIONS


@pytest.mark.parametrize(
    ("operation", "engine"),
    [
        ("xhs_list_page", {"user_id": USER}),
        ("xhs_download_image", {"image": {"name": "x", "sha256": "y", "byte_size": True,
                                          "media_type": "image/png", "extension": "png",
                                          "width": None, "height": None}}),
        ("xhs_resolve_link", {"prompt_version": "v", "url": None, "page_title": None,
                              "url_state": "auto_matched", "final_url": None,
                              "checked_title": None, "verification_failure": None}),
        ("ingest_arxiv", {}),
        ("xhs_ocr_image", ["not", "an", "object"]),
    ],
)
def test_a_malformed_result_is_rejected(operation: str, engine: Any) -> None:
    with pytest.raises(ValueError):
        validate_engine(operation, engine)


def test_the_provider_modules_are_imported_lazily() -> None:
    source = Path(child.__file__).read_text(encoding="utf-8")
    header = source.split("def ", 1)[0]
    assert "cortex_research" not in header


# -- in-process handlers ------------------------------------------------------


def test_list_page_sends_only_the_tikhub_key(monkeypatch, credentials) -> None:
    recorder = Recorder(lambda request: httpx.Response(200, json=list_document()))
    fake_http(monkeypatch, recorder)
    outcome = dispatch(
        "xhs_list_page", {"user_id": USER, "cursor": "", "tikhub_base": "https://tikhub.example"}
    )
    request = recorder.requests[0]
    assert request.url.host == "tikhub.example"
    assert request.headers["authorization"] == "Bearer dummy-tikhub"
    engine = outcome["engine"]
    assert engine["has_more"] is True and engine["next_cursor"] == "cursor-1"
    assert engine["notes"][0]["images"][0]["url"].endswith("sign=s1&t=1")
    assert "sign=" not in json.dumps(engine["raw"])
    assert outcome["paper_dirs"] == []


@pytest.mark.parametrize(
    ("status", "category"), [(401, "auth"), (402, "payment"), (429, "rate_limited"), (503, "transient")]
)
def test_detail_failures_keep_their_category(monkeypatch, credentials, status, category) -> None:
    fake_http(monkeypatch, Recorder(lambda request: httpx.Response(status)))
    failure = refusal("xhs_note_detail", {"note_id": NOTE, "tikhub_base": "https://tikhub.example"})
    assert failure.category == category
    assert "dummy-tikhub" not in failure.message


def test_an_unbound_tikhub_key_is_auth_without_a_request(monkeypatch) -> None:
    monkeypatch.delenv("CORTEX_TIKHUB_API_KEY", raising=False)
    recorder = Recorder(lambda request: httpx.Response(200, json=list_document()))
    fake_http(monkeypatch, recorder)
    failure = refusal("xhs_list_page", {"user_id": USER, "tikhub_base": "https://tikhub.example"})
    assert failure.category == "auth" and recorder.requests == []


def test_a_malformed_payload_is_refused(credentials) -> None:
    assert refusal("xhs_list_page", {"user_id": "short", "tikhub_base": "https://t.example"}).category == (
        "invalid_response"
    )
    assert refusal("xhs_note_detail", {"tikhub_base": "https://t.example"}).category == "invalid_response"


def test_download_writes_only_under_its_bound_root(monkeypatch, tmp_path: Path) -> None:
    from cortex_research import xhs_client

    image = png()
    recorder = Recorder(lambda request: httpx.Response(200, content=image))
    real = xhs_client.download_image
    monkeypatch.setattr(
        xhs_client,
        "download_image",
        lambda url, destination: real(
            url, destination, transport=httpx.MockTransport(recorder), resolver=public_resolver
        ),
    )
    root = tmp_path / "xhs-notes"
    staging = root / NOTE / ".staging"
    outcome = dispatch(
        "xhs_download_image",
        {"note_id": NOTE, "ordinal": 1, "url": "https://cdn.example/a?sign=s1", "staging_dir": str(staging)},
        write_roots=(root,),
    )
    digest = hashlib.sha256(image).hexdigest()
    assert outcome["engine"]["image"]["name"] == f"{digest}.png"
    assert outcome["engine"]["image"]["sha256"] == digest
    assert "path" not in outcome["engine"]["image"]
    assert (staging / f"{digest}.png").read_bytes() == image
    assert "authorization" not in recorder.requests[0].headers

    outside = refusal(
        "xhs_download_image",
        {"url": "https://cdn.example/a", "staging_dir": str(tmp_path / "elsewhere")},
        write_roots=(root,),
    )
    assert outside.category == "invalid_response"
    assert refusal(
        "xhs_download_image", {"url": "https://cdn.example/a", "staging_dir": str(root)},
        write_roots=(root,),
    ).category == "invalid_response"


def test_ocr_reads_local_bytes_and_uses_only_ocr_keys(monkeypatch, credentials, tmp_path) -> None:
    image = png()
    path = tmp_path / "image.png"
    path.write_bytes(image)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "text[[1, 2, 3, 4]]\n第一页 page one"},
                         "finish_reason": "length"}]
        })

    recorder = Recorder(respond)
    fake_http(monkeypatch, recorder)
    outcome = dispatch(
        "xhs_ocr_image", {"image_path": str(path), "sha256": hashlib.sha256(image).hexdigest()}
    )
    engine = outcome["engine"]
    assert engine["engine"] == "deepseek-ocr-2"
    assert engine["markdown"] == "第一页 page one"
    assert engine["flags"] == ["truncated"]
    assert engine["text_sha256"] == hashlib.sha256("第一页 page one".encode()).hexdigest()
    assert recorder.requests[0].headers["authorization"] == "Bearer dummy-novita"
    body = json.loads(recorder.requests[0].content)
    assert body["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")

    changed = refusal("xhs_ocr_image", {"image_path": str(path), "sha256": "0" * 64})
    assert changed.category == "invalid_response"


def test_ocr_runs_the_responses_model_first_only_with_a_configured_base(
    monkeypatch, credentials, tmp_path
) -> None:
    image = png()
    path = tmp_path / "image.png"
    path.write_bytes(image)
    answers = {
        "gpt.example": lambda: httpx.Response(200, json=responses_answer("第一页 arXiv:2609.15903v2")),
        "api.novita.ai": lambda: httpx.Response(200, json={
            "choices": [{"message": {"content": "第一页"}, "finish_reason": "stop"}]
        }),
    }
    recorder = Recorder(lambda request: answers[request.url.host]())
    fake_http(monkeypatch, recorder)
    payload = {"image_path": str(path), "sha256": hashlib.sha256(image).hexdigest()}

    engine = dispatch("xhs_ocr_image", {**payload, "gpt_base": "https://gpt.example/v1",
                                         "gpt_model": "gpt-6-luna"})["engine"]
    assert [request.url.host for request in recorder.requests] == ["gpt.example"]
    assert engine["engine"] == "responses" and engine["markdown"] == "第一页 arXiv:2609.15903v2"
    request = recorder.requests[0]
    assert request.url.path == "/v1/responses"
    assert request.headers["authorization"] == "Bearer dummy-gpt"
    assert json.loads(request.content)["reasoning"] == {"effort": "low"}

    recorder.requests.clear()
    engine = dispatch("xhs_ocr_image", {**payload, "gpt_base": "", "gpt_model": "gpt-6-luna"})["engine"]
    assert [request.url.host for request in recorder.requests] == ["api.novita.ai"]
    assert engine["engine"] == "deepseek-ocr-2"


TRANSCRIPTIONS = [
    {"image": 1, "text": "Reading list\nAttention Is All You Need arXiv:1706.03762"},
    {"image": 3, "text": "Blog: The Illustrated Transformer https://jalammar.example/illustrated"},
]


def identify_payload(**extra) -> dict[str, Any]:
    payload = {
        "caption": "本周推荐 recommendations",
        "transcriptions": TRANSCRIPTIONS,
        "gpt_base": "https://gpt.example/v1",
        "gpt_model": "gpt-6-luna",
        "gpt_effort": "xhigh",
    }
    payload.update(extra)
    return payload


def test_identify_filters_the_model_answer_verbatim(monkeypatch, credentials) -> None:
    answer = {
        "items": [
            {"kind": "paper", "title": "Attention Is All You Need", "image": 1,
             "quote": "attention is  all you need", "arxiv_id": "1706.03762", "url": None},
            {"kind": "blog", "title": "The Illustrated Transformer", "image": 3,
             "quote": "Blog: The Illustrated Transformer", "arxiv_id": None,
             "url": "https://jalammar.example/illustrated"},
            {"kind": "paper", "title": "An Invented Paper", "image": 1,
             "quote": "An Invented Paper", "arxiv_id": None, "url": None},
            {"kind": "blog", "title": "The Illustrated Transformer", "image": 2,
             "quote": "The Illustrated Transformer", "arxiv_id": None, "url": None},
        ]
    }
    recorder = Recorder(lambda request: httpx.Response(200, json=responses_answer(json.dumps(answer))))
    fake_http(monkeypatch, recorder)
    engine = dispatch("xhs_identify", identify_payload())["engine"]
    request = recorder.requests[0]
    assert request.headers["authorization"] == "Bearer dummy-gpt"
    body = json.loads(request.content)
    assert "tools" not in body and body["stream"] is False
    assert "## Image 3" in body["input"] and "## Image 2" not in body["input"]
    assert engine["prompt_version"] and engine["dropped"] == 2
    items = {item["item_key"]: item for item in engine["items"]}
    paper = items["arxiv:1706.03762"]
    assert paper["origin"] == "rule+model" and paper["image"] == 1
    blog = next(item for item in engine["items"] if item["kind"] == "blog")
    assert blog["url"] == "https://jalammar.example/illustrated"
    assert blog["url_state"] == "from_text" and blog["image"] == 3


@pytest.mark.parametrize("text", ["not json", '{"items": "nope"}', '{"items": [{"kind": "x"}]}'])
def test_a_malformed_identification_fails_and_never_returns_empty(monkeypatch, credentials, text) -> None:
    fake_http(monkeypatch, Recorder(lambda request: httpx.Response(200, json=responses_answer(text))))
    assert refusal("xhs_identify", identify_payload()).category == "invalid_response"


def test_identify_without_a_base_fails_closed(monkeypatch, credentials) -> None:
    recorder = Recorder(lambda request: httpx.Response(200))
    fake_http(monkeypatch, recorder)
    assert refusal("xhs_identify", identify_payload(gpt_base="")).category == "auth"
    assert recorder.requests == []


def _link_world(monkeypatch, answer: dict[str, Any], page: httpx.Response | None):
    from cortex_research import blog_fetch

    gpt = Recorder(lambda request: httpx.Response(200, json=responses_answer(json.dumps(answer))))
    fake_http(monkeypatch, gpt)
    site = Recorder(lambda request: page)
    real = blog_fetch.fetch_title
    monkeypatch.setattr(
        blog_fetch,
        "fetch_title",
        lambda url: real(url, transport=httpx.MockTransport(site), resolver=public_resolver),
    )
    return gpt, site


def test_resolve_link_verifies_the_page_title(monkeypatch, credentials) -> None:
    page = httpx.Response(
        200,
        text="<title>The Illustrated Transformer – Synthetic Blog</title>",
        headers={"content-type": "text/html"},
    )
    gpt, site = _link_world(
        monkeypatch, {"url": "https://jalammar.example/illustrated", "page_title": "x"}, page
    )
    engine = dispatch("xhs_resolve_link", identify_payload(title="The Illustrated Transformer"))["engine"]
    assert json.loads(gpt.requests[0].content)["tools"] == [{"type": "web_search"}]
    assert engine["url_state"] == "auto_matched"
    assert engine["checked_title"] == "The Illustrated Transformer – Synthetic Blog"
    assert "authorization" not in site.requests[0].headers


def test_resolve_link_labels_a_mismatch_and_a_null(monkeypatch, credentials) -> None:
    page = httpx.Response(200, text="<title>Something Else Entirely</title>",
                          headers={"content-type": "text/html"})
    _link_world(monkeypatch, {"url": "https://other.example/", "page_title": None}, page)
    engine = dispatch("xhs_resolve_link", identify_payload(title="The Illustrated Transformer"))["engine"]
    assert engine["url_state"] == "unverified" and engine["url"] == "https://other.example/"

    _link_world(monkeypatch, {"url": None, "page_title": None}, None)
    engine = dispatch("xhs_resolve_link", identify_payload(title="The Illustrated Transformer"))["engine"]
    assert engine["url_state"] == "not_found" and engine["url"] is None


def test_blog_fetch_writes_article_and_raw_under_its_root(monkeypatch, credentials, tmp_path) -> None:
    from cortex_research import blog_fetch

    paragraphs = "".join(
        f"<p>Paragraph {index} explains one more step of the synthetic method in detail.</p>"
        for index in range(8)
    )
    page = f"<html><head><title>Synthetic Post</title></head><body><article>{paragraphs}</article></body></html>"
    site = Recorder(lambda request: httpx.Response(200, text=page, headers={"content-type": "text/html"}))
    real = blog_fetch.fetch_blog
    monkeypatch.setattr(
        blog_fetch,
        "fetch_blog",
        lambda url, **options: real(
            url, transport=httpx.MockTransport(site), resolver=public_resolver, **options
        ),
    )
    root = tmp_path / "blogs"
    staging = root / "abc" / ".staging"
    engine = dispatch(
        "blog_fetch",
        {"url": "HTTPS://Blog.Example/Post#x", "staging_dir": str(staging)},
        write_roots=(root,),
    )["engine"]
    assert engine["normalized_url"] == "https://blog.example/Post"
    assert engine["authority_id"] == hashlib.sha256(b"https://blog.example/Post").hexdigest()
    assert engine["metadata"]["content_source"] == "origin"
    assert set(engine["files"]) == {"article.md", "raw/page.html"}
    assert (staging / "raw" / "page.html").read_text() == page
    assert (staging / "article.md").read_text().startswith("# Synthetic Post")
    assert "authorization" not in site.requests[0].headers

    unsafe = refusal(
        "blog_fetch", {"url": "https://user:pw@blog.example/", "staging_dir": str(staging)},
        write_roots=(root,),
    )
    assert unsafe.category == "not_found"


def test_blog_fetch_writes_copied_images_under_assets(monkeypatch, credentials, tmp_path) -> None:
    from cortex_research import blog_fetch

    figure = b"\x89PNG\r\n\x1a\n" + b"\x01" * 24
    copied = blog_fetch.BlogImage("page-01-0123456789ab.png", "https://blog.example/f.png", figure)
    article = blog_fetch.BlogArticle(
        requested_url="https://blog.example/Post", final_url="https://blog.example/Post",
        content_source="origin", title="Synthetic Post", author=None, date=None,
        markdown=f"Body.\n\n![Figure](assets/{copied.name})", page_html=b"<html></html>",
        jina_text=None, origin_failure=None, images=(copied,), images_not_copied=1,
    )
    monkeypatch.setattr(blog_fetch, "fetch_blog", lambda url, **options: article)
    root = tmp_path / "blogs"
    staging = root / "abc" / ".staging"
    engine = dispatch(
        "blog_fetch",
        {"url": "https://blog.example/Post", "staging_dir": str(staging)},
        write_roots=(root,),
    )["engine"]
    assert set(engine["files"]) == {"article.md", "raw/page.html", f"assets/{copied.name}"}
    assert engine["files"][f"assets/{copied.name}"] == {
        "sha256": hashlib.sha256(figure).hexdigest(), "bytes": len(figure),
    }
    assert (staging / "assets" / copied.name).read_bytes() == figure
    assert (engine["metadata"]["images"], engine["metadata"]["images_not_copied"]) == (1, 1)


# -- the real child -----------------------------------------------------------


class _FakeTikHub(BaseHTTPRequestHandler):
    seen: list[dict[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        _FakeTikHub.seen.append({"path": self.path, "authorization": self.headers.get("Authorization", "")})
        body = json.dumps(list_document()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


@pytest.fixture
def tikhub():
    _FakeTikHub.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeTikHub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def every_credential(operation: str) -> dict[str, SecretValue]:
    # Over-supplies on purpose: the supervisor narrows to the scope.
    return {
        alias: SecretValue(alias, f"dummy-{alias}")
        for alias in ("tikhub", "sub2api-gpt", "jina", "novita", "glm", "glm-app-id", "openrouter")
    }


@pytest.fixture
def supervisor(roots, research_db) -> ResearchEffectSupervisor:
    return ResearchEffectSupervisor(
        store=ActivationGate(True),
        roots=roots,
        python_executable=Path(sys.executable),
        secret_provider=every_credential,
        timeout_seconds=120,
    )


def test_a_real_list_page_child_gets_one_key_and_writes_nothing(supervisor, roots, tikhub) -> None:
    wal = roots.research_db.with_name(roots.research_db.name + "-wal")
    before = roots.research_db.stat().st_mtime_ns
    execution = supervisor.run(
        "xhs_list_page", {"user_id": USER, "cursor": "", "tikhub_base": tikhub}
    )
    assert execution.ok, (execution.failure_category, execution.failure_message, execution.stderr_tail)
    assert validate_engine("xhs_list_page", execution.engine)["notes"][0]["note_id"] == NOTE
    assert execution.checkpointed and execution.write_boundary["ok"]
    assert _FakeTikHub.seen[0]["authorization"] == "Bearer dummy-tikhub"
    assert roots.research_db.stat().st_mtime_ns == before and not wal.exists()
    assert operation_secret_scope("xhs_list_page").aliases == {"tikhub"}


def test_a_real_download_child_is_bound_to_its_root(supervisor, tmp_path) -> None:
    root = tmp_path / "assets" / "xhs-notes"
    root.mkdir(parents=True)
    staging = root / NOTE / ".staging"
    # A loopback CDN URL is refused before any request, so no network is used.
    execution = supervisor.run(
        "xhs_download_image",
        {"note_id": NOTE, "ordinal": 1, "url": "http://127.0.0.1/a.png", "staging_dir": str(staging)},
        write_roots=(root,),
    )
    assert not execution.ok
    assert execution.failure_category == "not_found"
    assert execution.write_boundary["ok"]
    assert not staging.exists()


def test_write_roots_are_required_exactly_for_writing_operations(supervisor, tmp_path) -> None:
    with pytest.raises(ValueError):
        supervisor.run("xhs_download_image", {"url": "https://cdn.example/a"})
    with pytest.raises(ValueError):
        supervisor.run("blog_fetch", {"url": "https://blog.example/"})
    with pytest.raises(ValueError):
        supervisor.run("xhs_list_page", {}, write_roots=(tmp_path,))
    with pytest.raises(ValueError):
        supervisor.run("checkpoint", write_roots=(tmp_path,))


def test_a_malformed_payload_is_refused_inside_the_task_allowlist(supervisor, tikhub) -> None:
    execution = supervisor.run("xhs_note_detail", {"tikhub_base": tikhub})
    assert execution.failure_category == "invalid_response"
    assert _FakeTikHub.seen == []


@pytest.mark.parametrize(
    ("operation", "expected"),
    [("xhs_note_detail", "outcome_unknown"), ("ingest_arxiv", "materialization_failed")],
)
def test_a_category_outside_the_task_allowlist_becomes_outcome_unknown(
    supervisor, tmp_path, operation, expected
) -> None:
    from cortex_platform.product.engine.survivors import SurvivorReport

    result = tmp_path / "result.json"
    result.write_text(json.dumps({
        "ok": False,
        "failure": {"category": "materialization_failed", "message": "x"},
    }))
    execution = supervisor._judge(
        request=request_for(operation, {}),
        result_path=result,
        timed_out=False,
        exit_code=1,
        stderr="",
        report=SurvivorReport(locks=(), processes=(), environment_scan="ok"),
    )
    assert execution.failure_category == expected
