"""Shape checks for the JSON a provider child returns.

cortexd records a provider result only after it passes `validate_engine` for
its operation. A result that fails is treated as `invalid_response`: the
child's document is untrusted until checked, like any other provider answer.
Only shape is checked here; meaning (verbatim evidence, title matches) is
`identify.py`'s, and for the weekly fallback `fallback.py`'s.
"""

from __future__ import annotations

from typing import Any, Mapping

_STR = (str,)
_OPT_STR = (str, type(None))
_INT = (int,)
_OPT_INT = (int, type(None))
_BOOL = (bool,)
_LIST = (list,)
_DICT = (dict,)
_OPT_DICT = (dict, type(None))

_IMAGE = {
    "ordinal": _INT,
    "fileid": _STR,
    "width": _OPT_INT,
    "height": _OPT_INT,
    "variant": _OPT_STR,
    "url": _OPT_STR,
}
_LISTED_NOTE = {
    "note_id": _STR,
    "user_id": _OPT_STR,
    "note_type": _STR,
    "sticky": _BOOL,
    "title": _STR,
    "caption": _STR,
    "caption_complete": _BOOL,
    "published_at": _OPT_STR,
    "cursor": _STR,
    "images": _LIST,
}
_DETAIL_NOTE = {
    "note_id": _STR,
    "note_type": _STR,
    "title": _STR,
    "caption": _STR,
    "caption_complete": _BOOL,
    "published_at": _OPT_STR,
    "user_id": _OPT_STR,
    "user_name": _OPT_STR,
    "images": _LIST,
}
_DOWNLOADED = {
    "name": _STR,
    "sha256": _STR,
    "byte_size": _INT,
    "media_type": _STR,
    "extension": _STR,
    "width": _OPT_INT,
    "height": _OPT_INT,
}
_ITEM = {
    "item_key": _STR,
    "kind": _STR,
    "title": _STR,
    "image": _OPT_INT,
    "quote": _STR,
    "arxiv_id": _OPT_STR,
    "url": _OPT_STR,
    "url_state": _STR,
    "origin": _STR,
}

ENGINE_SHAPES: Mapping[str, Mapping[str, tuple[type, ...]]] = {
    "xhs_list_page": {
        "user_id": _STR,
        "cursor": _STR,
        "has_more": _BOOL,
        "next_cursor": _OPT_STR,
        "notes": _LIST,
        "raw": _DICT,
    },
    "xhs_note_detail": {"note": _DICT, "raw": _DICT},
    "xhs_download_image": {"image": _DICT},
    "xhs_ocr_image": {
        "sha256": _STR,
        "engine": _STR,
        "markdown": _STR,
        "text_sha256": _STR,
        "flags": _LIST,
        "finish_reason": _OPT_STR,
        "attempts": _LIST,
        "raw": _DICT,
    },
    "xhs_identify": {
        "prompt_version": _STR,
        "input_sha256": _STR,
        "items": _LIST,
        "model_items": _LIST,
        "dropped": _INT,
        "rule_items": _INT,
    },
    "xhs_resolve_link": {
        "prompt_version": _STR,
        "url": _OPT_STR,
        "page_title": _OPT_STR,
        "url_state": _STR,
        "final_url": _OPT_STR,
        "checked_title": _OPT_STR,
        "verification_failure": _OPT_DICT,
    },
    "blog_fetch": {
        "normalized_url": _STR,
        "authority_id": _STR,
        "metadata": _DICT,
        "files": _DICT,
    },
    "xhs_fallback_decide": {
        "prompt_version": _STR,
        "input_text_sha256": _STR,
        "response_id": _OPT_STR,
        "model": _OPT_STR,
        "usage": _OPT_DICT,
        "answer": _OPT_DICT,
        "answer_error": _OPT_STR,
    },
    "xhs_fallback_verify": {
        "check": _STR,
        "requested_url": _STR,
        "final_url": _OPT_STR,
        "title": _OPT_STR,
        "og_title": _OPT_STR,
        "paper_host": _BOOL,
    },
}
_FALLBACK_ANSWER = {
    "outcome": _STR,
    "url": _OPT_STR,
    "arxiv_id": _OPT_STR,
    "reason_code": _OPT_STR,
    "reason": _OPT_STR,
}
_FALLBACK_CHECKS = frozenset({"blog", "arxiv"})
_NESTED: Mapping[tuple[str, str], Mapping[str, tuple[type, ...]]] = {
    ("xhs_note_detail", "note"): _DETAIL_NOTE,
    ("xhs_download_image", "image"): _DOWNLOADED,
}
_LISTS: Mapping[tuple[str, str], Mapping[str, tuple[type, ...]]] = {
    ("xhs_list_page", "notes"): _LISTED_NOTE,
    ("xhs_identify", "items"): _ITEM,
}
_OCR_FLAGS = frozenset({"truncated", "empty"})
_RESOLVED_STATES = frozenset({"auto_matched", "unverified", "not_found"})
_ITEM_URL_STATES = frozenset({"none", "from_text"})
_CONTENT_SOURCES = frozenset({"origin", "jina"})


def _check(value: Any, shape: Mapping[str, tuple[type, ...]], where: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{where} is not an object")
    for name, types in shape.items():
        if name not in value:
            raise ValueError(f"{where}.{name} is missing")
        field = value[name]
        # bool is an int subclass; an integer field must not accept one.
        if isinstance(field, bool) and bool not in types:
            raise ValueError(f"{where}.{name} has the wrong type")
        if not isinstance(field, types):
            raise ValueError(f"{where}.{name} has the wrong type")


def validate_engine(operation: str, engine: Any) -> Mapping[str, Any]:
    """Return `engine` unchanged when its shape fits `operation`, else raise."""

    shape = ENGINE_SHAPES.get(operation)
    if shape is None:
        raise ValueError(f"{operation} is not a provider operation")
    _check(engine, shape, operation)
    for (owner, name), nested in _NESTED.items():
        if owner == operation:
            _check(engine[name], nested, f"{operation}.{name}")
    for (owner, name), nested in _LISTS.items():
        if owner == operation:
            for index, entry in enumerate(engine[name]):
                _check(entry, nested, f"{operation}.{name}[{index}]")
    if operation == "xhs_list_page":
        for note in engine["notes"]:
            for image in note["images"]:
                _check(image, _IMAGE, f"{operation}.notes.images")
    elif operation == "xhs_note_detail":
        for image in engine["note"]["images"]:
            _check(image, _IMAGE, f"{operation}.note.images")
    elif operation == "xhs_ocr_image":
        if not set(engine["flags"]) <= _OCR_FLAGS:
            raise ValueError("xhs_ocr_image.flags carries an unknown flag")
    elif operation == "xhs_identify":
        for item in engine["items"]:
            if item["url_state"] not in _ITEM_URL_STATES:
                raise ValueError("xhs_identify item url_state is unsupported")
    elif operation == "xhs_resolve_link":
        if engine["url_state"] not in _RESOLVED_STATES:
            raise ValueError("xhs_resolve_link.url_state is unsupported")
        if (engine["url"] is None) != (engine["url_state"] == "not_found"):
            raise ValueError("xhs_resolve_link url and url_state disagree")
    elif operation == "blog_fetch":
        if engine["metadata"].get("content_source") not in _CONTENT_SOURCES:
            raise ValueError("blog_fetch.metadata.content_source is unsupported")
        if "article.md" not in engine["files"]:
            raise ValueError("blog_fetch.files has no article.md")
    elif operation == "xhs_fallback_decide":
        # The call answered, usably or not: exactly one of the two is set.
        if (engine["answer"] is None) == (engine["answer_error"] is None):
            raise ValueError("xhs_fallback_decide answer and answer_error disagree")
        if engine["answer"] is not None:
            _check(engine["answer"], _FALLBACK_ANSWER, f"{operation}.answer")
    elif operation == "xhs_fallback_verify":
        if engine["check"] not in _FALLBACK_CHECKS:
            raise ValueError("xhs_fallback_verify.check is unsupported")
        if engine["final_url"] is None and not engine["paper_host"]:
            raise ValueError("xhs_fallback_verify fetched no page")
    return engine
