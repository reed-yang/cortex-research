"""Audit or refresh an existing research corpus without changing paper files.

Run with explicit --database and --corpus paths. The default is read-only;
--apply uses the retained indexer and never deletes radar stubs or missing rows.
New source adoption is a separate ControlStore operation.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote

from cortex_research.catalog import parse_notes
from cortex_research.index_papers import _chunk_rows, index_paper


def audit(database: Path, corpus: Path) -> dict:
    if not database.is_file() or not corpus.is_dir():
        raise ValueError("Database and corpus must already exist")
    connection = sqlite3.connect(f"file:{quote(str(database.resolve()), safe='/')}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        papers = {r["paper_dir"]: dict(r) for r in connection.execute("SELECT * FROM papers")}
        pending, current = [], 0
        for notes in sorted(corpus.glob("*/notes.md")):
            name = notes.parent.name
            if notes.is_symlink() or notes.parent.is_symlink():
                raise ValueError("Corpus maintenance refuses symlink inputs")
            entry = parse_notes(notes)
            row = papers.get(name)
            chunks = [(r[0], r[1]) for r in connection.execute(
                "SELECT section,text FROM chunks WHERE paper_dir=? ORDER BY chunk_idx,id", (name,))]
            same = row is not None and all(row[k] == entry.get(k) for k in ("title", "date", "summary"))
            same = same and all(json.loads(row[k] or "[]") == (entry.get(k) or []) for k in ("keywords", "projects"))
            if same and chunks == _chunk_rows(notes, entry):
                current += 1
            else:
                pending.append({"paper_dir": name, "reason": "changed" if row else "unindexed"})
        missing = sorted(name for name in papers if not name.startswith("arxiv:") and not (corpus / name).is_dir())
        return {"current": current, "pending": pending, "missing_directories": missing,
                "radar_stubs": sum(name.startswith("arxiv:") for name in papers)}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--paper-dir", action="append", default=[])
    args = parser.parse_args()
    report = audit(args.database, args.corpus)
    if args.apply:
        if os.environ.get("CORTEX_SKIP_EMBED") == "1":
            raise SystemExit("Maintenance refuses CORTEX_SKIP_EMBED=1; preserve real embeddings")
        wanted = set(args.paper_dir)
        known = {p["paper_dir"] for p in report["pending"]}
        if wanted - known:
            raise SystemExit("Explicit paper directory is not in the pending audit")
        os.environ["CORTEX_RESEARCH_DB"] = str(args.database.resolve())
        updated = []
        for item in report["pending"]:
            name = item["paper_dir"]
            if wanted and name not in wanted:
                continue
            if index_paper(args.corpus / name / "notes.md", skip_unchanged=True):
                updated.append(name)
        report["updated"] = updated
        report["after"] = audit(args.database, args.corpus)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
