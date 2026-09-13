"""Canonical JSON and closed-object helpers shared by distribution manifests."""

from __future__ import annotations

import json
from collections.abc import Mapping


class ClosedSchemaError(ValueError):
    """A distribution object contains missing, extra, or invalid fields."""


def canonical_json_bytes(value: object) -> bytes:
    """Serialize a distribution object with one deterministic JSON contract."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def exact_mapping(
    value: object,
    fields: set[str] | frozenset[str],
    *,
    label: str,
) -> Mapping[str, object]:
    """Return an object only when its field set matches the closed schema."""

    if not isinstance(value, dict) or set(value) != fields:
        raise ClosedSchemaError(f"{label} schema is invalid")
    return value
