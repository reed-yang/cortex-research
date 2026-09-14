import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import review


def packet():
    return {
        "repository": "example/cortex-research", "pr_number": 12,
        "head_sha": "a" * 40, "base_sha": "b" * 40, "packet_id": "packet-1",
        "coverage": "bounded_changed_files_only", "omitted": [],
        "files": [{"path": "a.py", "status": "modified", "patch": "+return value[0]",
                   "head_text": "def first(value):\n    return value[0]\n"}],
    }


def answer():
    return {"summary": "One finding", "limitations": ["Caller not supplied"], "findings": [
        {"path": "a.py", "line": 2, "severity": "P2", "title": "Empty value",
         "body": "An empty list fails at index zero.", "evidence": "return value[0]"}
    ]}


def configuration():
    config = review.load(Path(__file__).with_name("backends.json"))
    # Injected runners test coordinator behavior, never Gemini entitlement.
    config["backends"]["gemini-ai-pro"].pop("disabled_reason", None)
    return config


class OutputTests(unittest.TestCase):
    def test_evidence_missing_from_packet_is_rejected(self):
        obj = answer()
        obj["findings"][0]["evidence"] = "invented_code()"
        with self.assertRaisesRegex(review.ReviewError, "evidence_not_in_packet"):
            review.parse_findings(json.dumps(obj), packet())

    def test_unknown_file_is_rejected(self):
        obj = answer()
        obj["findings"][0]["path"] = "../../oauth.json"
        with self.assertRaises(review.ReviewError):
            review.parse_findings(json.dumps(obj), packet())

    def test_unverified_line_loses_anchor_not_finding(self):
        obj = answer()
        obj["findings"][0]["line"] = 900
        result = review.parse_findings(json.dumps(obj), packet())
        self.assertIsNone(result["findings"][0]["line"])

    def test_line_without_the_claimed_evidence_loses_anchor(self):
        obj = answer()
        obj["findings"][0]["line"] = 1
        result = review.parse_findings(json.dumps(obj), packet())
        self.assertIsNone(result["findings"][0]["line"])

    def test_no_reviewable_text_does_not_spend_model_calls(self):
        empty = dict(packet(), files=[])
        with self.assertRaisesRegex(review.ReviewError, "no_reviewable_text"):
            review.run_reviews(empty, configuration())

    def test_rejects_boolean_line_and_invalid_schema(self):
        obj = answer()
        obj["findings"][0]["line"] = True
        for text in ["[]", "Not JSON", json.dumps(obj)]:
            with self.assertRaises(review.ReviewError):
                review.parse_findings(text, packet())

    def test_two_independent_reviews_keep_unique_findings(self):
        def grok(*args):
            return json.dumps(answer()), "grok-test", {}
        def gemini(*args):
            return json.dumps({"summary": "No finding", "limitations": [], "findings": []}), "gemini-test", {}
        result = review.run_reviews(packet(), configuration(), {
            "compatible_packet": grok, "antigravity_packet": gemini,
        })
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["reviews"][0]["findings"]), 1)
        self.assertEqual(len(result["reviews"][1]["findings"]), 0)

    def test_failed_backend_is_partial_not_clean(self):
        def fail(*args):
            raise review.ReviewError("http_403")
        result = review.run_reviews(packet(), configuration(), {
            "compatible_packet": lambda *args: (json.dumps(answer()), "grok", {}),
            "antigravity_packet": fail,
        })
        self.assertEqual(result["status"], "partial")
        self.assertIn("该审查未完成", review.render(result))

    def test_mentions_and_model_links_are_not_rendered_as_active_markup(self):
        escaped = review.plain("@reed [click](https://example.com) <img> `code`\nnext")
        self.assertNotIn("@", escaped)
        self.assertNotIn("[", escaped)
        self.assertNotIn("<img>", escaped)
        self.assertNotIn("`", escaped)


class RoutingTests(unittest.TestCase):
    def test_shipped_config_blocks_unqualified_subscription_before_invoking_harness(self):
        config = review.load(Path(__file__).with_name("backends.json"))
        def unexpected(*args):
            self.fail("Unqualified subscription adapter must not run")
        result = review.run_slot(config["slots"][1], config["backends"], packet(),
                                 {"antigravity_packet": unexpected})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempts"][0]["error"], "antigravity_subscription_hosted_auth_unqualified")

    def test_transport_failure_uses_configured_alternative(self):
        config = configuration()
        config["backends"]["grok-backup"] = dict(config["backends"]["grok-gateway"])
        config["backends"]["grok-backup"]["key_env"] = "BACKUP"
        slot = dict(config["slots"][0], backends=["grok-gateway", "grok-backup"])
        calls = []
        def run(backend, prompt):
            calls.append(backend["key_env"])
            if len(calls) == 1:
                raise review.ReviewError("http_503")
            return json.dumps(answer()), "grok-backup-model", {}
        result = review.run_slot(slot, config["backends"], packet(), {"compatible_packet": run})
        self.assertEqual(result["backend"], "grok-backup")
        self.assertEqual(len(result["attempts"]), 2)

    def test_quota_exhaustion_does_not_automatically_switch(self):
        config = configuration()
        config["backends"]["backup"] = dict(config["backends"]["grok-gateway"])
        slot = dict(config["slots"][0], backends=["grok-gateway", "backup"])
        calls = []
        def run(*args):
            calls.append(True)
            raise review.ReviewError("http_429")
        result = review.run_slot(slot, config["backends"], packet(), {"compatible_packet": run})
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "failed")

    def test_fallback_cannot_impersonate_an_independent_model_family(self):
        config = configuration()
        slot = dict(config["slots"][0], backends=["gemini-ai-pro"])
        with self.assertRaisesRegex(review.ReviewError, "preserve_opinion_family"):
            review.run_slot(slot, config["backends"], packet())


class AuthenticationTests(unittest.TestCase):
    def test_readiness_reports_names_without_secret_values(self):
        with patch.dict(os.environ, {"GROK_API_KEY": "secret-value", "GROK_MODEL": "test",
                                     "GROK_BASE_URL": "https://example.invalid/v1"}, clear=True):
            result = review.configuration_status(review.load(Path(__file__).with_name("backends.json")))
        self.assertEqual(result[0]["status"], "configured")
        self.assertEqual(result[1]["status"], "unavailable")
        self.assertNotIn("secret-value", json.dumps(result))

    def test_missing_review_configuration_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            result = review.configuration_status(configuration())
        self.assertEqual(result[0]["missing"], ["GROK_API_KEY", "GROK_BASE_URL", "GROK_MODEL"])
        self.assertEqual(result[1]["reason"], "harness_not_implemented")

    def test_provider_truncation_is_not_clean_review(self):
        backend = configuration()["backends"]["grok-gateway"]
        with patch.dict(os.environ, {"GROK_MODEL": "test", "GROK_BASE_URL": "https://gateway.example/v1", "GROK_API_KEY": "test"}):
            with patch("review.request_json", return_value={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}):
                with self.assertRaisesRegex(review.ReviewError, "incomplete"):
                    review.run_compatible(backend, "test")


class SnapshotAndPublicationTests(unittest.TestCase):
    def pr(self):
        return {"state": "open", "draft": False, "title": "test", "body": "",
                "head": {"sha": "a" * 40, "repo": {"full_name": "example/cortex-research"}},
                "base": {"sha": "b" * 40, "ref": "main"}, "changed_files": 1}

    def test_manual_collection_rejects_another_base_branch(self):
        pr = self.pr()
        pr["base"]["ref"] = "untrusted"
        with patch("review.github", return_value=pr) as api:
            with self.assertRaisesRegex(review.ReviewError, "non_default_base"):
                review.prepare("example/cortex-research", 12, "unused.md", base_branch="main")
        self.assertEqual(api.call_count, 1)

    def test_fork_collection_is_rejected_before_fetching_content(self):
        pr = self.pr()
        pr["head"]["repo"]["full_name"] = "fork/cortex-research"
        with patch("review.github", return_value=pr) as api:
            with self.assertRaisesRegex(review.ReviewError, "fork_pr_not_enabled"):
                review.prepare("example/cortex-research", 12, "unused.md", base_branch="main")
        self.assertEqual(api.call_count, 1)

    def test_dry_run_does_not_call_models_or_publish(self):
        def unexpected(*args):
            self.fail("Dry run must not call a model")
        result = review.run_reviews(packet(), configuration(),
                                    {"compatible_packet": unexpected, "antigravity_packet": unexpected},
                                    dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertTrue(all(r["status"] == "not_run" for r in result["reviews"]))
        with patch("review.github") as api:
            with self.assertRaisesRegex(review.ReviewError, "dry_run_cannot_publish"):
                review.publish(result, "example/cortex-research", 12)
            api.assert_not_called()

    def test_partial_cli_saves_report_and_exits_unsuccessfully(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            review.save(path / "packet.json", packet())
            process = subprocess.run(
                [os.sys.executable, str(Path(review.__file__)), "run",
                 "--packet", str(path / "packet.json"),
                 "--config", str(Path(__file__).with_name("backends.json")),
                 "--out", str(path / "result.json")],
                capture_output=True, text=True, env={})
            self.assertEqual(process.returncode, 1)
            self.assertEqual(review.load(path / "result.json")["status"], "failed")
            self.assertTrue((path / "result.md").is_file())

    def test_collection_rejects_changed_head(self):
        changed = self.pr()
        changed["head"]["sha"] = "c" * 40
        with tempfile.TemporaryDirectory() as temp:
            rules = Path(temp) / "rules.md"
            rules.write_text("rules")
            with patch("review.github", side_effect=[self.pr(), [{"filename": "a.py", "status": "removed", "patch": "-old_line()"}], changed]):
                with self.assertRaisesRegex(review.ReviewError, "changed_during_collection"):
                    review.prepare("example/cortex-research", 12, rules)

    def test_missing_patch_is_reported_as_omitted(self):
        with tempfile.TemporaryDirectory() as temp:
            rules = Path(temp) / "rules.md"
            rules.write_text("rules")
            with patch("review.github", side_effect=[self.pr(), [{"filename": "image.png", "status": "modified"}], self.pr()]):
                result = review.prepare("example/cortex-research", 12, rules)
        self.assertEqual(result["files"], [])
        self.assertEqual(result["omitted"][0]["path"], "image.png")

    def test_stale_result_cannot_publish(self):
        changed = self.pr()
        changed["head"]["sha"] = "c" * 40
        with patch("review.github", return_value=changed) as api:
            with self.assertRaisesRegex(review.ReviewError, "superseded"):
                review.publish(packet(), "example/cortex-research", 12)
        self.assertEqual(api.call_count, 1)

    def test_result_cannot_redirect_publisher_to_another_pr(self):
        with patch("review.github") as api:
            with self.assertRaisesRegex(review.ReviewError, "target_mismatch"):
                review.publish(packet(), "example/cortex-research", 13)
            api.assert_not_called()

    def test_publisher_updates_only_its_own_marked_comment(self):
        result = dict(packet(), status="partial", reviews=[])
        comments = [
            {"id": 1, "body": "<!-- independent-pr-review:v1 -->", "user": {"login": "someone"}},
            {"id": 2, "body": "<!-- independent-pr-review:v1 -->", "user": {"login": "github-actions[bot]"}},
        ]
        with patch("review.github", side_effect=[self.pr(), comments, self.pr(), {}]) as api:
            review.publish(result, "example/cortex-research", 12)
            self.assertEqual(api.call_args.args[1], "issues/comments/2")
            self.assertEqual(api.call_args.args[3], "PATCH")


if __name__ == "__main__":
    unittest.main()
