"""Library reader routes over the real reader, store and corpus files."""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
from urllib.parse import quote, urlencode

import pytest

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.daemon import create_server
from cortex_platform.tests.product.sources.test_adoption_reader import (  # noqa: F401
    corpus,
    database,
)
from cortex_platform.tests.product.sources.test_knowledge_document import (
    CHINESE,
    ENGLISH,
    SIGNATURES,
    png,
    source_id,
    write_asset,
)
from cortex_platform.tests.product.sources.test_knowledge_reader import knowledge  # noqa: F401

from .test_sources import TOKEN, _headers

ASSET_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)


@pytest.fixture
def reader_api(knowledge):  # noqa: F811
    store, root, _, _ = knowledge
    api = ControlAPI(store, access_token=TOKEN, allowed_origins=frozenset({"https://cortex.test"}))
    return api, store, root


def get(api, target, **kwargs):
    return api.handle(method="GET", target=target, headers=kwargs.pop("headers", _headers()), **kwargs)


def asset_target(sid: str, path: str) -> str:
    return f"/api/v1/sources/{sid}/asset?" + urlencode({"path": path}, quote_via=quote)


@pytest.mark.parametrize("kind", ["notes", "full_text", "grounding"])
def test_document_route_returns_whole_projected_document(reader_api, kind):
    api, store, root = reader_api
    sid = source_id(store, ENGLISH)
    raw = ("# Title\n" + "word " * 8_000 + "\n/home/example/private.md\n" + kind).encode()
    (root / ENGLISH / f"{kind}.md").write_bytes(raw)
    response = get(api, f"/api/v1/sources/{sid}/document?kind={kind}")
    assert response.status == 200, response.payload
    assert response.headers == (("Cache-Control", "no-store"),)
    assert response.body is None and response.content_type == "application/json"
    assert set(response.payload) == {
        "source_id", "canonical_id", "kind", "text", "content_sha256",
        "retained_bytes", "redacted",
    }
    assert response.payload["text"] == raw.decode().replace("/home/example/private.md", "[redacted]")
    assert response.payload["redacted"] is True
    assert response.payload["retained_bytes"] == len(raw)
    assert response.payload["content_sha256"] == hashlib.sha256(raw).hexdigest()
    paged = get(api, f"/api/v1/sources/{sid}/content?kind={kind}")
    assert paged.payload["content_sha256"] == response.payload["content_sha256"]


def test_document_route_defaults_to_notes_without_redaction(reader_api):
    api, store, _ = reader_api
    response = get(api, f"/api/v1/sources/{source_id(store, ENGLISH)}/document")
    assert response.status == 200
    assert response.payload["kind"] == "notes"
    assert response.payload["text"] == "notes for Speculative Decoding"
    assert response.payload["redacted"] is False


@pytest.mark.parametrize("query", [
    "kind=pdf", "kind=", "kind=../notes", "kind=notes&kind=grounding", "cursor=abc",
    "limit=10", "kind=notes&path=/tmp/private",
])
def test_document_route_query_errors(reader_api, query):
    api, store, _ = reader_api
    response = get(api, f"/api/v1/sources/{source_id(store, ENGLISH)}/document?{query}")
    assert response.status == 400
    assert response.payload["category"] == "source_query_invalid"
    assert "/tmp/" not in json.dumps(response.payload)


def test_document_route_two_mebibyte_boundary(reader_api):
    api, store, root = reader_api
    sid = source_id(store, ENGLISH)
    path = root / ENGLISH / "full_text.md"
    path.write_bytes(b"z" * 2_097_152)
    response = get(api, f"/api/v1/sources/{sid}/document?kind=full_text")
    assert response.status == 200
    assert response.payload["retained_bytes"] == 2_097_152
    path.write_bytes(b"z" * 2_097_153)
    response = get(api, f"/api/v1/sources/{sid}/document?kind=full_text")
    assert response.status == 413
    assert response.payload["category"] == "source_document_too_large"
    assert response.content_type == "application/problem+json"
    assert str(root) not in json.dumps(response.payload)


def test_document_route_refuses_unknown_and_non_paper_sources(reader_api):
    import sqlite3

    api, store, root = reader_api
    sid = source_id(store, ENGLISH)
    # As on the paged route, an unknown id is not distinguished from an
    # unadopted one; an id outside the public grammar matches no route.
    for target, status, category in (
        ("missing", 409, "source_content_unavailable"), ("x" * 201, 404, "not_found"),
    ):
        response = get(api, f"/api/v1/sources/{target}/document")
        assert (response.status, response.payload["category"]) == (status, category)
    connection = sqlite3.connect(store.path)
    try:
        connection.execute("UPDATE sources SET source_kind = 'blog' WHERE id = ?", (sid,))
        connection.commit()
    finally:
        connection.close()
    response = get(api, f"/api/v1/sources/{sid}/document")
    assert response.status == 409
    assert response.payload["category"] == "source_content_unavailable"
    assert str(root) not in json.dumps(response.payload)


@pytest.mark.parametrize("route", ["document", "asset?path=assets/fig1.png"])
@pytest.mark.parametrize("boundary", ["token", "origin", "host"])
def test_reader_routes_authenticate_before_reading(reader_api, monkeypatch, route, boundary):
    from cortex_platform.product.sources import reader as module

    api, store, root = reader_api
    write_asset(root, "fig1.png", png())

    def forbidden(*args, **kwargs):
        pytest.fail("an unauthenticated request must not reach the reader")

    monkeypatch.setattr(module.SourceKnowledgeReader, "_registered", forbidden)
    kwargs = (
        {"headers": {}} if boundary == "token"
        else {"headers": {**_headers(), "Origin": "https://evil.test"}} if boundary == "origin"
        else {"client_host": "192.0.2.1"}
    )
    response = get(api, f"/api/v1/sources/{source_id(store, ENGLISH)}/{route}", **kwargs)
    assert response.status == 403
    assert response.body is None


@pytest.mark.parametrize("path", [
    "assets/fig1.png", "./assets/fig1.png", f"papers/{ENGLISH}/assets/fig1.png",
])
def test_asset_route_returns_raw_image_with_exact_headers(reader_api, path):
    api, store, root = reader_api
    image = png()
    write_asset(root, "fig1.png", image)
    response = get(api, asset_target(source_id(store, ENGLISH), path))
    assert response.status == 200
    assert response.body == image
    assert response.content_type == "image/png"
    assert response.headers == ASSET_HEADERS


@pytest.mark.parametrize(("data", "media_type"), SIGNATURES,
                         ids=["png", "jpeg", "gif87a", "gif89a", "webp"])
def test_asset_route_media_type_follows_signature(reader_api, data, media_type):
    api, store, root = reader_api
    write_asset(root, "figure.bin", data)
    response = get(api, asset_target(source_id(store, ENGLISH), "assets/figure.bin"))
    assert (response.status, response.content_type, response.body) == (200, media_type, data)


@pytest.mark.parametrize("query", [
    "", "path=", "path=assets/fig1.png&path=assets/fig1.png", "path=assets/fig1.png&kind=notes",
    "name=assets/fig1.png", "path=%2Fassets%2Ffig1.png", "path=assets%2F..%2Fnotes.md",
    "path=assets%252Ffig1.png", "path=assets%252e%252e%252Fnotes.md", "path=..%2Fnotes.md",
    "path=assets%5Cfig1.png", "path=assets%2Ffig1.png%00", "path=assets/%0Afig1.png",
    "path=https://example.test/assets/fig1.png", "path=//example.test/assets/fig1.png",
    "path=data:image/png;base64,iVBORw0KGgo=", "path=file:///tmp/private/fig1.png",
    "path=" + "assets/" + "a" * 506, "path=notes.md", "path=" + quote(f"./papers/{ENGLISH}/assets/fig1.png"),
])
def test_asset_route_rejects_paths_outside_the_grammar(reader_api, query):
    api, store, root = reader_api
    write_asset(root, "fig1.png", png())
    response = get(api, f"/api/v1/sources/{source_id(store, ENGLISH)}/asset?{query}")
    assert response.status == 400
    assert response.payload["category"] == "source_asset_invalid"
    assert response.content_type == "application/problem+json" and response.body is None
    assert "/tmp/" not in json.dumps(response.payload)
    assert str(root) not in json.dumps(response.payload)


def test_asset_route_problem_categories_are_path_free(reader_api, tmp_path):
    api, store, root = reader_api
    sid = source_id(store, ENGLISH)
    write_asset(root, "fig1.png", png())
    write_asset(root, "vector.png", b'<svg xmlns="http://www.w3.org/2000/svg"/>')
    write_asset(root, "large.png", png() + b"\x00" * (8 * 1024 * 1024))
    write_asset(root, "other.png", png(), paper_dir=CHINESE)
    (root / ENGLISH / "assets" / "folder.png").mkdir()
    cases = [
        (sid, "assets/missing.png", 404, "source_asset_unavailable"),
        (sid, "assets/folder.png", 404, "source_asset_unavailable"),
        (sid, f"papers/{CHINESE}/assets/other.png", 404, "source_asset_unavailable"),
        ("missing", "assets/fig1.png", 404, "source_asset_unavailable"),
        (sid, "assets/vector.png", 415, "source_asset_unsupported"),
        (sid, "assets/large.png", 413, "source_asset_too_large"),
    ]
    for target, path, status, category in cases:
        response = get(api, asset_target(target, path))
        assert (response.status, response.payload["category"]) == (status, category), path
        assert response.content_type == "application/problem+json" and response.body is None
        assert str(tmp_path) not in json.dumps(response.payload)
        assert path not in json.dumps(response.payload)


def test_daemon_sends_binary_assets_verbatim_and_json_unchanged(reader_api):
    api, store, root = reader_api
    sid = source_id(store, ENGLISH)
    image = png()
    write_asset(root, "fig1.png", image)
    stop = threading.Event()
    server = create_server(host="127.0.0.1", port=0, instance_id="reader-test",
                           control_token=TOKEN, stop_requested=stop, control_api=api)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    connection = http.client.HTTPConnection("127.0.0.1", int(server.server_address[1]), timeout=2)

    def fetch(target):
        connection.request("GET", target, headers=_headers())
        response = connection.getresponse()
        return response, response.read()

    try:
        response, body = fetch(asset_target(sid, "assets/fig1.png"))
        assert response.status == 200
        assert body == image
        headers = [(name, value) for name, value in response.getheaders()
                   if name not in {"Server", "Date"}]
        assert headers == [
            ("Content-Type", "image/png"),
            ("Content-Length", str(len(image))),
            ("Cache-Control", "no-store"),
            *ASSET_HEADERS,
        ]
        response, body = fetch(asset_target(sid, "assets/missing.png"))
        assert response.status == 404
        assert response.getheader("Content-Type") == "application/problem+json"
        assert json.loads(body)["category"] == "source_asset_unavailable"
        response, body = fetch(f"/api/v1/sources/{sid}/document")
        assert response.status == 200
        assert response.getheader("Content-Type") == "application/json"
        assert int(response.getheader("Content-Length")) == len(body)
        assert json.loads(body)["text"] == "notes for Speculative Decoding"
        assert body == json.dumps(api.handle(
            method="GET", target=f"/api/v1/sources/{sid}/document", headers=_headers(),
        ).payload, sort_keys=True, separators=(",", ":")).encode()
    finally:
        stop.set()
        connection.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
