"""Public research routes retain authentication, identity and document boundaries."""

import json

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.control import ControlStore
from cortex_platform.product.research.catalog import ResearchCatalog
from cortex_platform.product.research.documents import ResearchDocumentAdopter
from cortex_platform.tests.product.research.test_catalog import _build


TOKEN = "x" * 48


def request(api, path, method="GET", body=None, key=None, authenticated=True):
    headers = {"X-Cortex-Control-Token": TOKEN} if authenticated else {}
    if key:
        headers["Idempotency-Key"] = key
    return api.handle(method=method, target=path, headers=headers,
                      body=json.dumps(body).encode() if body is not None else b"")


def test_catalog_unavailable_does_not_look_like_empty_or_leak_path(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    api = ControlAPI(store, access_token=TOKEN)
    assert request(api, "/api/v1/research-items", authenticated=False).status == 403
    response = request(api, "/api/v1/research-items")
    assert response.status == 409
    assert str(tmp_path) not in json.dumps(response.payload)


def test_real_catalog_document_and_thread_routes(tmp_path):
    # The catalog lane's real-DDL fixture supplies origin identity and status.
    database = _build(tmp_path / "research.db")
    catalog = ResearchCatalog(database)
    store = ControlStore(tmp_path / "product" / "control.db")
    store.initialize()
    api = ControlAPI(store, access_token=TOKEN, research_catalog=catalog)
    page = request(api, "/api/v1/research-items?limit=2&offset=0")
    assert page.status == 200
    assert len(page.payload["items"]) == 2
    item = catalog.list_items(kind="project")["items"][0]
    detail = request(api, f"/api/v1/research-items/{item['id']}")
    assert detail.payload["unavailable_reason"] == "documents_not_adopted"
    assert not detail.payload["continuation_ready"]
    root = tmp_path / "projects"
    root.mkdir()
    document = root / "project.md"
    document.write_text("# Project dossier\n\n$x^2$\nPrivate: /Users/fixture/private\n")
    adopter = ResearchDocumentAdopter(store, catalog)
    preview = adopter.preview({"project": root}, item_ids={item["id"]}, document_map={
        item["id"]: [{"kind": "project", "registered_path": str(document)}],
    })
    adopter.apply(preview, destination=tmp_path / "adopted")
    detail = request(api, f"/api/v1/research-items/{item['id']}").payload
    assert detail["continuation_ready"]
    assert "relative_path" not in json.dumps(detail)
    version_id = detail["documents"][0]["id"]
    content = request(api, f"/api/v1/research-documents/{version_id}/content")
    assert content.status == 200
    assert "[redacted]" in content.payload["content"]
    assert "/Users/fixture" not in content.payload["content"]
    assert content.payload["byte_length"] == len(content.payload["content"].encode())
    assert content.payload["redacted"]
    ws = store.create_workspace(title="Work", actor_id="local", idempotency_key="api-research-workspace").value
    command = {"workspace_id": ws["id"], "expected_revision": ws["revision"]}
    opened = request(api, f"/api/v1/research-items/{item['id']}/thread", "POST", command, "api-research-thread")
    assert opened.status == 201
    again = request(api, f"/api/v1/research-items/{item['id']}/thread", "POST", command, "api-research-thread")
    assert again.payload == opened.payload
    assert store.get_research_thread_item(opened.payload["id"])["id"] == item["id"]
    assert request(api, "/api/v1/research-items?file=/etc/passwd").status == 400
