"""`cortex xhs …`: followed bloggers, the two schedule rows, scans, retries and status.

The commands only write Control state; nothing here reaches a provider. The
blogger IDs are synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

from cortex_platform.product.cli import main
from cortex_platform.product.control import ControlStore

USER = "5f0e1d2c3b4a59687766554a"
OTHER_USER = "5f0e1d2c3b4a59687766554b"


def _paths(root: Path) -> list[str]:
    return [
        "--config-dir", str(root / "config"),
        "--data-dir", str(root / "data"),
        "--state-dir", str(root / "state"),
        "--cache-dir", str(root / "cache"),
        "--log-dir", str(root / "log"),
    ]


def _run(root: Path, *args: str, capsys) -> tuple[int, dict]:
    code = main([*args, *_paths(root)], environ={"HOME": str(root)})
    out, err = capsys.readouterr()
    text = out.strip() or err.strip()
    return code, (json.loads(text) if text else {})


def _store(root: Path) -> ControlStore:
    store = ControlStore(root / "data" / "control.db")
    store.initialize()
    return store


def _ready(root: Path, capsys, *, enabled: bool = True) -> ControlStore:
    """An initialized product with both plugin roots and two followed bloggers."""

    assert main(["init", *_paths(root)], environ={"HOME": str(root)}) == 0
    capsys.readouterr()
    if enabled:
        with (root / "config" / "config.toml").open("a", encoding="utf-8") as stream:
            stream.write("\n[xhs]\nenabled = true\nmax_list_pages = 2\n")
    store = _store(root)
    for root_id in ("xhs-notes", "blogs"):
        store.register_asset_root(
            root_id=root_id, private_path=root / "assets" / root_id, max_bytes=1 << 30,
            enabled=True, actor_id="local-operator", idempotency_key=f"root-{root_id}-00001",
        )
    for user in (USER, OTHER_USER):
        assert _run(root, "xhs", "follow", user, "--role", "curator", capsys=capsys)[0] == 0
    return store


def test_follow_set_role_unfollow_and_list(tmp_path, capsys) -> None:
    code, followed = _run(
        tmp_path, "xhs", "follow", USER.upper(), "--role", "curator", "--name", "合成博主",
        capsys=capsys,
    )
    assert code == 0
    assert (followed["user_id"], followed["role"], followed["followed"]) == (
        USER, "curator", True,
    )
    assert followed["display_name"] == "合成博主"
    code, changed = _run(tmp_path, "xhs", "set-role", USER, "author", capsys=capsys)
    assert (code, changed["role"]) == (0, "author")
    code, unfollowed = _run(tmp_path, "xhs", "unfollow", USER, capsys=capsys)
    assert (code, unfollowed["followed"]) == (0, False)
    code, listed = _run(tmp_path, "xhs", "list", capsys=capsys)
    assert code == 0
    assert [(b["user_id"], b["role"], b["followed"]) for b in listed["bloggers"]] == [
        (USER, "author", False)
    ]
    code, error = _run(tmp_path, "xhs", "follow", "abc", "--role", "curator", capsys=capsys)
    assert (code, error["error"]) == (1, "invalid_request")
    code, error = _run(tmp_path, "xhs", "unfollow", OTHER_USER, capsys=capsys)
    assert code == 1 and error["error"] != "invalid_request"


def test_status_reads_without_creating_the_database(tmp_path, capsys) -> None:
    code, error = _run(tmp_path, "xhs", "status", capsys=capsys)
    assert code == 1 and "cortex init" in error["message"]
    assert not (tmp_path / "data" / "control.db").exists()
    _ready(tmp_path, capsys, enabled=False)
    code, status = _run(tmp_path, "xhs", "status", capsys=capsys)
    assert code == 0
    assert (status["enabled_in_config"], status["refusal"]) == (False, "disabled_in_config")
    assert status["roots"] == {"blogs": "ready", "xhs-notes": "ready"}
    assert {key: row["enabled"] for key, row in status["schedules"].items()} == {
        "xhs-pull": False, "xhs-drain": False,
    }
    assert [b["user_id"] for b in status["bloggers"]] == [USER, OTHER_USER]
    assert status["tasks"]["pending"] == 0
    assert status["usage"]["gpt"] == {"calls": 0, "cap": 300}
    assert status["last_failures"]["tikhub"] is None


def test_enable_and_disable_arm_both_schedule_rows(tmp_path, capsys) -> None:
    store = _ready(tmp_path, capsys)
    before = {key: store.get_research_schedule(key)["revision"] for key in ("xhs-pull", "xhs-drain")}
    code, enabled = _run(tmp_path, "xhs", "enable", capsys=capsys)
    assert code == 0 and enabled["refusal"] is None
    assert {key: row["enabled"] for key, row in enabled["schedules"].items()} == {
        "xhs-pull": True, "xhs-drain": True,
    }
    assert {key: row["revision"] for key, row in enabled["schedules"].items()} == {
        key: revision + 1 for key, revision in before.items()
    }
    # Already armed: nothing changes.
    code, again = _run(tmp_path, "xhs", "enable", capsys=capsys)
    assert again["schedules"] == enabled["schedules"]
    code, disabled = _run(tmp_path, "xhs", "disable", capsys=capsys)
    assert code == 0
    assert {row["enabled"] for row in disabled["schedules"].values()} == {False}


def test_scan_queues_a_scan_and_a_full_scan_needs_its_estimate_confirmed(
    tmp_path, capsys
) -> None:
    store = _ready(tmp_path, capsys)
    code, error = _run(tmp_path, "xhs", "scan", "--full", capsys=capsys)
    assert code == 1 and "--max-pages" in error["message"]
    code, estimate = _run(
        tmp_path, "xhs", "scan", "--full", "--max-pages", "5", capsys=capsys
    )
    assert code == 1 and estimate["enqueued"] is False
    assert estimate["estimate"]["tikhub_list_calls_at_most"] == 10
    assert estimate["estimate"]["daily_calls"] == {"gpt": 300, "ocr": 1000, "tikhub": 100}
    assert store.xhs_task_counts()["pending"] == 0
    code, queued = _run(
        tmp_path, "xhs", "scan", "--full", "--max-pages", "5", "--yes", "--user", USER,
        capsys=capsys,
    )
    assert code == 0 and queued["enqueued"] is True
    assert queued["estimate"]["tikhub_list_calls_at_most"] == 5
    assert [scan["user_id"] for scan in queued["scans"]] == [USER]
    task = store.get_xhs_task(queued["scans"][0]["task_id"])
    assert (task["payload"]["full"], task["payload"]["max_pages"]) == (True, 5)
    # A plain scan uses the configured page limit and skips the unfinished scan.
    code, plain = _run(tmp_path, "xhs", "scan", capsys=capsys)
    assert code == 0 and [scan["user_id"] for scan in plain["scans"]] == [OTHER_USER]
    other = store.get_xhs_task(plain["scans"][0]["task_id"])
    assert (other["payload"]["full"], other["payload"]["max_pages"]) == (False, 2)


def test_scan_refuses_while_the_plugin_is_disabled_in_config(tmp_path, capsys) -> None:
    store = _ready(tmp_path, capsys, enabled=False)
    code, error = _run(tmp_path, "xhs", "scan", capsys=capsys)
    assert code == 1 and "disabled_in_config" in error["message"]
    assert store.xhs_task_counts()["pending"] == 0


def test_retry_failed_makes_failed_tasks_due_again(tmp_path, capsys) -> None:
    store = _ready(tmp_path, capsys)
    code, error = _run(tmp_path, "xhs", "retry", capsys=capsys)
    assert code == 1 and "--failed" in error["message"]
    created = store.start_xhs_scans(max_pages=1)
    for _ in created:
        task = store.claim_xhs_task(lease_seconds=60)
        store.record_xhs_task_failure(
            task["id"], expected_revision=task["revision"], category="not_found"
        )
    assert store.xhs_task_counts()["failed"] == 2
    code, status = _run(tmp_path, "xhs", "status", capsys=capsys)
    assert status["last_failures"]["tikhub"] == "not_found"
    assert {b["last_scan_outcome"] for b in status["bloggers"]} == {"failed"}
    code, retried = _run(tmp_path, "xhs", "retry", "--failed", "--kind", "detail", capsys=capsys)
    assert (code, retried["retried"], retried["skipped"]) == (0, {"detail": 0}, 0)
    code, retried = _run(tmp_path, "xhs", "retry", "--failed", capsys=capsys)
    assert code == 0 and retried["retried"]["list_page"] == 2
    assert store.xhs_task_counts()["pending"] == 2
