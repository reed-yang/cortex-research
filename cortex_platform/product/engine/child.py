"""The per-effect engine child: one operation, one reply, then exit.

Everything this module does happens in a process cortexd will not survive
without: the engine detaches grandchildren that inherit the whole environment
(F7), `db.connect()` flips the copied database into WAL and mkdirs on open
(F5), and the OCR path dlopens and shells out. None of that may run in the
process that owns `control.db`.

⟦AMD-5⟧ fixes the order in which this process may claim success: close every
connection it opened on `research.db`, `PRAGMA wal_checkpoint(TRUNCATE)`, pass
the write-boundary assertion, pass the no-descendant assertion. A child that
has not checkpointed has not finished -- S1's reader refuses a non-empty `-wal`
and it is right to.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping

from .protocol import PROVIDER_OPERATIONS, EffectRequest, write_result
from . import digests, survivors

# Audit events that mean "a path is about to change". `open` is filtered by its
# mode and flags -- the same event fires for every read.
_WRITE_EVENTS = {
    "os.mkdir",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.link",
    "os.symlink",
    "os.truncate",
    "shutil.copyfile",
    "shutil.copymode",
    "shutil.copystat",
    "shutil.move",
}
_WRITE_MODES = frozenset("waxt+")


class _WriteAudit:
    """D3 layer 3: record every path this process changed.

    ⟦AMD-10⟧ names this honestly -- it is detection after the fact, not
    prevention before the write. Bindings catch the known cases and `HOME` the
    forgotten ones; this catches the ones no layer anticipated, and it catches
    them by reporting a write that already happened.
    """

    def __init__(self) -> None:
        self.paths: set[str] = set()
        self._enabled = False

    def install(self) -> None:
        self._enabled = True
        sys.addaudithook(self._hook)

    def _record(self, value: object) -> None:
        if isinstance(value, (str, bytes, os.PathLike)):
            try:
                self.paths.add(os.fspath(os.fsdecode(value)))
            except (TypeError, ValueError):
                pass

    def _hook(self, event: str, arguments: tuple) -> None:
        if not self._enabled:
            return
        try:
            if event == "open":
                path, mode, flags = (arguments + (None, None, None))[:3]
                writing = False
                if isinstance(mode, str):
                    writing = bool(_WRITE_MODES & set(mode))
                if isinstance(flags, int):
                    writing = writing or bool(
                        flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND)
                    )
                if writing:
                    self._record(path)
                return
            if event in _WRITE_EVENTS:
                for argument in arguments:
                    self._record(argument)
        except Exception:  # pragma: no cover - an audit hook may never raise
            return


def assess_write_boundary(
    paths: set[str], write_roots: tuple[Path, ...]
) -> tuple[str, ...]:
    """Every recorded write that landed outside the bound roots."""

    resolved = tuple(Path(root).resolve(strict=False) for root in write_roots)
    violations: list[str] = []
    for raw in sorted(paths):
        if not raw or not raw.startswith("/"):
            # A relative path is resolved against the child's cwd, which the
            # supervisor pins inside a bound root.
            continue
        candidate = Path(raw).resolve(strict=False)
        if any(candidate == root or candidate.is_relative_to(root) for root in resolved):
            continue
        violations.append(str(candidate))
    return tuple(violations)


def checkpoint_research_db(database: Path) -> bool:
    """Truncate the write-ahead log so S1's reader can open the copy.

    Not an optimisation: `read_corpus` refuses a non-empty `-wal` because an
    `immutable=1` read of a database with an outstanding log silently returns
    the pre-commit row set (`sources/adoption.py:208-228`).
    """

    database = Path(database)
    if not database.exists():
        return True
    connection = sqlite3.connect(str(database))
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
    finally:
        connection.close()
    log = database.with_name(database.name + "-wal")
    try:
        return not log.exists() or log.stat().st_size == 0
    except OSError:
        return False


def _chunk_count(database: Path, paper_dir: str) -> int:
    """How many chunks the ingest actually indexed, read back from the copy."""

    if not database or not Path(database).exists():
        return 0
    connection = sqlite3.connect(str(database))
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM chunks WHERE paper_dir = ?", (paper_dir,)
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.DatabaseError:
        return 0
    finally:
        connection.close()


def _capability_refusal(
    engine_message: str, status: Mapping[str, str] | None
) -> str:
    """The engine's refusal, plus the supervisor's reason when it has one."""

    if not status or status.get("state") in (None, "ready"):
        return engine_message
    return f"{engine_message}; OCR capability {status['state']}: {status.get('reason', '')}"


def _ingest_arxiv(
    payload: Mapping[str, Any],
    capabilities: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    from cortex_research.paper_ingest import (
        IngestError,
        OcrUnavailableError,
        TransientIngestError,
        _norm_id,
        ingest_arxiv,
    )

    identifier = str(payload["identifier"])
    try:
        arxiv_id = _norm_id(identifier)
    except IngestError as error:
        # The payload is not an arxiv identifier at all: the source is wrong,
        # which is a different refusal from a materialization that failed.
        raise _Refusal("invalid_source", str(error)) from error
    try:
        result = ingest_arxiv(arxiv_id, source="product", strict=True)
    except OcrUnavailableError as error:
        # A PDF-only paper this installation cannot OCR: the paper is fine and
        # a retry succeeds once the operator's OCR skill is accepted.
        raise _Refusal(
            "capability_unavailable",
            _capability_refusal(str(error), (capabilities or {}).get("ocr")),
        ) from error
    except IngestError as error:
        raise _Refusal("materialization_failed", str(error)) from error
    except TransientIngestError as error:
        raise _Refusal("outcome_unknown", str(error)) from error
    if not result.get("ok"):
        # The URL path returns ok=False with exit 0, so the result field is the
        # verdict and the exit code is not.
        category = "outcome_unknown" if result.get("transient") else "materialization_failed"
        raise _Refusal(category, str(result.get("error") or "ingest reported ok=false"))
    paper_dir = result.get("paper_dir")
    engine = dict(result)
    if paper_dir:
        engine["chunk_count"] = _chunk_count(
            os.environ.get("CORTEX_RESEARCH_DB", ""), str(paper_dir)
        )
    return {"engine": engine, "paper_dirs": [paper_dir] if paper_dir else []}


def _reconcile_arxiv(payload: Mapping[str, Any]) -> dict[str, Any]:
    from cortex_research.paper_ingest import _corpus_lookup, _norm_id

    arxiv_id = _norm_id(str(payload["identifier"]))
    existing = _corpus_lookup(arxiv_id)
    return {
        "engine": {"arxiv_id": arxiv_id, "paper_dir": existing},
        "paper_dirs": [existing] if existing else [],
    }


def _self_check(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Write to a path the request names, so the boundary can be proven live.

    D9 item 7 requires the write boundary to fire on a deliberately unbound
    path, and the AST scan found no real one in the package -- so the
    unbound write has to be synthesised, under an operation that does nothing
    else and only ever touches the path it is handed.
    """

    target = payload.get("write_path")
    if target:
        path = Path(str(target))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("p4.2 write-boundary probe\n", encoding="utf-8")
    engine: dict[str, Any] = {"write_path": target}
    if payload.get("report_environment_names"):
        # NAMES only. A value is a credential until proven otherwise, and this
        # document is written to disk and echoed into the acceptance record.
        engine["environment_names"] = sorted(os.environ)
    return {"engine": engine, "paper_dirs": []}


# -- ⟦XHS⟧ first-party provider operations ------------------------------------
#
# Each handler imports its research-profile client lazily, reads only the
# credential variables its operation is bound (`bindings.py`), and passes the
# value to the client as an argument: no client module reads the environment.
# A provider failure leaves as its own category; nothing here retries.


def _payload_text(
    payload: Mapping[str, Any], name: str, *, maximum: int = 2_000, blank: bool = False
) -> str:
    value = payload.get(name, "" if blank else None)
    if not isinstance(value, str) or len(value) > maximum or (not blank and not value):
        raise _Refusal("invalid_response", f"invalid request: {name}")
    return value


def _credential(alias: str) -> str:
    """The value bound for one alias in this child, or `""` when unbound."""

    from .bindings import PROVIDER_SECRET_BINDINGS, engine_secret_aliases

    name = {**engine_secret_aliases(), **PROVIDER_SECRET_BINDINGS}[alias]
    return os.environ.get(name, "")


def _provider_call(function, *args, **kwargs):
    """Run one client call, turning its typed failure into a refusal."""

    from cortex_research.provider_http import ProviderError

    try:
        return function(*args, **kwargs)
    except ProviderError as error:
        raise _Refusal(error.category, error.message) from error


def _inside(path: str, roots: tuple[Path, ...]) -> Path:
    """A directory strictly below one bound write root, or a refusal."""

    candidate = Path(path)
    if not candidate.is_absolute():
        raise _Refusal("invalid_response", "invalid request: path is not absolute")
    resolved = candidate.resolve(strict=False)
    for root in roots:
        bound = Path(root).resolve(strict=False)
        if resolved != bound and resolved.is_relative_to(bound):
            return resolved
    raise _Refusal("invalid_response", "invalid request: path is outside the write roots")


def _gpt_settings(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        "base": _payload_text(payload, "gpt_base", blank=True),
        "model": _payload_text(payload, "gpt_model", maximum=200),
        "effort": _payload_text(payload, "gpt_effort", maximum=40),
        "api_key": _credential("sub2api-gpt"),
    }


def _xhs_list_page(payload: Mapping[str, Any]) -> dict[str, Any]:
    from cortex_research import xhs_client

    user_id = _payload_text(payload, "user_id", maximum=24)
    cursor = _payload_text(payload, "cursor", maximum=400, blank=True)
    try:
        page = _provider_call(
            xhs_client.list_user_notes,
            user_id,
            cursor,
            api_key=_credential("tikhub"),
            base=_payload_text(payload, "tikhub_base"),
        )
    except ValueError as error:
        raise _Refusal("invalid_response", f"invalid request: {error}") from error
    return {
        "engine": {
            "user_id": user_id,
            "cursor": cursor,
            "has_more": page.has_more,
            "next_cursor": page.next_cursor,
            "notes": [note.to_dict() for note in page.notes],
            "raw": page.raw,
        },
        "paper_dirs": [],
    }


def _xhs_note_detail(payload: Mapping[str, Any]) -> dict[str, Any]:
    from cortex_research import xhs_client

    note_id = _payload_text(payload, "note_id", maximum=24)
    try:
        detail = _provider_call(
            xhs_client.note_detail,
            note_id,
            api_key=_credential("tikhub"),
            base=_payload_text(payload, "tikhub_base"),
        )
    except ValueError as error:
        raise _Refusal("invalid_response", f"invalid request: {error}") from error
    return {"engine": {"note": detail.to_dict(), "raw": detail.raw}, "paper_dirs": []}


def _xhs_download_image(
    payload: Mapping[str, Any], write_roots: tuple[Path, ...]
) -> dict[str, Any]:
    from cortex_research import xhs_client

    staging = _inside(_payload_text(payload, "staging_dir", maximum=4_000), write_roots)
    image = _provider_call(
        xhs_client.download_image, _payload_text(payload, "url", maximum=8_000), staging
    )
    described = image.to_dict()
    described["name"] = image.path.name
    del described["path"]
    return {
        "engine": {
            "note_id": payload.get("note_id"),
            "ordinal": payload.get("ordinal"),
            "image": described,
        },
        "paper_dirs": [],
    }


def _xhs_ocr_image(payload: Mapping[str, Any]) -> dict[str, Any]:
    import hashlib

    from cortex_research import image_ocr, xhs_client

    path = Path(_payload_text(payload, "image_path", maximum=4_000))
    expected = _payload_text(payload, "sha256", maximum=64)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise _Refusal("invalid_response", "invalid request: image is unreadable") from error
    if hashlib.sha256(data).hexdigest() != expected:
        raise _Refusal("invalid_response", "invalid request: image bytes changed")
    media_type = _provider_call(xhs_client.sniff_image, data)[0]
    overrides = {
        name: _payload_text(payload, name)
        for name in ("novita_base", "glm_url")
        if payload.get(name)
    }
    result = _provider_call(
        image_ocr.ocr_image,
        data,
        media_type,
        novita_key=_credential("novita") or None,
        glm_app_id=_credential("glm-app-id") or None,
        glm_key=_credential("glm") or None,
        **overrides,
    )
    return {
        "engine": {
            "sha256": expected,
            "engine": result.engine,
            "markdown": result.markdown,
            "text_sha256": hashlib.sha256(result.markdown.encode("utf-8")).hexdigest(),
            "flags": list(result.flags),
            "finish_reason": result.finish_reason,
            "usage": dict(result.usage) if result.usage else None,
            "attempts": [attempt.to_dict() for attempt in result.attempts],
            "raw": dict(result.raw),
        },
        "paper_dirs": [],
    }


def _transcriptions(payload: Mapping[str, Any]) -> list[tuple[int, str]]:
    entries = payload.get("transcriptions")
    if not isinstance(entries, list):
        raise _Refusal("invalid_response", "invalid request: transcriptions")
    pairs: list[tuple[int, str]] = []
    for entry in entries:
        image = entry.get("image") if isinstance(entry, dict) else None
        text = entry.get("text") if isinstance(entry, dict) else None
        if isinstance(image, bool) or not isinstance(image, int) or not isinstance(text, str):
            raise _Refusal("invalid_response", "invalid request: transcriptions")
        pairs.append((image, text))
    return pairs


def _xhs_identify(payload: Mapping[str, Any]) -> dict[str, Any]:
    from cortex_research import responses_client

    from cortex_platform.product.xhs import identify

    caption = _payload_text(payload, "caption", maximum=20_000, blank=True)
    transcriptions = _transcriptions(payload)
    try:
        text = identify.build_identify_input(caption, transcriptions)
        digest = identify.input_sha256(caption, transcriptions)
    except ValueError as error:
        raise _Refusal("invalid_response", f"invalid request: {error}") from error
    answer = _provider_call(
        responses_client.create_response,
        text,
        instructions=identify.IDENTIFY_INSTRUCTIONS,
        **_gpt_settings(payload),
    )
    try:
        model_items = identify.parse_model_items(answer.text)
    except identify.IdentifyAnswerError as error:
        # Never an empty list: an answer that is not the asked-for JSON fails.
        raise _Refusal("invalid_response", f"responses: {error}") from error
    outcome = identify.identify(caption, transcriptions, model_items)
    return {
        "engine": {
            "prompt_version": identify.PROMPT_VERSION,
            "input_sha256": digest,
            "response_id": answer.response_id,
            "model": answer.model,
            "usage": dict(answer.usage) if answer.usage else None,
            "model_items": model_items,
            "items": [dict(item) for item in outcome.items],
            "dropped": outcome.dropped,
            "rule_items": outcome.rule_items,
        },
        "paper_dirs": [],
    }


def _xhs_resolve_link(payload: Mapping[str, Any]) -> dict[str, Any]:
    from cortex_research import blog_fetch, responses_client
    from cortex_research.provider_http import PolicyRefusal, ProviderError

    from cortex_platform.product.xhs import identify

    title = _payload_text(payload, "title", maximum=1_000)
    answer = _provider_call(
        responses_client.create_response,
        identify.build_link_input(title),
        instructions=identify.LINK_INSTRUCTIONS,
        tools=[{"type": "web_search"}],
        **_gpt_settings(payload),
    )
    try:
        url, page_title = identify.parse_link_answer(answer.text)
    except identify.IdentifyAnswerError as error:
        raise _Refusal("invalid_response", f"responses: {error}") from error
    engine: dict[str, Any] = {
        "prompt_version": identify.LINK_PROMPT_VERSION,
        "response_id": answer.response_id,
        "usage": dict(answer.usage) if answer.usage else None,
        "url": url,
        "page_title": page_title,
        "url_state": "not_found",
        "final_url": None,
        "checked_title": None,
        "verification_failure": None,
    }
    if url is None:
        return {"engine": engine, "paper_dirs": []}
    try:
        final_url, fetched_title, og_title = blog_fetch.fetch_title(url)
    except PolicyRefusal as error:
        # A suggested link the fetch policy refuses is not shown at all.
        engine.update(url=None, verification_failure=error.to_dict())
        return {"engine": engine, "paper_dirs": []}
    except ProviderError as error:
        engine.update(url_state="unverified", verification_failure=error.to_dict())
        return {"engine": engine, "paper_dirs": []}
    matched = identify.title_matches(title, (fetched_title, og_title))
    engine.update(
        url_state="auto_matched" if matched else "unverified",
        final_url=final_url,
        checked_title=og_title or fetched_title,
    )
    return {"engine": engine, "paper_dirs": []}


def _write_file(path: Path, data: bytes) -> dict[str, Any]:
    import hashlib

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_bytes(data)
    temporary.replace(path)
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _blog_fetch(payload: Mapping[str, Any], write_roots: tuple[Path, ...]) -> dict[str, Any]:
    from cortex_research import blog_fetch

    from cortex_platform.product.sources.identity import blog_url_identity

    staging = _inside(_payload_text(payload, "staging_dir", maximum=4_000), write_roots)
    try:
        normalized, authority_id = blog_url_identity(_payload_text(payload, "url"))
    except ValueError as error:
        raise _Refusal("not_found", f"blog: refused: {error}") from error
    options: dict[str, Any] = {"jina_key": _credential("jina") or None}
    if payload.get("jina_base"):
        options["jina_base"] = _payload_text(payload, "jina_base")
    article = _provider_call(blog_fetch.fetch_blog, normalized, **options)
    files = {
        "article.md": _write_file(
            staging / "article.md", blog_fetch.article_markdown(article).encode("utf-8")
        )
    }
    # Raw HTML only when the origin page was really fetched.
    if article.page_html is not None:
        files["raw/page.html"] = _write_file(staging / "raw" / "page.html", article.page_html)
    if article.jina_text is not None:
        files["raw/jina.md"] = _write_file(
            staging / "raw" / "jina.md", article.jina_text.encode("utf-8")
        )
    return {
        "engine": {
            "normalized_url": normalized,
            "authority_id": authority_id,
            "metadata": article.metadata(),
            "files": files,
        },
        "paper_dirs": [],
    }


class _Refusal(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


def _write_roots(request: EffectRequest) -> tuple[Path, ...]:
    return tuple(Path(root) for root in request.write_roots)


_HANDLERS = {
    "ingest_arxiv": lambda request: _ingest_arxiv(request.payload, request.capabilities),
    "reconcile_arxiv": lambda request: _reconcile_arxiv(request.payload),
    "self_check": lambda request: _self_check(request.payload),
    "checkpoint": lambda request: {"engine": {}, "paper_dirs": []},
    "xhs_list_page": lambda request: _xhs_list_page(request.payload),
    "xhs_note_detail": lambda request: _xhs_note_detail(request.payload),
    "xhs_download_image": lambda request: _xhs_download_image(
        request.payload, _write_roots(request)
    ),
    "xhs_ocr_image": lambda request: _xhs_ocr_image(request.payload),
    "xhs_identify": lambda request: _xhs_identify(request.payload),
    "xhs_resolve_link": lambda request: _xhs_resolve_link(request.payload),
    "blog_fetch": lambda request: _blog_fetch(request.payload, _write_roots(request)),
}


def run(request: EffectRequest) -> dict[str, Any]:
    audit = _WriteAudit()
    audit.install()
    # Snapshot before the handler: older engine processes are not this effect's
    # descendants and must not turn a successful result into an unknown outcome.
    baseline = survivors.snapshot_processes()
    watch = {name: Path(root) for name, root in request.watch_roots.items()}
    before = digests.sample_trees(watch) if watch else None

    result: dict[str, Any] = {
        "schema_version": 1,
        "operation": request.operation,
        "marker": request.marker,
        "ok": False,
        "engine": None,
        "paper_dirs": [],
        "failure": None,
        "checkpointed": False,
        "write_boundary": {"ok": True, "violations": []},
        "survivors": {},
        "gdrive": None,
    }
    try:
        outcome = _HANDLERS[request.operation](request)
        result["engine"] = outcome["engine"]
        result["paper_dirs"] = outcome["paper_dirs"]
        result["ok"] = True
    except _Refusal as refusal:
        result["failure"] = {"category": refusal.category, "message": refusal.message}
    except Exception as error:  # noqa: BLE001 - the category is the contract
        provider = request.operation in PROVIDER_OPERATIONS
        result["failure"] = {
            # A provider call may already have been made, and billed, when an
            # unexpected error ends the handler: that outcome is unknown.
            "category": "outcome_unknown" if provider else "materialization_failed",
            "message": f"{type(error).__name__}: {error}",
        }

    # ⟦AMD-5⟧ the completion protocol, in order, whatever the outcome was. A
    # provider operation never opens `research.db`, so it owes no checkpoint.
    result["checkpointed"] = (
        True
        if request.operation in PROVIDER_OPERATIONS
        else checkpoint_research_db(Path(request.research_db))
    )
    violations = assess_write_boundary(
        audit.paths, tuple(Path(root) for root in request.write_roots)
    )
    result["write_boundary"] = {"ok": not violations, "violations": list(violations)}
    report = survivors.scan(
        state_dir=Path(request.state_dir),
        marker=request.marker,
        needles=(request.marker, "-m cortex_research", request.state_dir),
        baseline=baseline,
    )
    result["survivors"] = report.to_dict()
    if watch:
        after = digests.sample_trees(watch)
        result["gdrive"] = {
            "before": before,
            "after": after,
            "changed": list(digests.differences(before or {}, after)),
        }
    if result["ok"] and (
        not result["checkpointed"]
        or violations
        or not report.clean
        or (result["gdrive"] or {}).get("changed")
    ):
        result["ok"] = False
        result["failure"] = {
            "category": "outcome_unknown",
            "message": "completion protocol did not pass",
        }
    return result


def main(argv: list[str] | None = None) -> int:
    raw = json.loads(sys.stdin.read())
    request = EffectRequest.from_dict(raw)
    try:
        result = run(request)
    except Exception:  # pragma: no cover - a crash here is an unknown outcome
        result = {
            "schema_version": 1,
            "operation": request.operation,
            "marker": request.marker,
            "ok": False,
            "engine": None,
            "paper_dirs": [],
            "failure": {
                "category": "outcome_unknown",
                "message": traceback.format_exc(limit=1).strip().splitlines()[-1],
            },
            "checkpointed": False,
            "write_boundary": {"ok": True, "violations": []},
            "survivors": {},
            "gdrive": None,
        }
    write_result(Path(request.result_path), result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
