"""Metadata recovery preserves paper identity and known failure semantics."""
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


def status_error(status):
    response = httpx.Response(status, request=httpx.Request(
        "GET", f"https://export.arxiv.org/api/query?id_list={PAPER_ID}"))
    return httpx.HTTPStatusError(str(status), request=response.request, response=response)


@pytest.mark.parametrize("error", [status_error(429), status_error(503), httpx.ReadTimeout("timed out")])
def test_transient_api_failure_reads_abs_metadata(monkeypatch, error):
    urls = []

    def get(url, **kwargs):
        urls.append(url)
        if len(urls) == 1:
            raise error
        return httpx.Response(200, text=ABS_HTML)

    monkeypatch.setattr(paper_ingest, "_get_with_retry", get)
    result = paper_ingest.fetch_metadata(PAPER_ID)
    assert urls == [paper_ingest._META_URL.format(id=PAPER_ID), paper_ingest._ABS_URL.format(id=PAPER_ID)]
    assert result == {
        "arxiv_id": PAPER_ID,
        "title": "A Fixture Paper",
        "abstract": "An abstract with enough context.",
        "authors": ["Ada Lovelace", "Research Collaboration"],
        "categories": ["cs.CV", "physics.optics", "quant-ph"],
        "published_at": "2026-01-07",
    }


@pytest.mark.parametrize("status", [400, 404])
def test_permanent_api_error_does_not_fetch_abs(monkeypatch, status):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        raise status_error(status)

    monkeypatch.setattr(paper_ingest, "_get_with_retry", get)
    with pytest.raises(paper_ingest.IngestError, match=f"arxiv metadata.*{PAPER_ID}.*{status}"):
        paper_ingest.fetch_metadata(PAPER_ID)
    assert len(calls) == 1


def test_failed_fallback_retains_both_endpoint_errors(monkeypatch):
    def get(url, **kwargs):
        if url == paper_ingest._META_URL.format(id=PAPER_ID):
            raise status_error(429)
        raise httpx.ReadTimeout("abs timed out")

    monkeypatch.setattr(paper_ingest, "_get_with_retry", get)
    with pytest.raises(paper_ingest.IngestError, match="export API.*429.*abs fallback.*ReadTimeout") as raised:
        paper_ingest.fetch_metadata(PAPER_ID)
    assert isinstance(raised.value.__cause__, httpx.HTTPStatusError)


@pytest.mark.parametrize("body", [
    ABS_HTML.replace(PAPER_ID, "2601.99999"),
    ABS_HTML.replace('name="citation_arxiv_id"', 'name="unrelated"'),
    ABS_HTML.replace('name="citation_title"', 'name="unrelated"'),
    ABS_HTML.replace('name="citation_date"', 'name="unrelated"'),
    ABS_HTML.replace('name="citation_abstract"', 'name="unrelated"'),
    "<html><title>Temporary service error</title></html>",
])
def test_incomplete_or_wrong_paper_fallback_is_a_known_failure(monkeypatch, body):
    def get(url, **kwargs):
        if url == paper_ingest._META_URL.format(id=PAPER_ID):
            raise status_error(429)
        return httpx.Response(200, text=body)

    monkeypatch.setattr(paper_ingest, "_get_with_retry", get)
    with pytest.raises(paper_ingest.IngestError, match="abs fallback"):
        paper_ingest.fetch_metadata(PAPER_ID)


def test_successful_api_does_not_fetch_abs(monkeypatch):
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>http://arxiv.org/abs/2601.00042v1</id><title>API Title</title>
    <summary>API abstract</summary></entry></feed>"""
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, text=atom)

    monkeypatch.setattr(paper_ingest, "_get_with_retry", get)
    result = paper_ingest.fetch_metadata(PAPER_ID)
    assert result["title"] == "API Title"
    assert len(calls) == 1


def test_metadata_failure_leaves_corpus_unwritten(research_db, monkeypatch):
    def get(url, **kwargs):
        raise httpx.ReadTimeout("offline")

    monkeypatch.setattr(paper_ingest, "_get_with_retry", get)
    with pytest.raises(paper_ingest.IngestError, match="arxiv metadata"):
        paper_ingest.ingest_arxiv(PAPER_ID, strict=True)
    import sqlite3
    with sqlite3.connect(research_db) as connection:
        assert connection.execute("SELECT count(*) FROM papers").fetchone()[0] == 0
