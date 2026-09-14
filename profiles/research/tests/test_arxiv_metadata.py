"""Direct metadata avoids the export API until the abs page cannot answer."""
import httpx
import pytest

from cortex_research import paper_ingest

PAPER_ID = "2601.00042"
ABS_HTML = """<html><head>
<meta name="citation_arxiv_id" content="2601.00042">
<meta name="citation_title" content="A Fixture Paper">
<meta name="citation_author" content="Lovelace, Ada">
<meta name="citation_author" content="Research Collaboration">
<meta name="citation_date" content="2026/01/07">
<meta name="citation_abstract" content="An abstract with enough context.">
</head><body><td class="subjects">Computer Vision (cs.CV);
Optics (physics.optics); Quantum Physics (quant-ph)</td></body></html>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/abs/2601.00042v1</id><title>API Title</title>
<summary>API abstract</summary></entry></feed>"""


def status_error(status, url):
    response = httpx.Response(status, request=httpx.Request("GET", url))
    return httpx.HTTPStatusError(str(status), request=response.request, response=response)


def test_valid_abs_metadata_never_calls_the_api(monkeypatch):
    urls = []

    def get(url, **kwargs):
        urls.append(url)
        assert url == paper_ingest._ABS_URL.format(id=PAPER_ID)
        return httpx.Response(200, text=ABS_HTML)

    monkeypatch.setattr(paper_ingest, "_get_with_retry", get)
    result = paper_ingest.fetch_metadata(PAPER_ID)
    assert len(urls) == 1
    assert result == {
        "arxiv_id": PAPER_ID,
        "title": "A Fixture Paper",
        "abstract": "An abstract with enough context.",
        "authors": ["Ada Lovelace", "Research Collaboration"],
        "categories": ["cs.CV", "physics.optics", "quant-ph"],
        "published_at": "2026-01-07",
    }


@pytest.mark.parametrize("status", [403, 404, 429, 503, None])
def test_abs_request_failure_uses_api_fallback(monkeypatch, status):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if url == paper_ingest._ABS_URL.format(id=PAPER_ID):
            if status is None:
                raise httpx.ReadTimeout("abs timed out")
            raise status_error(status, url)
        return httpx.Response(200, text=ATOM)

    monkeypatch.setattr(paper_ingest, "_get_with_retry", get)
    result = paper_ingest.fetch_metadata(PAPER_ID)
    assert result["title"] == "API Title"
    assert calls == [paper_ingest._ABS_URL.format(id=PAPER_ID), paper_ingest._META_URL.format(id=PAPER_ID)]


def test_failed_fallback_retains_both_endpoint_errors(monkeypatch):
    def get(url, **kwargs):
        if url == paper_ingest._ABS_URL.format(id=PAPER_ID):
            raise httpx.ReadTimeout("abs timed out")
        raise status_error(429, url)

    monkeypatch.setattr(paper_ingest, "_get_with_retry", get)
    with pytest.raises(paper_ingest.IngestError, match="abs page.*ReadTimeout.*export API fallback.*429") as raised:
        paper_ingest.fetch_metadata(PAPER_ID)
    assert isinstance(raised.value.__cause__, httpx.HTTPStatusError)


@pytest.mark.parametrize("body", [
    ABS_HTML.replace(PAPER_ID, "2601.99999"),
    ABS_HTML.replace('name="citation_arxiv_id"', 'name="unrelated"'),
    ABS_HTML.replace('name="citation_title"', 'name="unrelated"'),
    ABS_HTML.replace('name="citation_date"', 'name="unrelated"'),
    ABS_HTML.replace('name="citation_abstract"', 'name="unrelated"'),
    ABS_HTML.replace('2026/01/07', 'not-a-date'),
    "<html><title>Temporary service error</title></html>",
])
def test_invalid_abs_metadata_recovers_from_api(monkeypatch, body):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, text=body if "/abs/" in url else ATOM)

    monkeypatch.setattr(paper_ingest, "_get_with_retry", get)
    result = paper_ingest.fetch_metadata(PAPER_ID)
    assert result["arxiv_id"] == PAPER_ID
    assert result["title"] == "API Title"
    assert len(calls) == 2


@pytest.mark.parametrize("body", [
    "<html>upstream error</html>",
    "<feed",
    ATOM.replace(PAPER_ID, "2601.99999"),
    ATOM.replace('API Title', ''),
])
def test_invalid_api_fallback_is_a_known_metadata_failure(monkeypatch, body):
    def get(url, **kwargs):
        if "/abs/" in url:
            raise httpx.ReadTimeout("abs timed out")
        return httpx.Response(200, text=body)

    monkeypatch.setattr(paper_ingest, "_get_with_retry", get)
    with pytest.raises(paper_ingest.IngestError, match="abs page.*export API fallback"):
        paper_ingest.fetch_metadata(PAPER_ID)


def test_metadata_failure_leaves_corpus_unwritten(research_db, monkeypatch):
    def get(url, **kwargs):
        raise httpx.ReadTimeout("offline")

    monkeypatch.setattr(paper_ingest, "_get_with_retry", get)
    with pytest.raises(paper_ingest.IngestError, match="arxiv metadata"):
        paper_ingest.ingest_arxiv(PAPER_ID, strict=True)
    import sqlite3
    with sqlite3.connect(research_db) as connection:
        assert connection.execute("SELECT count(*) FROM papers").fetchone()[0] == 0
