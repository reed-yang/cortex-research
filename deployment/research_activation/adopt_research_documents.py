#!/usr/bin/env python3
"""Preview/apply explicit research dossier adoption with an installed interpreter."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from cortex_platform.product.control import ControlStore
from cortex_platform.product.control.schema import SCHEMA_VERSION
from cortex_platform.product.research.catalog import ResearchCatalog
from cortex_platform.product.research.documents import ResearchDocumentAdopter


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-db", type=Path, required=True)
    parser.add_argument("--research-db", type=Path, required=True)
    parser.add_argument("--source-root", action="append", default=[], metavar="KIND=PATH")
    parser.add_argument("--document-map", type=Path, help="Explicit item-id to document candidates JSON, required for projects without registered paths")
    parser.add_argument("--item", action="append", help="Select only these original catalog item IDs")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    store = ControlStore(args.control_db)
    adopter = ResearchDocumentAdopter(store, ResearchCatalog(args.research_db))
    if args.apply:
        if args.destination is None:
            parser.error("--apply requires --destination")
        # This utility must not covertly upgrade a live product database.
        with sqlite3.connect(args.control_db.absolute().as_uri() + "?mode=ro", uri=True) as conn:
            version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if version != SCHEMA_VERSION:
            parser.error("use the matching installed generation after its schema upgrade")
        store.initialize()
        manifest = json.loads(args.manifest.read_text())
        result = adopter.apply(manifest, destination=args.destination)
        print(json.dumps({"adopted_items": len(result["items"]), "skipped": len(result["skipped"])}, indent=2))
    else:
        roots = {}
        for value in args.source_root:
            kind, separator, root = value.partition("=")
            if not separator or kind not in {"idea", "exploration", "project"}:
                parser.error("--source-root must be idea|exploration|project=PATH")
            roots[kind] = Path(root)
        document_map = json.loads(args.document_map.read_text()) if args.document_map else None
        result = adopter.preview(roots, item_ids=set(args.item) if args.item else None, document_map=document_map)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        with args.manifest.open("x") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        args.manifest.chmod(0o600)
        print(json.dumps({"preview_items": len(result["items"]), "skipped": len(result["skipped"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
