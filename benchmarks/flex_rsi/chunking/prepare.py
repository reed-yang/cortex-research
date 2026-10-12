"""Create public, synthetic Markdown fixtures; never create a hidden test set."""
from __future__ import annotations
import argparse
import hashlib
import json
import random
from pathlib import Path

FAMILIES = ("plain", "long_paragraph", "fences", "frontmatter", "repeated_headings",
            "deep_headings", "exclusion_scope", "short_preamble", "multilingual",
            "mixed_math", "paragraph_budget", "unclosed_fence")


def make_case(family, rng, i):
    tag = f"{rng.getrandbits(64):016x}"
    title = f"Synthetic Paper {tag}"
    budget = rng.choice([32, 48, 64])
    a = f"Claim {tag}: the method retains evidence and measures uncertainty."
    b = f"Result {tag}: two conditions improve coverage without losing text."
    if family == "plain":
        raw, sections = f"## Method\n{a}\n\n## Results\n{b}\n", [("Method", a), ("Results", b)]
    elif family == "long_paragraph":
        body = (a + " ") * 19
        raw, sections = f"## Method\n{body}\n", [("Method", body)]
    elif family == "fences":
        fence = rng.choice(["```", "~~~~"])
        body = f"{a}\n{fence}text\n## Not a real heading\n{b}\n{fence}\nAfter fence {tag}."
        raw, sections = f"## Method\n{body}\n", [("Method", body)]
    elif family == "frontmatter":
        raw = f'---\ntitle: "metadata---not-a-delimiter"\nsecret: ignored-{tag}\n---\n## Results\n{b}\n'
        sections = [("Results", b)]
    elif family == "repeated_headings":
        raw, sections = f"## Results\n{a}\n\n## Results\n{b}\n", [("Results", a), ("Results", b)]
    elif family == "deep_headings":
        raw, sections = f"## Method\n{a}\n#### Ablation\n{b}\n", [("Method", a), ("Ablation", b)]
    elif family == "exclusion_scope":
        raw = f"## Learned Preferences\n{a}\n## References\nDROP-{tag}\n### Cited works\nDROP-NESTED-{tag}\n## Results\n{b}\n"
        sections = [("Learned Preferences", a), ("Results", b)]
    elif family == "short_preamble":
        raw, sections = f"Brief abstract {tag}.\n\n## Results\n{b}\n", [("Abstract", f"Brief abstract {tag}."), ("Results", b)]
    elif family == "multilingual":
        body = (f"证据{tag}：保留误差与限制。🙂 A result with naïve café. " * 14)
        raw, sections = f"## 结果\n{body}\n", [("结果", body)]
    elif family == "mixed_math":
        body = f"{a}\n\n$x_t = x_0 + t v$\n\n{b}"
        raw, sections = f"## Method\n{body}\n", [("Method", body)]
    elif family == "paragraph_budget":
        body = "\n\n".join((tag * 4) for _ in range(7))
        budget = 32
        raw, sections = f"## Results\n{body}\n", [("Results", body)]
    elif family == "unclosed_fence":
        body = f"{a}\n```text\n## Still code\n{b}\n"
        raw, sections = f"## Method\n{body}", [("Method", body)]
    else:
        raise ValueError(family)
    return {"id": f"{family}-{i}-{tag}", "family": family, "title": title,
            "max_tokens": budget, "markdown": raw,
            "expected_sections": [{"section": s, "body": t} for s, t in sections]}


def prepare(destination):
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {"version": "0.1.0-proposal", "synthetic": True, "splits": {}}
    for split, seed, repeats in [("train", 260101, 4), ("dev", 260102, 2)]:
        rng = random.Random(seed)
        rows = [make_case(f, rng, i) for f in FAMILIES for i in range(repeats)]
        payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode()
        (destination / f"{split}.jsonl").write_bytes(payload)
        manifest["splits"][split] = {"documents": len(rows), "sha256": hashlib.sha256(payload).hexdigest()}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(prepare(parser.parse_args().output), indent=2))
