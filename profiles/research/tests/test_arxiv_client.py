"""arxiv client — Atom XML parse tests."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "radar" / "arxiv_response_video_moe.xml"


def test_parse_basic_returns_entries():
    from cortex_research.arxiv_client import parse_atom
    xml = FIXTURE.read_text(encoding="utf-8")
    entries = parse_atom(xml)
    assert len(entries) == 2


def test_parse_extracts_arxiv_id():
    from cortex_research.arxiv_client import parse_atom
    entries = parse_atom(FIXTURE.read_text(encoding="utf-8"))
    assert entries[0]["arxiv_id"] == "2403.12345"
    assert entries[1]["arxiv_id"] == "2403.99999"


def test_parse_extracts_title_and_abstract():
    from cortex_research.arxiv_client import parse_atom
    entries = parse_atom(FIXTURE.read_text(encoding="utf-8"))
    assert "Mixture of Experts" in entries[0]["title"]
    assert "throughput gains" in entries[0]["abstract"]


def test_parse_extracts_authors_as_list():
    from cortex_research.arxiv_client import parse_atom
    entries = parse_atom(FIXTURE.read_text(encoding="utf-8"))
    assert entries[0]["authors"] == ["Alice Researcher", "Bob Engineer"]
    assert entries[1]["authors"] == ["Charlie Author"]


def test_parse_extracts_categories():
    from cortex_research.arxiv_client import parse_atom
    entries = parse_atom(FIXTURE.read_text(encoding="utf-8"))
    assert "cs.CV" in entries[0]["categories"]
    assert "cs.LG" in entries[0]["categories"]


def test_parse_extracts_published_at_iso():
    from cortex_research.arxiv_client import parse_atom
    entries = parse_atom(FIXTURE.read_text(encoding="utf-8"))
    assert entries[0]["published_at"].startswith("2026-05-25")


def test_parse_empty_feed_returns_empty():
    from cortex_research.arxiv_client import parse_atom
    empty = (
        '<?xml version="1.0"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<title>Empty</title></feed>'
    )
    assert parse_atom(empty) == []


def test_parse_handles_missing_abstract():
    from cortex_research.arxiv_client import parse_atom
    xml = (
        '<?xml version="1.0"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry>'
        '<id>http://arxiv.org/abs/9999.0001v1</id>'
        '<title>No abstract paper</title>'
        '<published>2026-05-25T00:00:00Z</published>'
        '<author><name>X</name></author>'
        '</entry></feed>'
    )
    entries = parse_atom(xml)
    assert entries[0]["abstract"] in ("", None)


def test_build_query_url_includes_query_and_sort():
    from cortex_research.arxiv_client import build_query_url
    url = build_query_url("video MoE", max_results=50)
    assert "search_query=" in url
    assert "sortBy=submittedDate" in url
    assert "max_results=50" in url


def test_build_query_url_with_categories():
    from cortex_research.arxiv_client import build_query_url
    url = build_query_url("video MoE", categories=["cs.CV", "cs.LG"])
    assert "cat:cs.CV" in url
    assert "cat:cs.LG" in url


def test_search_uses_skip_gate_when_env(monkeypatch):
    monkeypatch.setenv("CORTEX_RADAR_SKIP_NETWORK", "1")
    from cortex_research.arxiv_client import search
    result = search("any query")
    assert result == []


def test_search_retries_on_readtimeout(monkeypatch):
    """arxiv export API is flaky; a transient ReadTimeout must be retried, not
    drop the whole query (first live scan lost 6/7 queries to this)."""
    import httpx
    from cortex_research import arxiv_client
    calls = {"n": 0}
    xml = FIXTURE.read_text(encoding="utf-8")

    def fake_get(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, text=xml, request=httpx.Request("GET", url))

    monkeypatch.setattr(arxiv_client.httpx, "get", fake_get)
    monkeypatch.setattr(arxiv_client.time, "sleep", lambda *a, **k: None)
    entries = arxiv_client.search("video MoE", categories=["cs.CV"])
    assert calls["n"] == 2          # retried once after the timeout
    assert len(entries) == 2


def test_inter_query_sleep_is_jittered(monkeypatch):
    """The pre-query throttle must apply base + random jitter so a back-to-back
    scan does not hit arxiv in a fixed-cadence burst that trips 429."""
    import httpx
    from cortex_research import arxiv_client

    slept: list[float] = []
    monkeypatch.setattr(arxiv_client.time, "sleep", lambda s: slept.append(s))
    # Force a deterministic, non-zero jitter so the assertion is exact.
    monkeypatch.setattr(arxiv_client.random, "uniform", lambda lo, hi: 1.5)

    xml = FIXTURE.read_text(encoding="utf-8")
    monkeypatch.setattr(
        arxiv_client.httpx, "get",
        lambda url, **kw: httpx.Response(200, text=xml,
                                         request=httpx.Request("GET", url)),
    )
    arxiv_client.search("video MoE")

    assert slept, "expected the throttle to sleep before the request"
    # base (_RATE_LIMIT_SECONDS) + jitter (1.5), strictly greater than base.
    assert slept[0] == arxiv_client._RATE_LIMIT_SECONDS + 1.5
    assert slept[0] > arxiv_client._RATE_LIMIT_SECONDS


def test_backoff_sleep_is_jittered(monkeypatch):
    """Retry backoff must also be jittered (de-sync the retry storm)."""
    import httpx
    from cortex_research import arxiv_client

    slept: list[float] = []
    monkeypatch.setattr(arxiv_client.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(arxiv_client.random, "uniform", lambda lo, hi: 0.7)

    xml = FIXTURE.read_text(encoding="utf-8")
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, text=xml, request=httpx.Request("GET", url))

    monkeypatch.setattr(arxiv_client.httpx, "get", fake_get)
    arxiv_client.search("video MoE")

    # slept = [pre-query throttle, backoff after the first timeout]
    assert calls["n"] == 2
    assert len(slept) == 2
    assert slept[1] == arxiv_client._BACKOFF_SECONDS[0] + 0.7


def test_search_does_not_retry_on_400(monkeypatch):
    """Genuine 4xx (not 429) is a real client error — re-raise immediately."""
    import httpx
    from cortex_research import arxiv_client
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        return httpx.Response(400, request=httpx.Request("GET", url))

    monkeypatch.setattr(arxiv_client.httpx, "get", fake_get)
    monkeypatch.setattr(arxiv_client.time, "sleep", lambda *a, **k: None)
    with pytest.raises(httpx.HTTPStatusError):
        arxiv_client.search("video MoE")
    assert calls["n"] == 1          # no retry on 400


# --- exists_batch (one HTTP call per chunk; arxiv id_list batch) -------------------
def _feed_for(ids):
    """Build a minimal Atom feed with one <entry> per id (version-stripped)."""
    entries = "".join(
        f'<entry><id>http://arxiv.org/abs/{i}v1</id>'
        f'<title>Paper {i}</title></entry>'
        for i in ids
    )
    return (
        '<?xml version="1.0"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f'{entries}</feed>'
    )


def test_exists_batch_returns_only_existing_ids(monkeypatch):
    """Two of three queried ids resolve to entries → the batch returns just those two
    (version-stripped); the absent third id = does not exist."""
    import httpx
    from cortex_research import arxiv_client

    captured = {"url": None}

    def fake_get_with_retry(url, *, timeout, max_retries=3):
        captured["url"] = url
        # arxiv returns an <entry> only for the valid ids (2 of the 3 asked).
        feed = _feed_for(["2605.00001", "2605.00002"])
        return httpx.Response(200, text=feed, request=httpx.Request("GET", url))

    monkeypatch.setattr(arxiv_client, "_get_with_retry", fake_get_with_retry)

    # Pass a version on one id to prove normalization (input id_list still version-stripped).
    out = arxiv_client.exists_batch(["2605.00001v3", "2605.00002", "2605.99999"])
    assert out == {"2605.00001", "2605.00002"}
    # Single batched HTTP call carrying the version-stripped, comma-joined id_list.
    assert "id_list=" in captured["url"]
    assert "2605.00001" in captured["url"] and "2605.99999" in captured["url"]
    assert "v3" not in captured["url"]  # version stripped before the query


def test_exists_batch_skip_network_returns_empty(monkeypatch):
    """SKIP_NETWORK guard short-circuits to an empty set without any HTTP."""
    from cortex_research import arxiv_client
    monkeypatch.setenv("CORTEX_RADAR_SKIP_NETWORK", "1")

    def boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("no HTTP under SKIP_NETWORK")

    monkeypatch.setattr(arxiv_client, "_get_with_retry", boom)
    assert arxiv_client.exists_batch(["2605.00001", "2605.00002"]) == set()


def test_exists_batch_empty_input_no_http(monkeypatch):
    """Empty (or all-blank) input returns an empty set without touching the network."""
    from cortex_research import arxiv_client

    def boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("no HTTP for empty input")

    monkeypatch.setattr(arxiv_client, "_get_with_retry", boom)
    assert arxiv_client.exists_batch([]) == set()
    assert arxiv_client.exists_batch(["", "  ", None]) == set()


def test_exists_batch_chunks_and_dedups(monkeypatch):
    """Ids are deduped and split into chunks of `chunk`; one HTTP call per chunk."""
    import httpx
    from cortex_research import arxiv_client

    calls: list[str] = []

    def fake_get_with_retry(url, *, timeout, max_retries=3):
        calls.append(url)
        # Echo back every id present in this chunk's id_list as existing.
        qs = url.split("id_list=", 1)[1].split("&", 1)[0]
        ids = qs.split("%2C") if "%2C" in qs else qs.split(",")
        return httpx.Response(200, text=_feed_for(ids),
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(arxiv_client, "_get_with_retry", fake_get_with_retry)

    ids = [f"2605.{i:05d}" for i in range(5)]
    # duplicate the first id (with a version) — must collapse to one.
    out = arxiv_client.exists_batch(ids + ["2605.00000v9"], chunk=2)
    assert out == set(ids)
    assert len(calls) == 3  # 5 unique ids / chunk=2 → 3 chunks


def test_exists_batch_400_chunk_treated_as_all_absent(monkeypatch):
    """A wholly-invalid id_list can 400; that chunk is treated as all-absent (robust),
    never crashing the whole batch."""
    import httpx
    from cortex_research import arxiv_client

    def fake_get_with_retry(url, *, timeout, max_retries=3):
        resp = httpx.Response(400, request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("bad request", request=resp.request, response=resp)

    monkeypatch.setattr(arxiv_client, "_get_with_retry", fake_get_with_retry)
    assert arxiv_client.exists_batch(["garbage", "2605.00001"]) == set()
