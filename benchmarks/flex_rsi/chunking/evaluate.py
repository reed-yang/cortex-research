"""Public development scorer. NOT a sandbox or the official hidden evaluator."""
from __future__ import annotations
import argparse
from collections import defaultdict
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import statistics
import tempfile
import time


def compact(text):
    return "".join(text.split())


def score_output(case, chunks, paper_dir):
    errors = []
    if not isinstance(chunks, list):
        return ["not_a_list"]
    chunk_cap = sum(2 * math.ceil(len(s["body"]) / (4 * case["max_tokens"])) + 1
                    for s in case["expected_sections"] if compact(s["body"]))
    if len(chunks) > chunk_cap:
        errors.append("chunk_count_budget")
    actual, ids = [], []
    required = {"chunk_id", "text", "section", "chunk_idx", "paper_dir"}
    for chunk in chunks:
        if not isinstance(chunk, dict) or not required <= chunk.keys():
            errors.append("schema")
            continue
        if (any(not isinstance(chunk[k], str) for k in ("chunk_id", "text", "section", "paper_dir"))
                or type(chunk["chunk_idx"]) is not int or chunk["chunk_idx"] < 0):
            errors.append("types")
            continue
        if chunk["paper_dir"] != paper_dir:
            errors.append("paper_identity")
        prefix = f'Paper: {case["title"]} | Section: {chunk["section"]}\n\n'
        if not chunk["text"].startswith(prefix):
            errors.append("prefix")
            continue
        body = chunk["text"][len(prefix):]
        if not compact(body):
            errors.append("empty_body")
        # Preserve the upstream character approximation, not a tokenizer claim.
        if len(body) > 4 * case["max_tokens"]:
            errors.append("body_budget")
        actual.extend((chunk["section"], c) for c in compact(body))
        ids.append(chunk["chunk_id"])
    if len(ids) != len(set(ids)) or any(not x for x in ids):
        errors.append("chunk_identity")
    expected = [(s["section"], c) for s in case["expected_sections"] for c in compact(s["body"])]
    if actual != expected:
        errors.append("content_or_section_or_order")
    return sorted(set(errors))


def load_candidate(path):
    spec = importlib.util.spec_from_file_location("chunking_candidate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.chunk_paper


def evaluate(candidate, cases):
    function = load_candidate(candidate)
    results, families = [], defaultdict(list)
    with tempfile.TemporaryDirectory(prefix="flex-chunk-") as tmp:
        for case in cases:
            directory = Path(tmp) / case["id"]
            directory.mkdir()
            path = directory / "full_text.md"
            original = case["markdown"].encode()
            path.write_bytes(original)
            started = time.perf_counter()
            try:
                output = function(path, case["title"], case["max_tokens"])
                errors = score_output(case, output, directory.name)
                again = function(path, case["title"], case["max_tokens"])
                if output != again:
                    errors.append("nondeterministic")
                if path.read_bytes() != original:
                    errors.append("input_modified")
            except Exception as exc:
                errors = [f"exception:{type(exc).__name__}"]
            elapsed = time.perf_counter() - started
            if elapsed > 2:
                errors.append("development_time_limit")
            passed = not errors
            families[case["family"]].append(passed)
            results.append({"id": case["id"], "family": case["family"], "passed": passed,
                            "errors": errors, "elapsed_seconds": elapsed})
    scores = {k: sum(v) / len(v) for k, v in sorted(families.items())}
    return {"task": "cortex-paper-chunking-v1", "version": "0.1.0-proposal",
            "evaluation_kind": "PUBLIC_DEVELOPMENT_ONLY", "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "documents": len(results), "passed": sum(r["passed"] for r in results),
            "score": 100 * statistics.mean(scores.values()), "family_scores": scores, "cases": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(s) for s in args.cases.read_text().splitlines() if s.strip()]
    if not rows:
        raise SystemExit("empty case file")
    report = evaluate(args.candidate, rows)
    report["cases_sha256"] = hashlib.sha256(args.cases.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("evaluation_kind", "documents", "passed", "score", "family_scores")}, indent=2))
