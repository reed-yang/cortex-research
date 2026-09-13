"""Programmatic arxiv → full-text → corpus ingest.

Fills the missing PDF/HTML→full-text path: previously the only ways a paper
entered the paper_search corpus were (a) a human pre-writing a notes.md under
agent-readings, or (b) radar storing an abstract-only STUB. Neither let the
agent import a paper's FULL TEXT on demand. This module fetches an arxiv paper's
metadata + full body (arxiv HTML preferred, PDF via PyMuPDF as fallback), writes
the notes.md + full_text.md layout that index_papers.index_paper expects, and
indexes it (chunks + embeddings) into research.db.

Mostly pure functions. A no-LaTeXML-HTML paper falls back to the paper-ingestion
glm-ocr skill (shelled out via `uv run`, cloud OCR, no GPU) for LaTeX + figures,
then to a plain PyMuPDF text dump; under `strict=True` -- the mode the product
engine child uses -- both fallbacks are refused with a typed IngestError instead.

The supported entry point is `ingest_arxiv`. Generic HTML/PDF-URL ingestion is
not part of this surface: the product engine refuses every payload whose kind is
not `arxiv` (`cortex_platform/product/engine/port.py`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from typing import Optional

import httpx

_log = logging.getLogger(__name__)

from .arxiv_client import (
    ARXIV_UA,
    _BACKOFF_SECONDS,
    _arxiv_throttle,
    _get_with_retry,
    _sleep_with_jitter,
    _strip_version,
    parse_atom,
)
from .index_papers import agent_readings_papers, index_paper

# CORTEX (P4.2): the arxiv endpoints are bound rather than hardcoded so the
# product that owns this process also owns its egress. Defaults are the values
# that shipped, so an unset environment behaves exactly as before.
_META_URL = os.environ.get(
    "CORTEX_ARXIV_API_BASE", "http://export.arxiv.org/api/query"
) + "?id_list={id}"
_HTML_URL = os.environ.get("CORTEX_ARXIV_HTML_BASE", "https://arxiv.org/html") + "/{id}"
_PDF_URL = os.environ.get("CORTEX_ARXIV_PDF_BASE", "https://arxiv.org/pdf") + "/{id}"
_TIMEOUT = 30.0
_MAX_FULLTEXT_CHARS = 200_000  # cap — keep embedding cost + chunk count bounded
_HTML_RETRIES = 3              # retry the HTML body fetch on 429/503/transient


class IngestError(Exception):
    """Raised on an unrecoverable ingest failure (bad id, no content)."""


class TransientIngestError(Exception):
    """Raised when an ingest can't complete due to a TRANSIENT condition (a
    persistent arxiv 429 / rate-limit / timeout / connection error) rather than a
    permanent one (genuine 404, no-PDF). The caller MUST NOT bake a degraded
    plain-text body in this case — it should leave the paper un-ingested/unchanged
    and surface a 'retry later' status. This is the abort-not-bake fix: a 429
    storm once silently degraded a paper to a no-formula/no-image PyMuPDF dump."""


def _is_transient_http(exc: Exception) -> bool:
    """True iff `exc` is a TRANSIENT arxiv failure (retry-worthy) vs a permanent
    one. Transient: 429, 5xx, ReadTimeout/ConnectTimeout/ConnectError. Permanent:
    a genuine 4xx (e.g. 404 = no such PDF) — those are NOT transient and may
    legitimately fall through to the text-only path."""
    if isinstance(exc, (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError,
                        httpx.PoolTimeout, httpx.WriteTimeout)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code if exc.response is not None else 0
        return code == 429 or 500 <= code < 600
    return False


def _norm_id(id_or_url: str) -> str:
    aid = _strip_version(id_or_url.strip())
    if not re.match(r"^\d{4}\.\d{4,5}$", aid):
        raise IngestError(f"could not parse an arxiv id from {id_or_url!r}")
    return aid


def fetch_metadata(arxiv_id: str) -> dict:
    """Title / abstract / published date for an arxiv id (export API)."""
    resp = _get_with_retry(_META_URL.format(id=arxiv_id), timeout=_TIMEOUT)
    entries = parse_atom(resp.text)
    if not entries:
        raise IngestError(f"no arxiv metadata for {arxiv_id}")
    return entries[0]


def _html_to_markdown(html: str, base_url: str = "") -> tuple[str, list[tuple[str, str]]]:
    """Extract markdown + figure images from an arxiv (LaTeXML) HTML page.

    Returns ``(markdown, images)`` where ``images`` is a list of
    ``(absolute_url, "assets/figN.ext")`` to download. Fidelity vs the old
    get_text()-only extraction:
      * MATH: each <math> is replaced by its ``alttext`` LaTeX wrapped in
        ``$...$`` (inline) or ``$$ ... $$`` (display) BEFORE text extraction, so
        the clean LaTeX survives instead of the MathML glyph-soup get_text() emits.
      * FIGURES: each <figure> emits ``![caption](assets/figN.ext)`` and records
        its <img src> (resolved against base_url) for download; the figcaption is
        kept as the alt text. Standalone <img> outside a <figure> is not captured
        (arxiv wraps figures in <figure>); PDF-fallback ingests have no images.
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "lxml")
    # Drop noise.
    for sel in ("script", "style", "nav", "header", "footer"):
        for el in soup.find_all(sel):
            el.decompose()
    # Replace every <math> with its LaTeX source so get_text() picks up clean math.
    for m in soup.find_all("math"):
        alt = (m.get("alttext") or "").strip()
        if not alt:
            m.decompose()
            continue
        m.replace_with(f"$$ {alt} $$" if m.get("display") == "block" else f"${alt}$")

    # arxiv HTML wraps content in <article> / div.ltx_page_main; fall back to body.
    root = soup.find("article") or soup.find("div", class_="ltx_page_main") or soup.body or soup
    out: list[str] = []
    images: list[tuple[str, str]] = []
    seen_src: dict[str, str] = {}

    def _register_img(src: str) -> str | None:
        if not src:
            return None
        abs_url = urljoin(base_url, src)
        # arxiv quirk (live-confirmed): the page redirects to the UNVERSIONED
        # /html/<id>, but figure srcs are VERSION-prefixed (<id>vN/...), so the
        # urljoin above doubles the id -> /html/<id>/<id>vN/... which 404s.
        # De-double to /html/<id>vN/... (the dir that actually serves assets).
        # A bare src (x1.png) has no <id>vN/ segment and is left untouched.
        abs_url = re.sub(r"(/html/)(\d{4}\.\d{4,5})/(\2v\d+/)", r"\1\3", abs_url)
        if abs_url in seen_src:
            return seen_src[abs_url]
        ext = os.path.splitext(src.split("?")[0])[1] or ".png"
        rel = f"assets/fig{len(images) + 1}{ext}"
        seen_src[abs_url] = rel
        images.append((abs_url, rel))
        return rel

    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "figure"]):
        cls = " ".join(el.get("class") or [])
        if "ltx_bibliography" in cls or "ltx_authors" in cls or "ltx_page_logo" in cls:
            continue
        if el.name == "figure":
            cap_el = el.find("figcaption")
            caption = (re.sub(r"\s+", " ", cap_el.get_text(" ", strip=True)).strip()
                       if cap_el else "")
            # A figure often bundles MULTIPLE <img> panels (sub-figures) with one
            # shared figcaption — emit one ref per panel (caption on the first).
            rels = [r for r in (_register_img(i.get("src"))
                                for i in el.find_all("img")) if r]
            if rels:
                for i_rel, rel in enumerate(rels):
                    out.append(f"![{caption if i_rel == 0 else ''}]({rel})")
            elif caption:
                out.append(f"_{caption}_")
            continue
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if not text:
            continue
        name = el.name
        if name in ("h1", "h2", "h3", "h4"):
            level = {"h1": "#", "h2": "##", "h3": "###", "h4": "####"}[name]
            out.append(f"\n{level} {text}\n")
        elif name == "li":
            out.append(f"- {text}")
        else:
            out.append(text)
    return "\n".join(out).strip(), images


_DEFAULT_MAX_IMAGES = 200              # generous count cap; real papers rarely exceed it
_MAX_IMG_BYTES = 25 * 1024 * 1024      # skip pathologically large figures (25MB)
# Per-PHASE timeout: a bare float (read=connect=30) does NOT fire on a half-closed
# / CLOSE_WAIT CDN socket, which would freeze the detached worker and starve an
# ingest slot (the failure xhs_fetcher/tikhub_client were twice hardened against).
_IMG_TIMEOUT = httpx.Timeout(10.0, read=30.0)
_IMG_RETRIES = 4                       # retry figure downloads on 429/503/timeout
# Backoff for figures (many per paper) — distinct from arxiv-API's long 10/30/60s.
# Hardened to 4 steps after a bulk migration at concurrency=4 hit arxiv 429s that
# the old [3,8] (2-retry) budget couldn't ride out, silently dropping ~35 papers'
# figures (the dead-image bug).
_IMG_BACKOFF_SECONDS = [3.0, 8.0, 20.0, 45.0]


def _max_images() -> int:
    """Per-paper figure count cap; override via CORTEX_INGEST_MAX_IMAGES."""
    try:
        return max(1, int(os.environ.get("CORTEX_INGEST_MAX_IMAGES", _DEFAULT_MAX_IMAGES)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_IMAGES


def _get_with_backoff(url: str) -> "httpx.Response | None":
    """GET a figure with the arxiv UA + backoff on 429/503/timeout (arxiv throttles
    bursty figure fetches). Returns the response, or None if all retries are
    exhausted / a connection error persists."""
    for attempt in range(_IMG_RETRIES + 1):
        try:
            _arxiv_throttle()  # global min-interval gate (figures hit arxiv.org too)
            r = httpx.get(url, timeout=_IMG_TIMEOUT, follow_redirects=True,
                          headers=ARXIV_UA)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError):
            if attempt >= _IMG_RETRIES:
                return None
            _sleep_with_jitter(_IMG_BACKOFF_SECONDS[min(attempt, len(_IMG_BACKOFF_SECONDS) - 1)])
            continue
        if r.status_code in (429, 503) and attempt < _IMG_RETRIES:
            _sleep_with_jitter(_IMG_BACKOFF_SECONDS[min(attempt, len(_IMG_BACKOFF_SECONDS) - 1)])
            continue
        return r
    return None


def _download_images(images: list[tuple[str, str]], paper_dir: Path) -> int:
    """Best-effort, bounded materialization of (src, rel_path) figures into
    paper_dir. `src` is either an http(s) URL (downloaded with UA + per-phase
    timeout + 429/503 backoff + image content-type check) OR a local filesystem
    path (COPIED — the GLM-OCR path renders figures to local files in a tempdir).
    A 25MB size cap applies to both; a failed / oversized / non-image item is
    skipped (never fails the ingest). Returns the count saved."""
    saved = 0
    for src, rel in images[:_max_images()]:
        dest = paper_dir / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.startswith(("http://", "https://")):
                r = _get_with_backoff(src)
                if r is None or r.status_code != 200 or not r.content:
                    continue
                ctype = r.headers.get("content-type", "")
                if ctype and not ctype.startswith("image/"):
                    continue
                if len(r.content) > _MAX_IMG_BYTES:
                    continue
                dest.write_bytes(r.content)
                saved += 1
            else:
                # Local file (OCR-rendered asset) -> copy.
                sp = Path(src)
                if not sp.is_file() or sp.stat().st_size > _MAX_IMG_BYTES:
                    continue
                shutil.copyfile(sp, dest)
                saved += 1
        except Exception:  # noqa: BLE001 — a missing/slow figure never fails ingest
            continue
    return saved


# GLM-OCR fallback for papers with no arxiv LaTeXML HTML. Reuses the mature
# paper-ingestion skill (Zhipu layout_parsing) by shelling out to `uv run`; the
# skill renders + crops figures to local files and emits clean markdown + LaTeX.
# GLM_API_ID/GLM_API_KEY must be in THIS process's env: the migration CLI gets
# them from a secret-loaded shell; the gateway MCP path gets them only because
# they are listed in the ingest server's config env: block (Hermes filters the
# subprocess env to a safe-list + the explicit block). Overridable for tests.
_SKILL_INGEST = Path(os.environ.get(
    "CORTEX_PAPER_INGEST_SKILL",
    str(Path.home() / ".claude/skills/paper-ingestion/scripts/ingest_paper.py")))
_UV_BIN = os.environ.get("CORTEX_UV_BIN", "/opt/homebrew/bin/uv")
# The skill's URL mode (PDF <=20MB) sends the whole multi-page PDF in ONE request
# that GLM processes server-side; a ~40-page paper can take ~600s PER attempt and
# the skill retries up to 3x (a live EGOSTREAM run succeeded at 605s — JUST over a
# 600s cap). Budget for that (3x300s URL + render + overhead). Env-overridable.
_OCR_TIMEOUT_SECONDS = int(os.environ.get("CORTEX_OCR_TIMEOUT", "1000") or "1000")


def _strip_frontmatter_and_h1(md: str) -> str:
    """Drop a leading YAML frontmatter block (--- … ---) and the leading H1
    title line. `_write_paper_dir`/`heal_one` re-prepend `# {title}`, so the
    body must not carry its own frontmatter or duplicate title.

    Real GLM-OCR output wraps the title as ``<div align="center">\\n\\n# Title
    \\n\\n</div>`` — strip the wrapper div lines too, else the H1 survives and the
    paper ends up with two identical title lines (review-caught against the live
    corpus)."""
    s = md.lstrip("﻿")
    if s.startswith("---"):
        end = s.find("\n---", 3)
        if end != -1:
            nl = s.find("\n", end + 1)
            s = s[nl + 1:] if nl != -1 else ""
    lines = s.split("\n")
    # The title is the FIRST single-hash H1 (OCR sections are ##/###). It may sit
    # behind a leading title-page figure and inside a <div align="center"> wrapper
    # (live Helios: `![](image_001)\n\n<div align="center">\n\n# Title\n\n</div>`).
    # Remove that H1 line plus its immediately-wrapping <div>/</div>, keeping any
    # leading figure and the body intact.
    h1 = next((i for i, ln in enumerate(lines) if ln.startswith("# ")), None)
    if h1 is not None:
        drop = {h1}
        j = h1 - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j >= 0 and re.match(r"<div\b[^>]*>\s*$", lines[j].strip()):
            drop.add(j)
        k = h1 + 1
        while k < len(lines) and not lines[k].strip():
            k += 1
        if k < len(lines) and lines[k].strip() == "</div>":
            drop.add(k)
        lines = [ln for i, ln in enumerate(lines) if i not in drop]
    return "\n".join(lines).strip("\n").lstrip()


# OCR engine chain: deepseek-ocr (primary — wins formula fidelity, decoupled from
# the GLM/Anthropic quota chains) then glm-ocr (fallback — wins table structure).
# Order is env-overridable (set CORTEX_OCR_ENGINES=glm-ocr,deepseek-ocr to revert
# to glm-first). Each engine runs only if ITS creds are present; the first to
# yield a good body wins, both failing -> None (caller degrades).
_OCR_ENGINE_CHAIN = [
    e.strip() for e in os.environ.get(
        "CORTEX_OCR_ENGINES", "deepseek-ocr,glm-ocr").split(",") if e.strip()
]


def _env_cred(name: str) -> str:
    """A usable cred value, or "" if absent/blank/an UNRESOLVED `${VAR}` placeholder.

    Hermes interpolates an UNSET `${VAR}` in config.yaml to the LITERAL string
    `"${VAR}"` (not empty) — a truthy junk value that would make us spawn a
    guaranteed-401 OCR run. Treat such a placeholder as absent so the engine is
    cleanly skipped until the secret is actually provisioned (the C5 lesson)."""
    v = (os.environ.get(name) or "").strip()
    return "" if (not v or v.startswith("${")) else v


def _ocr_engine_creds_ok(engine: str) -> bool:
    """Whether THIS process's env carries the creds the engine's skill backend reads."""
    if engine == "glm-ocr":
        return bool(_env_cred("GLM_API_ID") and _env_cred("GLM_API_KEY"))
    if engine == "deepseek-ocr":
        return bool(_env_cred("NOVITA_API_KEY"))
    return True  # mineru/docling need no cloud creds


def _run_ocr_engine(source: str, out_dir: Path, engine: str, label: str,
                    timeout: float | None = None):
    """Shell out to the paper-ingestion skill with ONE engine. Returns (body,
    image_pairs) or None on ANY failure (nonzero exit, error/short output, bad
    JSON, timeout). image_pairs = [(local_abs_path, 'assets/<name>')]. `timeout`
    is this engine's slice of the chain's wall-clock budget (default: full)."""
    timeout = _OCR_TIMEOUT_SECONDS if timeout is None else timeout
    argv = [_UV_BIN, "run", str(_SKILL_INGEST), source,
            "--engine", engine, "--output-dir", str(out_dir),
            "--image-format", "png"]
    # CRITICAL: run with cwd = the skill ROOT so `uv run` resolves the skill's
    # OWN pyproject/uv.lock. Inheriting the cortex cwd makes uv walk up to the
    # cortex workspace pyproject, whose [train]/[strategy] numpy extras are
    # mutually unsatisfiable -> `uv run` exits 1 before the skill ever runs
    # (live-caught; mocked tests can't see this). --output-dir + the source path
    # are absolute, so cwd only affects project resolution.
    skill_root = _SKILL_INGEST.parent.parent
    # Cap each engine's page concurrency so a large PDF doesn't trip the provider
    # rate limit (GLM: live-proven 3 lets a 39-page paper through; Novita: 4).
    # Each var is read only by its own engine, so setting both is harmless.
    env = {**os.environ}
    env.setdefault("GLM_OCR_MAX_WORKERS", "3")
    env.setdefault("DEEPSEEK_OCR_MAX_WORKERS", "4")
    # Run in a new session so a timeout can SIGKILL the WHOLE tree (uv child +
    # the grandchild OCR interpreter mid HTTP call), not just `uv`.
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, cwd=str(skill_root), env=env,
                               start_new_session=True)
    except OSError as e:
        _log.warning("[ingest] %s: %s OCR skill failed to spawn (%s) — no OCR body",
                     label, engine, e)
        return None
    try:
        stdout, _stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        proc.wait()
        _log.warning("[ingest] %s: %s OCR skill timed out after %.0fs (SIGKILL'd) — "
                     "no OCR body", label, engine, timeout)
        return None
    if proc.returncode != 0:
        _log.warning(
            "[ingest] %s: %s OCR skill exited %s — no OCR body; stderr: %s",
            label, engine, proc.returncode, (_stderr or "").strip()[:300])
        return None
    # stdout is a single JSON object; be defensive and take the last JSON line.
    data = None
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            data = None
        break
    if not isinstance(data, dict) or data.get("status") != "success":
        status = data.get("status") if isinstance(data, dict) else "<non-json>"
        _log.warning(
            "[ingest] %s: %s OCR skill returned bad/unsuccessful JSON (status=%r) — "
            "no OCR body", label, engine, status)
        return None
    try:
        md_path = Path(data["markdown_path"])
        assets_dir = Path(data["paper_dir"]) / "assets"
        body = _strip_frontmatter_and_h1(md_path.read_text(encoding="utf-8"))
    except (KeyError, OSError) as e:
        _log.warning("[ingest] %s: %s OCR output unreadable (%s) — no OCR body",
                     label, engine, e)
        return None
    body = body.replace("](./assets/", "](assets/")
    body = body[:_MAX_FULLTEXT_CHARS]
    if len(body) < 500:
        _log.warning("[ingest] %s: %s OCR body too short (%s chars, empty result) — "
                     "no OCR body", label, engine, len(body))
        return None
    pairs: list[tuple[str, str]] = []
    if assets_dir.is_dir():
        for f in sorted(assets_dir.iterdir()):
            rel = f"assets/{f.name}"
            if f.is_file() and rel in body:      # only refs surviving truncation
                pairs.append((str(f), rel))
    return body, pairs


def _ocr_pdf(source: str, out_tmp: Path, *, label: str | None = None):
    """OCR an ARBITRARY PDF (a local file path OR a direct PDF URL) via the
    paper-ingestion skill engine CHAIN (deepseek-ocr -> glm-ocr by default) — the
    arxiv-decoupled core shared by the arxiv path (`_ocr_pdf_via_skill`) and the
    non-arxiv url path (`ingest_pdf_url`).

    `source` is either a local filesystem path to a PDF (preferred — forces the
    skill's robust per-page image mode) or an http(s) URL the skill fetches in its
    whole-PDF-URL mode. `label` is a short id for the log lines (an arxiv id, or
    the url) — purely cosmetic.

    Tries each engine in `_OCR_ENGINE_CHAIN` whose creds are present, each writing
    to its OWN `out_tmp/<engine>` subdir (the skill's duplicate-title check would
    collide otherwise). Returns the first good (body_markdown, image_pairs) where
    image_pairs = [(local_abs_path, 'assets/<name>')], or None when every engine
    fails / is skipped (missing skill/uv, no creds, nonzero exit, error/short
    output, bad JSON, timeout) so the caller can gracefully fall back (PyMuPDF text
    dump for arxiv; an IngestError for the url path, which has no text fallback)."""
    label = label or source
    if not _SKILL_INGEST.exists() or not Path(_UV_BIN).exists():
        _log.warning(
            "[ingest] %s: OCR unavailable — paper-ingestion skill (%s) or uv (%s) "
            "missing; no usable full text", label, _SKILL_INGEST, _UV_BIN)
        return None
    # Aggregate wall-clock budget for the WHOLE chain (not per-engine), so a slow
    # primary can't let the total balloon to N x _OCR_TIMEOUT_SECONDS — important
    # for the in-process ingest callers (grounding gate / kill-gate) bounded by the
    # idea MCP's 300s. Each engine gets the remaining budget.
    deadline = time.monotonic() + _OCR_TIMEOUT_SECONDS
    tried_any = False
    for engine in _OCR_ENGINE_CHAIN:
        if not _ocr_engine_creds_ok(engine):
            # Missing creds is the classic silent-degrade (exploration MCP gap).
            # Warn explicitly and skip rather than spawn a guaranteed-failing run.
            _log.warning(
                "[ingest] %s: skipping %s OCR — its creds are not in this process's "
                "env (deepseek needs NOVITA_API_KEY; glm needs GLM_API_ID/GLM_API_KEY)",
                label, engine)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            _log.warning("[ingest] %s: OCR chain wall-clock budget (%ss) exhausted "
                         "before %s — no OCR body", label, _OCR_TIMEOUT_SECONDS, engine)
            break
        tried_any = True
        res = _run_ocr_engine(source, out_tmp / engine, engine, label, timeout=remaining)
        if res is not None:
            if engine != _OCR_ENGINE_CHAIN[0]:
                _log.info("[ingest] %s: OCR succeeded via fallback engine %s", label, engine)
            return res
        _log.warning("[ingest] %s: %s OCR produced no body — trying next engine", label, engine)
    if not tried_any:
        _log.warning(
            "[ingest] %s: no OCR engine had usable creds (chain=%s) — no OCR body",
            label, _OCR_ENGINE_CHAIN)
    return None


def _ocr_pdf_via_skill(arxiv_id: str, out_tmp: Path):
    """OCR a no-HTML arxiv PDF via the glm-ocr skill (arxiv wrapper over _ocr_pdf).

    Pre-downloads the arxiv PDF to a LOCAL file (forcing the skill's robust
    per-page mode) with the C abort-not-bake guard, then calls the generalized
    `_ocr_pdf`. On a PERSISTENT arxiv 429/timeout during the pre-download, raises
    TransientIngestError so the caller leaves the paper un-ingested (vs baking a
    plain-text dump). A PERMANENT failure (genuine 404 / no PDF) keeps the legacy
    fall-through to the remote-URL skill mode. Returns _ocr_pdf's result, or None
    on any OCR failure so the caller can fall back to the PyMuPDF text dump."""
    if not _SKILL_INGEST.exists() or not Path(_UV_BIN).exists():
        _log.warning(
            "[ingest] %s: OCR unavailable — glm-ocr skill (%s) or uv (%s) missing; "
            "will degrade to plain PyMuPDF text", arxiv_id, _SKILL_INGEST, _UV_BIN)
        return None
    # Prefer a LOCAL pdf path over the arxiv URL: the skill sends a remote URL as
    # ONE whole-PDF request that routinely exceeds GLM's 300s/attempt timeout for
    # a 40+ page paper (live-caught: EGOSTREAM 43pg failed all 3 attempts at the
    # 5-min cap). A local file forces the skill's robust per-page image mode
    # (small per-page requests, GLM_OCR_MAX_WORKERS-capped). Fall back to the URL
    # if the pre-download fails.
    target = _PDF_URL.format(id=arxiv_id)
    try:
        # Wider 4-step backoff (3/8/20/45s) for the OCR PDF leg, mirroring the
        # figure leg hardened in R2. The OLD bare `except: pass` jumped to the slow
        # whole-PDF-URL OCR mode exactly when arxiv was 429-throttling — that mode
        # exceeds 300s under throttle -> None -> a plain-text bake. Ride out the
        # storm instead.
        r = _get_with_retry(target, timeout=_TIMEOUT, max_retries=_IMG_RETRIES,
                            backoff=_IMG_BACKOFF_SECONDS)
        pdf_path = out_tmp / f"{arxiv_id}.pdf"
        pdf_path.write_bytes(r.content)
        if pdf_path.stat().st_size > 1000:
            target = str(pdf_path)
    except Exception as e:  # noqa: BLE001
        # ABORT-NOT-BAKE: a PERSISTENT 429 / rate-limit / timeout means arxiv is
        # throttling us (commonly self-inflicted by a burst of exploration
        # ingests). Falling through to the URL mode would just time out and bake a
        # plain-text dump. Raise so the caller leaves the paper un-ingested and
        # retries later. A PERMANENT failure (genuine 404 / no PDF) is NOT
        # transient — keep the legacy fall-through to the remote-URL skill mode.
        if _is_transient_http(e):
            _log.warning(
                "[ingest] %s: OCR PDF pre-download hit a persistent transient "
                "failure (%s) — aborting (NOT baking plaintext); retry later",
                arxiv_id, type(e).__name__)
            raise TransientIngestError(
                f"transient arxiv failure fetching PDF for OCR: {arxiv_id}") from e
    return _ocr_pdf(target, out_tmp, label=arxiv_id)


def _pdf_to_text(pdf_bytes: bytes) -> str:
    """Extract text from a PDF via PyMuPDF (fitz). Lower structure than HTML
    but gets the full body when the optional dependency is installed.

    PyMuPDF is NOT part of the product's wheel closure: optional PDF-fallback
    dependencies are deferred to a later generation and a PDF-only paper refuses
    with a typed reason instead. The refusal has to be `IngestError`, the
    engine's own type -- a bare `ImportError` here reaches the product as a
    generic failure carrying an interpreter message, which reads as an accident
    rather than as the decision it is."""
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise IngestError(
            "PyMuPDF (fitz) is not installed, so the plain-PDF fallback is "
            "unavailable; this paper has no LaTeXML HTML and cannot be ingested"
        ) from e

    parts: list[str] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            parts.append(page.get_text("text"))
    return "\n".join(parts).strip()


def fetch_full_text(arxiv_id: str, *, ocr_tmp: Path | None = None,
                    strict: bool = False
                    ) -> tuple[str, str, list[tuple[str, str]]]:
    """Return (full_text_markdown, source, images). `source` is 'html', 'ocr', or
    'pdf'; `images` is a list of (src, "assets/figN.ext") where src is an absolute
    URL (html) or a local file path (ocr). Tries arxiv HTML first (clean LaTeX +
    figures); for a paper with NO LaTeXML HTML, falls back to glm-ocr (LaTeX +
    cropped figures) when `ocr_tmp` is given, else a plain PyMuPDF text dump.

    `ocr_tmp` is a caller-owned tempdir for the OCR skill's output (its local
    figure files are copied into the paper dir before the caller removes it).
    With ocr_tmp=None the OCR path is skipped entirely (the legacy 1-arg shape).

    `strict` refuses the DEGRADED fallback instead of baking it. With no LaTeXML
    HTML and no usable OCR, the legacy path writes a plain PyMuPDF dump -- no
    formulas, no figures -- and `_corpus_lookup` then short-circuits that arxiv
    id to `already_ingested` PERMANENTLY, so the degraded body can never be
    replaced by a better one. A strict caller raises here, BEFORE
    `_write_paper_dir` runs, and gets to decide that the input is not yet
    ingestible rather than inheriting a body it cannot undo."""
    # 1) arxiv HTML (only exists for many recent papers). Retry on rate-limit /
    #    transient with the arxiv UA (arxiv throttles UA-less bursts); a 404 / non
    #    -html / parse error means "no usable HTML" -> fall back to PDF (no retry).
    for attempt in range(_HTML_RETRIES + 1):
        try:
            _arxiv_throttle()  # global min-interval gate (HTML body hits arxiv.org)
            r = httpx.get(_HTML_URL.format(id=arxiv_id), timeout=_IMG_TIMEOUT,
                          follow_redirects=True, headers=ARXIV_UA)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError):
            if attempt >= _HTML_RETRIES:
                break
            _sleep_with_jitter(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
            continue
        if r.status_code in (429, 503) and attempt < _HTML_RETRIES:
            _sleep_with_jitter(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
            continue
        try:
            if r.status_code == 200 and "<html" in r.text[:2000].lower():
                # base_url = the FINAL url (after redirects, e.g. .../<id>v<n>) so
                # relative figure srcs resolve. MUST end with '/': the arxiv page
                # lives at /html/<id> (no trailing slash) but its figures are under
                # /html/<id>/x1.png — without the slash urljoin drops the id segment
                # and every image 404s (live-verified 2026-06-01).
                base = str(r.url)
                if not base.endswith("/"):
                    base += "/"
                md, images = _html_to_markdown(r.text, base_url=base)
                if len(md) > 1500:  # got a real body, not a stub/redirect page
                    md = md[:_MAX_FULLTEXT_CHARS]
                    # Drop images whose ![](assets/figN) ref was cut by truncation.
                    images = [(u, rel) for (u, rel) in images if rel in md]
                    return md, "html", images
        except Exception:  # noqa: BLE001 — any HTML parse error -> PDF fallback
            pass
        break  # 404 / non-html / short body -> PDF fallback
    # 2) No LaTeXML HTML. Prefer glm-ocr (LaTeX + cropped figures) when a tempdir
    #    is provided; it returns local figure files for _download_images to copy.
    if ocr_tmp is not None:
        ocr = _ocr_pdf_via_skill(arxiv_id, ocr_tmp)
        if ocr is not None:
            return ocr[0], "ocr", ocr[1]
        # OCR was attempted and failed (each cause logged inside _ocr_pdf_via_skill).
        # We are about to bake a plain-text, no-formula/no-image body — surface that
        # this ingest is DEGRADED so the operator can re-run once OCR/arxiv recovers.
        _log.warning(
            "[ingest] %s: DEGRADED ingest — no LaTeXML HTML and OCR unavailable; "
            "falling back to a plain PyMuPDF text dump (no formulas, no figures)",
            arxiv_id)
    if strict:
        # Refuse before anything is written. Refusing after the write is worse
        # than not refusing at all: the paper dir and the papers row already
        # exist, so the id looks ingested forever.
        raise IngestError(
            f"no LaTeXML HTML and OCR unavailable for {arxiv_id}; "
            "refusing the degraded plain-text fallback")
    # 3) PDF via PyMuPDF (text only — no figures, no LaTeX; last-resort fallback).
    r = _get_with_retry(_PDF_URL.format(id=arxiv_id), timeout=_TIMEOUT)
    text = _pdf_to_text(r.content)
    if len(text) < 500:
        raise IngestError(f"extracted full text too short for {arxiv_id} ({len(text)} chars)")
    return text[:_MAX_FULLTEXT_CHARS], "pdf", []


def _slug(title: str, n: int = 60) -> str:
    # Keep word chars, whitespace, hyphen, and '.' so version numbers like
    # "1.0"/"4.5" survive instead of collapsing to "10"/"45" in the paper_dir
    # PRIMARY KEY (e.g. "DreamX-World 1.0" -> "...DreamX-World_1.0..."). Other
    # punctuation (':', '/', '$', ...) is still dropped.
    s = re.sub(r"[^\w\s.-]", "", title).strip()
    s = re.sub(r"\s+", "_", s)
    # Keep the slug a safe single path component: collapse repeated dots so a
    # title can never yield a '.'/'..' component, and trim leading/trailing
    # separators (no hidden-dir leading dot, no dangling trailing dot) -- also
    # after the length cap, which may land on a separator.
    s = re.sub(r"\.{2,}", ".", s).strip("._-")
    return s[:n].strip("._-") or "paper"


def _unique_paper_dir(papers_root: Path, base: str, arxiv_id: str) -> Path:
    """Pick a unique dir for a NEW paper (dedup already ran upstream). If the
    {date}-{title} base collides with a DIFFERENT paper, disambiguate by
    appending the arxiv id."""
    cand = papers_root / base
    if not cand.exists():
        return cand
    return papers_root / f"{base}-{arxiv_id}"


def _write_paper_dir(arxiv_id: str, meta: dict, full_text: str,
                     images: list[tuple[str, str]] | None = None,
                     *, ingest_date_iso: str | None = None) -> tuple[Path, int]:
    """Write the reader-convention paper dir: {YYYYMMDD}-{title_slug}.

    Returns (paper_dir, images_saved). images_saved is the count materialized
    into assets/ (downloaded URLs or copied local OCR files); 0 when no images.

    Per the reader corpus convention (paper-ingestion skill), the {YYYYMMDD}
    prefix AND the notes `### ` date are the INGEST date (when the paper entered
    the corpus), NOT the publish date. The arxiv PUBLISH date is preserved
    separately in the papers.published_at column + labelled in notes.md.

    ingest_date_iso ('YYYY-MM-DD') overrides the ingest date — used by the legacy
    re-migration to preserve a paper's ORIGINAL first-ingest date; a fresh ingest
    passes None and gets today."""
    from datetime import datetime

    title = meta.get("title") or arxiv_id
    abstract = meta.get("abstract") or ""
    pub = (meta.get("published_at") or "")[:10]            # arxiv publish date
    ingest_iso = (ingest_date_iso or datetime.now().strftime("%Y-%m-%d"))[:10]
    yyyymmdd = ingest_iso.replace("-", "")[:8] or "00000000"
    papers_root = agent_readings_papers()
    paper_dir = _unique_paper_dir(papers_root, f"{yyyymmdd}-{_slug(title)}", arxiv_id)
    paper_dir.mkdir(parents=True, exist_ok=True)
    notes = (
        f"# Notes: {title}\n\n"
        f"### {ingest_iso}\n\n"          # ingest date (reader convention)
        f"## 详细摘要\n\n"
        f"{abstract}\n\n"
        f"_published: {pub or 'unknown'} · source: arxiv:{arxiv_id} "
        f"(agent-ingested full text)_\n"
    )
    (paper_dir / "notes.md").write_text(notes, encoding="utf-8")
    (paper_dir / "full_text.md").write_text(
        f"# {title}\n\n{full_text}\n", encoding="utf-8"
    )
    n_saved = _download_images(images, paper_dir) if images else 0
    return paper_dir, n_saved


def _corpus_paper_dir(arxiv_id: str) -> str:
    """Legacy dash-form paper_dir (arxiv-<id>). Retained only as the dedup
    fallback key for rows not yet backfilled — the ACTUAL on-disk dir of a fresh
    ingest is now full-title. Use _corpus_lookup() to get the real paper_dir."""
    return f"arxiv-{arxiv_id}"


def _corpus_lookup(arxiv_id: str) -> str | None:
    """Return the paper_dir of this arxiv id's FULL TEXT in the corpus, else None.

    Authoritative match is the papers.arxiv_id column (naming-convention
    independent); also matches the legacy dash-form paper_dir for rows not yet
    backfilled. The colon stub 'arxiv:<id>' from radar_index is NOT a full-text
    hit, so a stub-only signal still gets its real body fetched.

    Defensive on schema: a fresh/bare DB may not have the papers table or the
    arxiv_id column yet (it is added by db.apply_schema CREATE + the
    ensure_radar_schema ALTER). Degrade to the dash-only / not-in-corpus path
    rather than crash with "no such column".
    """
    from .db import connect

    conn = connect()
    try:
        has_papers = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='papers'"
        ).fetchone()
        if has_papers is None:
            return None
        cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
        if "arxiv_id" in cols:
            row = conn.execute(
                "SELECT paper_dir FROM papers WHERE arxiv_id=? OR paper_dir=? LIMIT 1",
                (arxiv_id, _corpus_paper_dir(arxiv_id)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT paper_dir FROM papers WHERE paper_dir=? LIMIT 1",
                (_corpus_paper_dir(arxiv_id),),
            ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _already_in_corpus(arxiv_id: str) -> bool:
    """True iff this id's FULL TEXT is already in the papers table (by arxiv_id
    column or legacy dash paper_dir). The colon stub 'arxiv:<id>' is not a hit."""
    return _corpus_lookup(arxiv_id) is not None


def normalize_title(title: str | None) -> str:
    """Canonical key for an exact title match (the title-gate + the backfill CLI
    share this ONE helper).

    casefold -> every non-alphanumeric char -> a space -> collapse whitespace runs
    -> strip. `str.isalnum()` keeps CJK ideographs and other non-ASCII letters
    (`你好` survives, only ASCII punctuation/whitespace is flattened), so a Chinese
    or accented title normalizes losslessly. A None/empty title -> "" (which never
    matches a real row — guard at the callsite)."""
    if not title:
        return ""
    folded = title.casefold()
    chars = [c if c.isalnum() else " " for c in folded]
    return " ".join("".join(chars).split())


# Marker `full_text_source` values a title-match self-heal returns (so the
# detached worker can render a distinct Telegram line). Not persisted.
_TITLE_MATCH_SOURCE = "corpus"


def _title_match_lookup(title: str) -> tuple[str, str | None] | None:
    """Find an existing papers row whose normalized title EXACTLY matches `title`.

    Returns (paper_dir, existing_arxiv_id) on a hit (existing_arxiv_id may be
    None/'' for the 231 legacy rows we are here to self-heal), or None on no hit.
    Normalizes in Python over a full `papers` scan (374 rows — no index needed,
    no schema column added per the spec). When several rows share the normalized
    title, the OLDEST by indexed_at wins (deterministic; mirrors the backfill
    collision rule) so re-ingest heals the canonical kept row.

    EXCLUDES the ~78 radar COLON STUBS — rows whose paper_dir is `arxiv:<id>`
    (also the `arxiv:<s2-hash>` form): abstract-only placeholders radar_index
    leaves behind, NOT a full-text body. Both forms start with `arxiv:`, so a
    `paper_dir LIKE 'arxiv:%'` filter drops them. Matching a stub here would make
    ingest_arxiv return already_ingested with the STUB dir (no full text fetched)
    and backfill the id onto the stub, so _corpus_lookup hits it forever and the
    radar→ingest main path silently dies (reproduced live on arxiv:2605.30201).
    The _corpus_lookup contract is explicit that a colon stub is NOT a full-text
    hit; this gate honors it.

    Defensive on schema exactly like _corpus_lookup: a bare/old DB may lack the
    papers table or its columns -> degrade to no-hit rather than crash."""
    norm = normalize_title(title)
    if not norm:
        return None
    from .db import connect

    conn = connect()
    try:
        has_papers = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='papers'"
        ).fetchone()
        if has_papers is None:
            return None
        cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
        has_aid = "arxiv_id" in cols
        order = "ORDER BY indexed_at ASC" if "indexed_at" in cols else ""
        sel_aid = "arxiv_id" if has_aid else "NULL AS arxiv_id"
        rows = conn.execute(
            f"SELECT paper_dir, title, {sel_aid} FROM papers "
            f"WHERE paper_dir NOT LIKE 'arxiv:%' {order}"
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        row_norm = normalize_title(r["title"])
        # Empty-title guard at the GATE (not just the early `if not norm` above):
        # an empty/punctuation-only row title normalizes to "" and must never be
        # treated as a match for an (also-empty) fetched title.
        if row_norm and row_norm == norm:
            return r["paper_dir"], (r["arxiv_id"] if has_aid else None)
    return None


def _backfill_paper_id(paper_dir: str, arxiv_id: str, meta: dict) -> bool:
    """Claim-guarded self-heal of a title-matched legacy row: backfill arxiv_id
    onto the kept paper_dir ONLY while it is still NULL/'' (so a concurrent writer
    that filled it first is never overwritten), and COALESCE-backfill published_at
    + source_url from the fetched metadata. Returns True iff this call claimed the
    arxiv_id (rowcount==1) — a lost race (rowcount==0) means another writer already
    set it, which is fine. Never raises on a bare/old DB lacking the columns."""
    from .db import connect

    pub = (meta.get("published_at") or "")[:10] or None
    # M2: NEVER fabricate a source_url. fetch_metadata/parse_atom has no
    # source_url key, so an `or f"arxiv:{arxiv_id}"` fallback would COALESCE an
    # invented 'arxiv:<id>' value into a column whose schema comment says "NULL
    # for arxiv/reader papers" (26 of 30 rows polluted in the live rehearsal).
    # Pass through whatever the metadata carries (None for an arxiv self-heal) and
    # let the COALESCE below leave the column untouched.
    source_url = meta.get("source_url") or None
    conn = connect()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
        if "arxiv_id" not in cols:
            return False
        # COALESCE so an already-set published_at / source_url is preserved; the
        # arxiv_id WHERE-guard is the claim (only an empty id is filled).
        sets = ["arxiv_id=?"]
        params: list = [arxiv_id]
        if "published_at" in cols:
            sets.append("published_at=COALESCE(NULLIF(published_at,''), ?)")
            params.append(pub)
        if "source_url" in cols:
            sets.append("source_url=COALESCE(NULLIF(source_url,''), ?)")
            params.append(source_url)
        params.append(paper_dir)
        with conn:
            cur = conn.execute(
                f"UPDATE papers SET {', '.join(sets)} "
                "WHERE paper_dir=? AND (arxiv_id IS NULL OR arxiv_id='')",
                params,
            )
        return cur.rowcount == 1
    finally:
        conn.close()


def _mark_signal_indexed(arxiv_id: str, paper_dir: str) -> int:
    """Close the radar-signal lifecycle for a freshly-ingested paper.

    The agent-driven ingest path historically never wrote radar_signals.
    processed_status, so triaged signals stayed 'pending' forever and the
    director wake-gate fired on a permanently non-empty queue. On a successful
    ingest we flip the matching signal to 'indexed' and record the full-text
    paper_dir. GUARDED by processed_status='pending' so we never resurrect a
    'skipped' decision; matched on the BARE arxiv_id (radar_signals.arxiv_id is
    stored without an 'arxiv-'/'arxiv:' prefix). A non-radar ingest (idea crux
    paper, direct CLI) simply matches no row -> rowcount 0, a safe no-op.
    Returns the number of signals updated.
    """
    from .db import connect
    from .radar_schema import ensure_radar_schema

    conn = connect()
    try:
        ensure_radar_schema(conn)
        with conn:
            cur = conn.execute(
                "UPDATE radar_signals SET processed_status='indexed', paper_dir=?"
                " WHERE arxiv_id=? AND processed_status='pending'",
                (paper_dir, arxiv_id),
            )
        return cur.rowcount
    finally:
        conn.close()


def ingest_arxiv(id_or_url: str, *, source: str = "agent",
                 ingest_date_iso: str | None = None,
                 strict: bool = False) -> dict:
    """Fetch + index an arxiv paper's full text into the corpus.

    Returns a structured result dict (never raises for the common failure modes
    so it is safe to expose as an MCP tool); raises IngestError only on a truly
    unusable input that the caller should surface verbatim.
    """
    arxiv_id = _norm_id(id_or_url)
    meta = fetch_metadata(arxiv_id)

    # R2: already in the corpus -> skip the expensive fetch+embed, but STILL
    # close the signal (R1). The director must stop seeing this as pending.
    # Use the REAL paper_dir from the DB (full-title for fresh ingests), not the
    # legacy dash form.
    existing_dir = _corpus_lookup(arxiv_id)
    if existing_dir is not None:
        paper_dir_name = existing_dir
        n_closed = _mark_signal_indexed(arxiv_id, paper_dir_name)
        return {
            "ok": True,
            "arxiv_id": arxiv_id,
            "title": meta.get("title"),
            "paper_dir": paper_dir_name,
            "full_text_source": "corpus",
            "full_text_chars": 0,
            "indexed": True,
            "already_ingested": True,
            "signals_closed": n_closed,
        }

    # TITLE-MATCH DEDUP GATE (Fix 1): the arxiv_id column missed (this id is not in
    # the corpus), but a legacy row with arxiv_id NULL/'' may already hold this
    # paper's FULL TEXT under a different naming (the 231 reader/radar/agent rows
    # the 2026-05-21 vault import left id-less). Re-ingesting them by id would
    # create a duplicate papers row + vault dir + double-indexed chunks. Exact
    # normalized-title match against the existing corpus, using the metadata we
    # ALREADY fetched (zero new network calls). On a hit: self-heal the kept row
    # (claim-guarded id backfill) and short-circuit — UNLESS the matched row
    # already carries a DIFFERENT non-empty id, in which case two genuinely
    # distinct papers share a normalized title (extremely rare) and we proceed
    # with a normal ingest rather than ever silently merge distinct ids.
    title_collision = False  # spec §2.4 marker — surfaced in the RESULT dict
    title_hit = _title_match_lookup(meta.get("title"))
    if title_hit is not None:
        kept_dir, kept_aid = title_hit
        if kept_aid and kept_aid != arxiv_id:
            # title_collision: a distinct paper already owns this normalized title
            # under a DIFFERENT id. Never merge — fall through to a normal ingest.
            title_collision = True
            _log.warning(
                "[ingest] %s: normalized-title match on %r but that row already has "
                "a different arxiv_id %r — title_collision, proceeding with ingest",
                arxiv_id, kept_dir, kept_aid)
        else:
            id_backfilled = _backfill_paper_id(kept_dir, arxiv_id, meta)
            n_closed = _mark_signal_indexed(arxiv_id, kept_dir)
            return {
                "ok": True,
                "arxiv_id": arxiv_id,
                "title": meta.get("title"),
                "paper_dir": kept_dir,
                "full_text_source": "corpus",
                "full_text_chars": 0,
                "indexed": True,
                "already_ingested": True,
                "title_match": True,
                "id_backfilled": bool(id_backfilled),
                "signals_closed": n_closed,
            }

    # A caller-owned tempdir holds the glm-ocr skill's output (its local figure
    # files are copied into the paper dir by _write_paper_dir before the dir is
    # removed). For an HTML/PDF ingest it is simply unused.
    with tempfile.TemporaryDirectory(prefix="cortex-ocr-") as td:
        try:
            full_text, ft_source, images = fetch_full_text(
                arxiv_id, ocr_tmp=Path(td), strict=strict)
        except TransientIngestError as e:
            # ABORT-NOT-BAKE: arxiv is throttling us. Do NOT write a degraded
            # plain-text body — leave the paper un-ingested and signal a retryable
            # transient so the caller (grounding gate / director / agent) treats it
            # as "retry later", NOT a successful plaintext ingest.
            return {
                "ok": False,
                "arxiv_id": arxiv_id,
                "title": meta.get("title"),
                "paper_dir": None,
                "full_text_source": None,
                "full_text_chars": 0,
                "indexed": False,
                "already_ingested": False,
                "transient": True,
                "error": str(e),
                "signals_closed": 0,
                "title_collision": title_collision,
            }
        paper_dir, images_saved = _write_paper_dir(
            arxiv_id, meta, full_text, images, ingest_date_iso=ingest_date_iso)
        indexed = index_paper(paper_dir / "notes.md", source=source, arxiv_id=arxiv_id,
                              published_at=(meta.get("published_at") or "")[:10] or None,
                              full_text_source=ft_source)
    images_expected = len(images)
    if images_saved < images_expected:  # operator observability — never silent
        sys.stderr.write(
            f"[ingest] {arxiv_id}: only {images_saved}/{images_expected} figures saved\n")
    # R1: a successful full-text ingest closes any matching pending radar signal.
    n_closed = _mark_signal_indexed(arxiv_id, paper_dir.name) if indexed else 0
    return {
        "ok": bool(indexed),
        "arxiv_id": arxiv_id,
        "title": meta.get("title"),
        "paper_dir": paper_dir.name,
        "full_text_source": ft_source,
        "full_text_chars": len(full_text),
        "images_saved": images_saved,
        "images_expected": images_expected,
        "indexed": bool(indexed),
        "already_ingested": False,
        "signals_closed": n_closed,
        "title_collision": title_collision,
    }
