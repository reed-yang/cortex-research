"""arxiv API client — Atom XML pull + parse. Daily cron only, no SDK."""
from __future__ import annotations

import os
import random
import re
import threading
import time
from typing import Optional
from urllib.parse import urlencode, quote
from xml.etree import ElementTree as ET

import httpx

# arxiv 301-redirects http→https; use https directly (and follow redirects
# defensively at the request site) so the export API doesn't bounce us.
_ARXIV_API = "https://export.arxiv.org/api/query"
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
_RATE_LIMIT_SECONDS = 3.0
# Random extra delay (0..N s) added to every inter-query sleep AND every
# backoff. A radar scan fires queries back-to-back from a single process; a
# fixed cadence makes them land in lockstep and trip arxiv's burst 429s. Jitter
# de-synchronizes the cadence so the export API sees a less bursty pattern.
_RATE_LIMIT_JITTER_SECONDS = 2.0
# arxiv's export API is flaky + aggressively rate-limits bursts (429) when a
# scan fires many queries. Retry transient failures (ReadTimeout/429/5xx) with
# a patient backoff so a throttle window doesn't drop a whole query's results.
# Per-run coverage is still best-effort — stragglers are picked up on the next
# daily cron (radar_signals dedups by source_ref, so nothing is lost over time).
_BACKOFF_SECONDS = [10.0, 30.0, 60.0]

# arxiv throttles UA-less / default-httpx clients harder; a descriptive UA is the
# single biggest lever against 429s. Shared by every arxiv fetch (metadata, PDF,
# HTML body, figure images).
ARXIV_UA = {"User-Agent": "cortex-research/1.0 (arxiv full-text ingest; contact: operator)"}


# --------------------------------------------------------------------------- #
# C4: global min-interval throttle between arxiv.org requests (metadata / html /
# pdf / figures). A burst of ~20 back-to-back exploration ingests was
# self-inflicting arxiv 429s (which then degraded a paper to a plain-text dump).
# A cheap monotonic-clock gate spaces EVERY arxiv fetch by at least
# CORTEX_ARXIV_MIN_INTERVAL seconds (default 1.0; set 0 to disable, e.g. tests).
# Lock + last-call timestamp = thread-safe-enough for the modest concurrency here
# (serial crons + low-concurrency heal/ingest). This is a FLOOR on spacing; the
# longer 429-backoff sleeps (_BACKOFF_SECONDS) still apply on top when throttled.
# --------------------------------------------------------------------------- #
_DEFAULT_ARXIV_MIN_INTERVAL = 1.0
_arxiv_throttle_lock = threading.Lock()
_last_arxiv_call = 0.0


def _arxiv_min_interval() -> float:
    """Min seconds between arxiv.org requests; CORTEX_ARXIV_MIN_INTERVAL override
    (0 disables). A bad value falls back to the 1.0s default."""
    raw = os.environ.get("CORTEX_ARXIV_MIN_INTERVAL")
    if raw is None:
        return _DEFAULT_ARXIV_MIN_INTERVAL
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_ARXIV_MIN_INTERVAL


def _arxiv_throttle() -> None:
    """Block until at least the min interval has elapsed since the last arxiv
    request, then stamp the call time. No-op when the interval is 0."""
    interval = _arxiv_min_interval()
    if interval <= 0.0:
        return
    global _last_arxiv_call
    with _arxiv_throttle_lock:
        now = time.monotonic()
        wait = _last_arxiv_call + interval - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_arxiv_call = now


def _sleep_with_jitter(base: float) -> None:
    """Sleep `base` seconds plus a small random jitter (0.._RATE_LIMIT_JITTER).

    Single-flight by construction (callers invoke it synchronously between
    requests); the jitter only breaks the fixed-cadence burst pattern that
    trips arxiv's 429 rate limiter.
    """
    time.sleep(base + random.uniform(0.0, _RATE_LIMIT_JITTER_SECONDS))


def build_query_url(
    query: str,
    *,
    categories: Optional[list[str]] = None,
    max_results: int = 50,
) -> str:
    search = f"all:{query}"
    if categories:
        cats = " OR ".join(f"cat:{c}" for c in categories)
        search = f"({search}) AND ({cats})"
    # Use quote with safe chars so colons/parens remain readable (arxiv needs them)
    encoded_search = quote(search, safe=":() ")
    params = urlencode({
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": str(max_results),
    })
    return f"{_ARXIV_API}?search_query={encoded_search}&{params}"


def _strip_version(arxiv_id_or_url: str) -> str:
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", arxiv_id_or_url)
    if m:
        return m.group(1)
    return arxiv_id_or_url


def parse_atom(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    entries = []
    for e in root.findall("a:entry", _ATOM_NS):
        id_elem = e.find("a:id", _ATOM_NS)
        title_elem = e.find("a:title", _ATOM_NS)
        summary_elem = e.find("a:summary", _ATOM_NS)
        published_elem = e.find("a:published", _ATOM_NS)
        if id_elem is None or title_elem is None:
            continue
        arxiv_id = _strip_version(id_elem.text or "")
        authors = [a.find("a:name", _ATOM_NS).text
                   for a in e.findall("a:author", _ATOM_NS)
                   if a.find("a:name", _ATOM_NS) is not None]
        categories = [c.get("term") for c in
                      e.findall("{http://arxiv.org/schemas/atom}category")]
        if not categories:
            categories = [c.get("term") for c in
                          e.findall("a:category", _ATOM_NS)]
        entries.append({
            "arxiv_id": arxiv_id,
            "title": (title_elem.text or "").strip(),
            "abstract": (summary_elem.text or "").strip() if summary_elem is not None else "",
            "authors": authors,
            "categories": categories,
            "published_at": (published_elem.text or "").strip() if published_elem is not None else "",
        })
    return entries


def _get_with_retry(url: str, *, timeout: float, max_retries: int = 3,
                    backoff: list[float] | None = None) -> httpx.Response:
    """GET with backoff on transient arxiv failures (ReadTimeout / 429 / 5xx).
    Non-transient 4xx re-raise immediately.

    `backoff` overrides the default per-attempt sleep schedule (_BACKOFF_SECONDS).
    The OCR PDF pre-download leg passes a wider 4-step 3/8/20/45s schedule
    (mirroring the figure leg) so a 429 storm is ridden out rather than dropped to
    the slow whole-PDF-URL OCR fallback."""
    sched = backoff or _BACKOFF_SECONDS
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            _arxiv_throttle()  # global min-interval gate (kill self-inflicted 429s)
            resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                             headers=ARXIV_UA)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else 0
            if code != 429 and not (500 <= code < 600):
                raise  # genuine client error — don't retry
            last_exc = e
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError) as e:
            last_exc = e
        if attempt < max_retries:
            _sleep_with_jitter(sched[min(attempt, len(sched) - 1)])
    assert last_exc is not None
    raise last_exc


def search(
    query: str,
    *,
    categories: Optional[list[str]] = None,
    max_results: int = 50,
    timeout: float = 30.0,
) -> list[dict]:
    if os.environ.get("CORTEX_RADAR_SKIP_NETWORK") == "1":
        return []
    url = build_query_url(query, categories=categories, max_results=max_results)
    _sleep_with_jitter(_RATE_LIMIT_SECONDS)
    resp = _get_with_retry(url, timeout=timeout)
    return parse_atom(resp.text)


def exists(arxiv_id: str, *, timeout: float = 15.0) -> bool:
    """True if an arxiv id resolves to a real paper (uses the id_list param, not search)."""
    if os.environ.get("CORTEX_RADAR_SKIP_NETWORK") == "1":
        return False
    url = f"{_ARXIV_API}?id_list={quote(arxiv_id, safe='')}&max_results=1"
    try:
        resp = _get_with_retry(url, timeout=timeout)
        return len(parse_atom(resp.text)) > 0
    except Exception:
        return False


def exists_batch(ids: list[str], *, timeout: float = 30.0, chunk: int = 50) -> set[str]:
    """Return the subset of arxiv ids (version-stripped) that exist, via ?id_list= batch
    queries (one HTTP call per `chunk` ids, each paced by _throttle). SKIP_NETWORK → empty set.

    Collapses what would otherwise be one paced exists() per id (3s each) into one batched,
    retried+throttled HTTP call per `chunk` ids — the key lever that makes the xhs backfill
    feasible (~18 per-note exists() calls → 1). arxiv's id_list returns an <entry> only for
    valid ids; an id absent from the response is treated as non-existent. A wholly-invalid
    id_list can 400 — that chunk is caught and treated as all-absent (robust, never crashes
    the whole batch)."""
    if os.environ.get("CORTEX_RADAR_SKIP_NETWORK") == "1":
        return set()
    # Normalize (version-strip), drop empties, dedup while preserving order.
    seen: set[str] = set()
    norm: list[str] = []
    for raw in ids:
        if not raw:
            continue
        nid = _strip_version(str(raw).strip())
        if not nid or nid in seen:
            continue
        seen.add(nid)
        norm.append(nid)
    if not norm:
        return set()

    found: set[str] = set()
    for start in range(0, len(norm), chunk):
        batch = norm[start:start + chunk]
        id_list = quote(",".join(batch), safe="")
        url = f"{_ARXIV_API}?id_list={id_list}&max_results={len(batch)}"
        try:
            resp = _get_with_retry(url, timeout=timeout)
        except Exception:
            # 4xx (e.g. a wholly-invalid id_list 400) or exhausted retries → treat this
            # chunk's ids as all-absent rather than aborting the whole batch.
            continue
        for entry in parse_atom(resp.text):
            aid = entry.get("arxiv_id") or ""
            if aid in seen:  # only count ids we actually asked for
                found.add(aid)
    return found


def search_title(title: str, *, max_results: int = 1, timeout: float = 30.0) -> list[dict]:
    """Field-qualified title search (search_query=ti:"...", NOT all:-wrapped)."""
    if os.environ.get("CORTEX_RADAR_SKIP_NETWORK") == "1":
        return []
    encoded = quote(f'ti:"{title}"', safe=':()" ')
    params = urlencode({"sortBy": "relevance", "sortOrder": "descending",
                        "max_results": str(max_results)})
    url = f"{_ARXIV_API}?search_query={encoded}&{params}"
    _sleep_with_jitter(_RATE_LIMIT_SECONDS)
    resp = _get_with_retry(url, timeout=timeout)
    return parse_atom(resp.text)
