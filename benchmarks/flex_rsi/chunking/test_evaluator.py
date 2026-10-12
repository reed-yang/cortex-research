"""Sanity-check scoring boundaries, including cheap reward-hacking strategies."""
import copy
import random
import unittest
from evaluate import score_output
from prepare import FAMILIES, make_case


def gold_chunks(case):
    result = []
    for section in case["expected_sections"]:
        body = section["body"]
        limit = 4 * case["max_tokens"]
        for offset in range(0, len(body), limit):
            part = body[offset:offset + limit]
            if not part.strip():
                continue
            result.append({"chunk_id": f"p::{len(result)}", "chunk_idx": len(result),
                           "paper_dir": "p", "section": section["section"],
                           "text": f'Paper: {case["title"]} | Section: {section["section"]}\n\n{part}'})
    return result


class EvaluatorTests(unittest.TestCase):
    def test_valid_segmentations(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                case = make_case(family, random.Random(73), 0)
                self.assertEqual(score_output(case, gold_chunks(case), "p"), [])

    def test_empty_drop_duplicate_and_wrong_section_fail(self):
        case = make_case("plain", random.Random(73), 0)
        gold = gold_chunks(case)
        for bad in ([], gold[:-1], gold + gold, [{**gold[0], "section": "invented"}]):
            self.assertTrue(score_output(case, bad, "p"))

    def test_oversized_content_and_colliding_ids_fail(self):
        case = make_case("long_paragraph", random.Random(73), 0)
        gold = gold_chunks(case)
        bad = copy.deepcopy(gold)
        for row in bad:
            row["chunk_id"] = "collision"
        self.assertIn("chunk_identity", score_output(case, bad, "p"))
        huge = gold[0].copy()
        huge["text"] = f'Paper: {case["title"]} | Section: Method\n\n' + case["expected_sections"][0]["body"]
        self.assertIn("body_budget", score_output(case, [huge], "p"))

    def test_injected_text_and_identity_change_fail(self):
        case = make_case("plain", random.Random(73), 0)
        gold = gold_chunks(case)
        bad = copy.deepcopy(gold)
        bad[0]["text"] += "FABRICATED"
        self.assertTrue(score_output(case, bad, "p"))
        bad = copy.deepcopy(gold)
        bad[0]["paper_dir"] = "other"
        self.assertIn("paper_identity", score_output(case, bad, "p"))

    def test_character_fragmentation_fails(self):
        case = make_case("plain", random.Random(73), 0)
        chunks = []
        for s in case["expected_sections"]:
            for c in s["body"]:
                if c.strip():
                    chunks.append({"chunk_id": str(len(chunks)), "chunk_idx": 0,
                                   "paper_dir": "p", "section": s["section"],
                                   "text": f'Paper: {case["title"]} | Section: {s["section"]}\n\n{c}'})
        self.assertIn("chunk_count_budget", score_output(case, chunks, "p"))


if __name__ == "__main__":
    unittest.main()
