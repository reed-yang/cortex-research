from __future__ import annotations

import sqlite3
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cortex_platform.product.control.schema import MIGRATION_VERSIONS
from cortex_platform.product.control import (
    IdempotencyConflict,
    InvalidTransition,
    RevisionConflict,
)
from cortex_platform.product.control.schema import (
    _MIGRATION_1,
    _MIGRATION_2,
    _MIGRATION_3,
    _MIGRATION_4,
)

from .fakes import (
    ECHO_TITLE,
    create_golden_intent,
    make_named_run,
    make_run,
    make_store,
    register_echo,
)


def test_v4_database_migrates_to_source_schema_without_mutating_legacy_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    with sqlite3.connect(database) as conn:
        for script in (_MIGRATION_1, _MIGRATION_2, _MIGRATION_3, _MIGRATION_4):
            conn.executescript(script)
        conn.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'old')",
            [(1,), (2,), (3,), (4,)],
        )
        conn.execute(
            """INSERT INTO workspaces
               (id, title, revision, created_at, updated_at)
               VALUES ('ws-old', 'Preserved', 3, 'old', 'old')"""
        )
    database.chmod(0o600)
    key = database.with_name(f".{database.name}.transport.key")
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)

    from cortex_platform.product.control import ControlStore

    migrated = ControlStore(database)
    migrated.initialize()

    assert migrated.get_workspace("ws-old")["revision"] == 3
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in MIGRATION_VERSIONS]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "source_intents",
        "source_candidates",
        "sources",
        "source_aliases",
        "source_resolutions",
        "run_source_bindings",
        "source_import_actions",
        "source_import_waiters",
    } <= tables
    with sqlite3.connect(database) as conn:
        intent_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(source_intents)")
        }
        candidate_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(source_candidates)")
        }
    assert {
        "locator_claim_kind",
        "locator_canonical_id",
        "locator_version",
        "locator_sha256",
    } <= intent_columns
    assert "version" in candidate_columns


def test_g0_persists_exact_conflict_with_no_import_or_materialization_side_effect(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    fixture_path = (
        Path(__file__).parents[2]
        / "fixtures"
        / "productization"
        / "g0-source-conflict.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["mutation_manifest"] == {
        "scope": "research_import_materialization_side_effects",
        "excludes_control_entities": [
            "run",
            "attempt",
            "decision",
            "event",
            "checkpoint",
        ],
        "transactions": [],
        "materializations": [],
    }
    assert fixture["source_intent"]["title"] == ECHO_TITLE
    echo = register_echo(store)
    run = make_run(store)

    intent = create_golden_intent(store, run)

    assert intent["state"] == "pending"
    assert intent["revision"] == 0
    assert intent["decision"]["state"] == "pending"
    assert [item["canonical_id"] for item in intent["candidates"]] == [
        "arxiv:2606.04527",
        "arxiv:2607.07675",
    ]
    assert intent["candidates"][0]["source_id"] == echo["id"]
    assert intent["candidates"][1]["source_id"] is None
    assert store.list_pending_source_imports() == []
    assert store.list_sources() == [echo]
    assert not (tmp_path / "research.db").exists()
    assert not (tmp_path / "imports").exists()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_import_actions").fetchone()[0] == 0
    events = store.list_run_events(run["id"])
    assert [event["type"] for event in events[-3:]] == [
        "source.intent_received",
        "source.conflict_detected",
        "decision.required",
    ]


def test_intent_replay_is_exact_and_changed_claim_conflicts(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    register_echo(store)
    run = make_run(store)
    first = create_golden_intent(store, run)
    replay = create_golden_intent(store, run)

    assert replay == first
    changed_resolver_observation = store.create_source_intent(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        title=ECHO_TITLE,
        locator=first["locator"],
        candidates=(
            {
                "claim_kind": "title",
                "authority": "arxiv",
                "authority_id": "2999.99999",
                "official_title": "Resolver drift must not replace a receipt",
            },
        ),
        actor_id="local",
        idempotency_key="source-intent-0001",
    )
    assert changed_resolver_observation.replayed is True
    assert changed_resolver_observation.value == first
    with pytest.raises(IdempotencyConflict):
        store.create_source_intent(
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            title="Changed title",
            locator=first["locator"],
            candidates=first["candidates"],
            actor_id="local",
            idempotency_key="source-intent-0001",
        )


def test_candidate_locator_must_match_its_authority_identity(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    run = make_run(store)

    with pytest.raises(ValueError, match="locator"):
        store.create_source_intent(
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            title="Echo-Infinity",
            locator="https://arxiv.org/abs/2607.07675",
            candidates=(
                {
                    "claim_kind": "url",
                    "authority": "arxiv",
                    "authority_id": "2606.04527",
                    "canonical_id": "arxiv:2606.04527",
                    "official_title": "Echo-Infinity",
                    "locator": "https://arxiv.org/abs/2607.07675",
                },
                {
                    "claim_kind": "title",
                    "authority": "arxiv",
                    "authority_id": "2606.04527",
                    "official_title": "Echo-Infinity",
                },
            ),
            actor_id="local",
            idempotency_key="mismatched-locator-001",
        )


@pytest.mark.parametrize(
    "candidates",
    [
        (
            {
                "claim_kind": "title",
                "authority": "arxiv",
                "authority_id": "2606.04527",
                "official_title": ECHO_TITLE,
            },
        ),
        (
            {
                "claim_kind": "url",
                "authority": "arxiv",
                "authority_id": "2606.04527",
                "official_title": ECHO_TITLE,
                "locator": "https://arxiv.org/abs/2606.04527",
            },
            {
                "claim_kind": "title",
                "authority": "arxiv",
                "authority_id": "2606.04527",
                "official_title": ECHO_TITLE,
            },
        ),
    ],
)
def test_wrong_resolver_cannot_omit_or_replace_supplied_locator_observation(
    tmp_path: Path, candidates: tuple[dict, ...]
) -> None:
    store = make_store(tmp_path)
    run = make_run(store)

    with pytest.raises(ValueError, match="locator observation"):
        store.create_source_intent(
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            title=ECHO_TITLE,
            locator="https://arxiv.org/pdf/2607.07675",
            candidates=candidates,
            actor_id="local",
            idempotency_key="wrong-resolver-locator-1",
        )

    assert store.get_run(run["id"])["state"] == "queued"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_intents").fetchone()[0] == 0


def test_every_supplied_title_requires_a_title_observation(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    run = make_run(store)
    with pytest.raises(ValueError, match="title observation"):
        store.create_source_intent(
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            title=ECHO_TITLE,
            locator="2607.07675",
            candidates=(
                {
                    "claim_kind": "arxiv",
                    "authority": "arxiv",
                    "authority_id": "2607.07675",
                    "official_title": "LingBot",
                },
            ),
            actor_id="local",
            idempotency_key="missing-title-observation-1",
        )


def test_arxiv_locator_version_observation_must_be_exact_and_is_persisted(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    run = make_run(store)
    intent = store.create_source_intent(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        title=None,
        locator="2607.07675v2",
        candidates=(
            {
                "claim_kind": "arxiv",
                "authority": "arxiv",
                "authority_id": "2607.07675v2",
                "official_title": "Versioned paper",
            },
        ),
        actor_id="local",
        idempotency_key="versioned-source-intent-1",
    ).value
    assert intent["locator_identity"] == {
        "claim_kind": "arxiv",
        "canonical_id": "arxiv:2607.07675",
        "version": 2,
    }
    assert intent["candidates"][0]["version"] == 2


def test_candidate_locator_version_must_match_candidate_and_user_claim(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    run = make_run(store)

    with pytest.raises(ValueError, match="version"):
        store.create_source_intent(
            run_id=run["id"],
            attempt_id=run["attempt"]["id"],
            title=None,
            locator="https://arxiv.org/abs/2607.07675v2",
            candidates=(
                {
                    "claim_kind": "url",
                    "authority": "arxiv",
                    "authority_id": "2607.07675v2",
                    "official_title": "Versioned paper",
                    "locator": "https://arxiv.org/abs/2607.07675v3",
                },
            ),
            actor_id="local",
            idempotency_key="candidate-version-drift-01",
        )


def test_candidate_observations_remain_immutable_after_resolution(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    register_echo(store)
    intent = create_golden_intent(store, make_run(store))
    before = intent["candidates"]

    store.resolve_source_intent(
        intent_id=intent["id"],
        choice="keep_both",
        expected_revision=0,
        actor_id="local",
        idempotency_key="immutable-resolution-1",
    )

    assert store.get_source_intent(intent["id"])["candidates"] == before


def test_matching_title_and_url_evidence_still_requires_explicit_resolution(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    run = make_run(store)
    candidates = (
        {
            "claim_kind": claim_kind,
            "authority": "arxiv",
            "authority_id": "2606.04527",
            "official_title": ECHO_TITLE,
            "locator": "https://arxiv.org/abs/2606.04527",
        }
        for claim_kind in ("title", "url")
    )
    intent = store.create_source_intent(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        title=ECHO_TITLE,
        locator="https://arxiv.org/abs/2606.04527",
        candidates=tuple(candidates),
        actor_id="local",
        idempotency_key="matching-source-intent-1",
    ).value

    assert intent["state"] == "pending"
    assert [option["id"] for option in intent["decision"]["options"]] == [
        "use_source",
        "cancel",
    ]
    assert store.list_pending_source_imports() == []

    resolved = store.resolve_source_intent(
        intent_id=intent["id"],
        choice="use_source",
        expected_revision=0,
        actor_id="local",
        idempotency_key="matching-source-resolve-1",
    ).value
    assert resolved["state"] == "resolved"
    assert len(store.list_pending_source_imports()) == 1


def test_alias_collision_and_unicode_alias_fail_closed(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    register_echo(store)

    with pytest.raises(InvalidTransition, match="alias"):
        store.register_source(
            authority="arxiv",
            authority_id="2607.07675",
            source_kind="paper",
            official_title="Other",
            engine_ref="paper:other",
            aliases=({"authority": "project", "value": "echo-infinity"},),
            actor_id="fixture",
            idempotency_key="register-other-00001",
        )
    with pytest.raises(ValueError, match="ASCII"):
        store.register_source(
            authority="arxiv",
            authority_id="2607.07675",
            source_kind="paper",
            official_title="Other",
            engine_ref="paper:other",
            aliases=({"authority": "project", "value": "Ｅcho"},),
            actor_id="fixture",
            idempotency_key="register-unicode-001",
        )


@pytest.mark.parametrize(
    "alias",
    [
        {"authority": "unknown", "value": "safe-name"},
        {"authority": "project", "value": "/private/operator/paper.pdf"},
        {"authority": "project", "value": "https://example.test/paper"},
        {"authority": "project", "value": "file:///tmp/paper.pdf"},
        {"authority": "project", "value": "secret token"},
        {"authority": "arxiv", "value": "https://arxiv.org/abs/2607.07675"},
    ],
)
def test_alias_authority_and_value_use_closed_safe_syntax(
    tmp_path: Path, alias: dict[str, str]
) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="alias"):
        store.register_source(
            authority="arxiv",
            authority_id="2607.07675",
            source_kind="paper",
            official_title="Other",
            engine_ref="paper:other",
            aliases=(alias,),
            actor_id="fixture",
            idempotency_key="register-unsafe-alias-1",
        )


def test_resolution_is_revision_checked_and_concurrent_choice_has_one_winner(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    register_echo(store)
    intent = create_golden_intent(store, make_run(store))

    def resolve(choice: str, key: str):
        return store.resolve_source_intent(
            intent_id=intent["id"],
            choice=choice,
            expected_revision=0,
            actor_id="local",
            idempotency_key=key,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(resolve, "keep_both", "resolve-keep-both-01"),
            pool.submit(resolve, "cancel", "resolve-cancel-00001"),
        ]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except RevisionConflict:
            outcomes.append("revision_conflict")

    assert sum(item != "revision_conflict" for item in outcomes) == 1
    assert sum(item == "revision_conflict" for item in outcomes) == 1


def test_replace_url_and_cancel_create_no_stale_import_actions(tmp_path: Path) -> None:
    for choice in ("replace_url_with_echo", "cancel"):
        case = tmp_path / choice
        case.mkdir(mode=0o700)
        store = make_store(case)
        register_echo(store)
        intent = create_golden_intent(store, make_run(store))

        result = store.resolve_source_intent(
            intent_id=intent["id"],
            choice=choice,
            expected_revision=0,
            actor_id="local",
            idempotency_key=f"resolve-{choice}-0001",
        ).value

        assert result["state"] == ("canceled" if choice == "cancel" else "resolved")
        assert store.list_pending_source_imports() == []
        bindings = result["bindings"]
        assert len(bindings) == (0 if choice == "cancel" else 1)
        if bindings:
            assert bindings[0]["disposition"] == "reused"
            assert bindings[0]["source"]["official_title"] == ECHO_TITLE
        if choice == "cancel":
            cancel_requested = store.get_run(result["run_id"])
            assert cancel_requested["state"] == "cancel_requested"
            canceled_run = store.fail_unbound_run(
                run_id=cancel_requested["id"],
                attempt_id=cancel_requested["active_attempt_id"],
                expected_revision=cancel_requested["revision"],
                category="operator_cancelled",
                actor_id="runtime",
                idempotency_key="settle-source-cancel-001",
            ).value
            thread = store.get_thread(canceled_run["thread_id"])
            assert canceled_run["state"] == "canceled"
            assert thread["active_run_id"] is None
            assert thread["status"] == "canceled"


def test_source_decision_cannot_use_generic_resolve_and_cancel_cannot_resurrect(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    register_echo(store)
    intent = create_golden_intent(store, make_run(store))

    with pytest.raises(InvalidTransition, match="source_decision"):
        store.resolve_decision(
            decision_id=intent["decision"]["id"],
            choice="keep_both",
            expected_revision=0,
            actor_id="local",
            idempotency_key="generic-source-resolve-1",
        )

    run = store.get_run(intent["run_id"])
    canceled = store.transition_run(
        run_id=run["id"],
        target_state="cancel_requested",
        expected_revision=run["revision"],
        actor_id="local",
        idempotency_key="cancel-pending-source-1",
    ).value
    assert canceled["state"] == "cancel_requested"
    closed_intent = store.get_source_intent(intent["id"])
    assert closed_intent["state"] == "canceled"
    assert closed_intent["decision"]["state"] == "expired"
    with pytest.raises(RevisionConflict):
        store.resolve_source_intent(
            intent_id=intent["id"],
            choice="keep_both",
            expected_revision=0,
            actor_id="local",
            idempotency_key="late-source-resolve-01",
        )
    assert store.list_pending_source_imports() == []


def _create_single_source_intent(store, run: dict, *, key: str) -> dict:
    return store.create_source_intent(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        title="LingBot paper",
        locator=None,
        candidates=(
            {
                "claim_kind": "title",
                "authority": "arxiv",
                "authority_id": "2607.07675",
                "official_title": "LingBot paper",
            },
        ),
        actor_id="local",
        idempotency_key=key,
    ).value


def test_pending_source_reuses_one_import_action_and_explicit_waiters(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    first = _create_single_source_intent(
        store, make_named_run(store, "first"), key="single-intent-first-01"
    )
    store.resolve_source_intent(
        intent_id=first["id"],
        choice="use_source",
        expected_revision=0,
        actor_id="local",
        idempotency_key="single-resolve-first-1",
    )
    action = store.list_pending_source_imports()[0]

    second = _create_single_source_intent(
        store, make_named_run(store, "second"), key="single-intent-second-1"
    )
    resolved = store.resolve_source_intent(
        intent_id=second["id"],
        choice="use_source",
        expected_revision=0,
        actor_id="local",
        idempotency_key="single-resolve-second-01",
    ).value

    assert len(store.list_all_source_imports()) == 1
    assert store.list_all_source_imports()[0]["id"] == action["id"]
    waiters = store.list_source_import_waiters(action["id"])
    assert {waiter["run_id"] for waiter in waiters} == {
        first["run_id"],
        second["run_id"],
    }
    assert all(waiter["state"] == "waiting" for waiter in waiters)
    assert resolved["bindings"] == []
    assert not any(
        event["type"] == "source.reused"
        for event in store.list_run_events(second["run_id"])
    )
