"""Idempotent SQLite schema migrations for the Cortex control store."""

from __future__ import annotations

import sqlite3

#: The migration that creates a gated table is named once and imported
#: everywhere the gate is asserted, so a renumber moves one number rather than
#: a literal in every file that happens to know it. `cortex_platform/backup.py`
#: reads these: a gate naming a version the migration does not create the table
#: at makes `inspect_database` refuse a healthy database.
#:
#: The set has no gaps: `distribution/state_safety.py` refuses a control
#: database whose applied versions are not exactly `1..n`, so the numbers are a
#: merge-order property rather than a per-branch choice.
#: ⟦P4/D6⟧ The runtime activation gate orchestration consults before
#: any dispatch. Separate from the transport window below, and never coupled
#: to it: one says a certified runtime may run, the other says the product
#: holds the bot token.
RUNTIME_ACTIVATION_MIGRATION = 12
CAPTURES_MIGRATION = 13
#: ⟦S3.4/D6⟧ The operator's approval of one exact release.
RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION = 14
TRANSPORT_ACTIVATION_MIGRATION = 15
#: ⟦P4.3/D7⟧ The research schedules table.
RESEARCH_SCHEDULES_MIGRATION = 16

RESEARCH_CONTEXTS_MIGRATION = 17
#: ⟦Web shell 2026-09⟧ Threads can be archived: hidden from the default list,
#: never deleted. A column, not a table, so backup.py's table roster is unchanged.
THREAD_ARCHIVE_MIGRATION = 18
RESEARCH_ITEMS_MIGRATION = 19
SCHEMA_VERSION = RESEARCH_ITEMS_MIGRATION

_MIGRATION_RESEARCH_ITEMS = """
CREATE TABLE research_items (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('idea', 'exploration', 'project')),
    origin_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(kind, origin_id)
);
CREATE TABLE research_document_versions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    item_id TEXT NOT NULL REFERENCES research_items(id),
    version INTEGER NOT NULL CHECK(version > 0),
    title TEXT NOT NULL,
    asset_root_id TEXT NOT NULL REFERENCES asset_roots(root_id),
    relative_path TEXT NOT NULL,
    origin_relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL CHECK(media_type IN ('text/markdown', 'text/plain')),
    byte_length INTEGER NOT NULL CHECK(byte_length > 0 AND byte_length <= 1048576),
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE(document_id, version),
    UNIQUE(document_id, sha256)
);
CREATE INDEX research_documents_item ON research_document_versions(item_id, document_id, version);
CREATE TRIGGER research_documents_no_update BEFORE UPDATE ON research_document_versions
BEGIN SELECT RAISE(ABORT, 'research document versions are immutable'); END;
CREATE TRIGGER research_documents_no_delete BEFORE DELETE ON research_document_versions
BEGIN SELECT RAISE(ABORT, 'research document versions are immutable'); END;
CREATE TABLE research_thread_items (
    thread_id TEXT PRIMARY KEY REFERENCES threads(id),
    item_id TEXT NOT NULL REFERENCES research_items(id),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    selected_at TEXT NOT NULL,
    actor_id TEXT NOT NULL
);
CREATE INDEX research_threads_item ON research_thread_items(item_id);
"""


_MIGRATION_RESEARCH_CONTEXTS = """
CREATE TABLE research_contexts (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    thread_id TEXT NOT NULL REFERENCES threads(id),
    message_id TEXT NOT NULL REFERENCES messages(id),
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    query TEXT NOT NULL CHECK(length(query) BETWEEN 1 AND 1024),
    snapshot_json TEXT NOT NULL CHECK(length(CAST(snapshot_json AS BLOB)) <= 131072),
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    created_at TEXT NOT NULL,
    actor_id TEXT NOT NULL
);
CREATE TRIGGER research_contexts_no_update BEFORE UPDATE ON research_contexts
BEGIN
    SELECT RAISE(ABORT, 'research contexts are immutable');
END;
CREATE TRIGGER research_contexts_no_delete BEFORE DELETE ON research_contexts
BEGIN
    SELECT RAISE(ABORT, 'research contexts are immutable');
END;
DROP TRIGGER artifact_version_sources_insert_guard;
CREATE TRIGGER artifact_version_sources_insert_guard
BEFORE INSERT ON artifact_version_sources
WHEN NOT EXISTS (
    SELECT 1 FROM artifact_versions v JOIN sources s ON s.id = NEW.source_id
    WHERE v.id = NEW.artifact_version_id
      AND v.state = 'pending_materialization' AND v.lineage_sealed = 0
      AND s.import_state IN ('existing', 'imported') AND s.engine_ref IS NOT NULL
      AND (
          EXISTS (SELECT 1 FROM run_source_bindings b
                  WHERE b.run_id = v.run_id AND b.source_id = s.id)
          OR EXISTS (
              SELECT 1 FROM research_contexts c
              JOIN runs r ON r.id = c.run_id AND r.thread_id = c.thread_id
              JOIN json_each(c.snapshot_json, '$.sources') selected
              JOIN adoption_entries e ON e.source_id = s.id AND e.engine_ref = s.engine_ref
              JOIN adoption_manifests m ON m.manifest_id = e.manifest_id
              JOIN asset_roots root ON root.root_id = m.corpus_root_id
              WHERE c.run_id = v.run_id AND s.source_kind = 'paper'
                AND root.root_id = 'research-corpus' AND root.enabled = 1
                AND json_extract(selected.value, '$.source_id') = s.id
                AND json_extract(selected.value, '$.canonical_id') = s.canonical_id
                AND json_extract(selected.value, '$.engine_ref') = s.engine_ref
          )
      )
)
BEGIN SELECT RAISE(ABORT, 'artifact version lineage is sealed'); END;
"""


_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 500),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 500),
    status TEXT NOT NULL DEFAULT 'idle',
    active_run_id TEXT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX threads_workspace_idx ON threads(workspace_id, created_at, id);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL CHECK(length(content) > 0),
    position INTEGER NOT NULL CHECK(position >= 1),
    created_at TEXT NOT NULL,
    UNIQUE(thread_id, position)
);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    active_attempt_id TEXT,
    stage TEXT,
    latest_sequence INTEGER NOT NULL DEFAULT 0 CHECK(latest_sequence >= 0),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX runs_one_active_per_thread_idx
    ON runs(thread_id)
    WHERE state NOT IN ('completed', 'failed', 'canceled');

CREATE TABLE attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    number INTEGER NOT NULL CHECK(number >= 1),
    state TEXT NOT NULL,
    runtime_binding_id TEXT REFERENCES runtime_bindings(id),
    runtime_release_id TEXT,
    checkpoint_uri TEXT,
    source_attempt_id TEXT REFERENCES attempts(id),
    source_checkpoint_uri TEXT,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(run_id, number)
);

CREATE TABLE runtime_bindings (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    adapter_id TEXT NOT NULL,
    runtime_session_ref TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation >= 0),
    adapter_version TEXT NOT NULL,
    parent_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(adapter_id, runtime_session_ref, generation)
);

CREATE TABLE run_events (
    global_cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    attempt_id TEXT REFERENCES attempts(id) ON DELETE SET NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    causation_id TEXT,
    durability TEXT NOT NULL CHECK(durability = 'durable'),
    payload_json TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);
CREATE INDEX run_events_cursor_idx ON run_events(global_cursor);

CREATE TABLE decisions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    prompt TEXT NOT NULL,
    options_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'resolved', 'expired')),
    resolution_json TEXT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX decisions_state_idx ON decisions(state, created_at, id);

CREATE TABLE runtime_actions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    runtime_binding_id TEXT NOT NULL REFERENCES runtime_bindings(id),
    runtime_release_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'acked', 'failed')),
    created_at TEXT NOT NULL,
    acknowledged_at TEXT
);
CREATE INDEX runtime_actions_pending_idx
    ON runtime_actions(state, created_at, id);

CREATE TABLE transport_bindings (
    id TEXT PRIMARY KEY,
    transport TEXT NOT NULL,
    external_scope TEXT NOT NULL,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(transport, external_scope)
);

CREATE TABLE idempotency_receipts (
    actor_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(actor_id, operation, idempotency_key)
);

CREATE TABLE control_audit (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX control_audit_aggregate_idx
    ON control_audit(aggregate_type, aggregate_id, cursor);

CREATE TRIGGER threads_active_run_insert_guard
BEFORE INSERT ON threads
WHEN NEW.active_run_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'active run must be assigned after thread creation');
END;
"""


_MIGRATION_2 = """
ALTER TABLE attempts ADD COLUMN state_generation_id TEXT;
ALTER TABLE attempts ADD COLUMN dispatch_owner TEXT;
ALTER TABLE attempts ADD COLUMN dispatch_expires_at TEXT;
ALTER TABLE decisions ADD COLUMN runtime_decision_ref TEXT;
ALTER TABLE decisions ADD COLUMN runtime_decision_revision INTEGER;
ALTER TABLE runtime_actions ADD COLUMN claim_owner TEXT;
ALTER TABLE runtime_actions ADD COLUMN claim_expires_at TEXT;
ALTER TABLE runtime_actions ADD COLUMN failure_category TEXT;
CREATE INDEX runtime_actions_claim_idx
    ON runtime_actions(state, claim_expires_at, created_at, id);
"""


_MIGRATION_3 = """
ALTER TABLE runtime_actions
    ADD COLUMN claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK(claim_epoch >= 0);
ALTER TABLE runtime_actions
    ADD COLUMN outcome_state TEXT NOT NULL DEFAULT 'ready'
    CHECK(outcome_state IN ('ready', 'outcome_unknown', 'reconcile'));

CREATE TABLE runtime_event_inbox (
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    adapter_event_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY(attempt_id, adapter_event_id)
);

CREATE TABLE runtime_pin_releases (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    runtime_release_id TEXT NOT NULL,
    state_generation_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'acked', 'failed')),
    claim_owner TEXT,
    claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK(claim_epoch >= 0),
    claim_expires_at TEXT,
    failure_category TEXT,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    UNIQUE(attempt_id, runtime_release_id, state_generation_id)
);
CREATE INDEX runtime_pin_releases_pending_idx
    ON runtime_pin_releases(state, claim_expires_at, created_at, id);

CREATE TABLE runtime_recovery_commands (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    runtime_binding_id TEXT REFERENCES runtime_bindings(id),
    runtime_release_id TEXT,
    state_generation_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN (
        'dispatch', 'resume_dispatch', 'inspect_runtime', 'manual_recovery'
    )),
    state TEXT NOT NULL CHECK(state IN (
        'pending', 'completed', 'failed', 'manual_required'
    )),
    claim_owner TEXT,
    claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK(claim_epoch >= 0),
    claim_expires_at TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE UNIQUE INDEX runtime_recovery_one_open_attempt_idx
    ON runtime_recovery_commands(attempt_id)
    WHERE state = 'pending';
CREATE INDEX runtime_recovery_pending_idx
    ON runtime_recovery_commands(state, claim_expires_at, created_at, id);
CREATE INDEX runtime_actions_delivery_idx
    ON runtime_actions(state, outcome_state, claim_expires_at, created_at, id);
"""


_MIGRATION_4 = """
ALTER TABLE attempts
    ADD COLUMN runtime_identity_version INTEGER NOT NULL DEFAULT 0
    CHECK(runtime_identity_version IN (0, 1));
ALTER TABLE attempts ADD COLUMN runtime_slot_id TEXT;
ALTER TABLE attempts ADD COLUMN runtime_artifact_digest TEXT;
ALTER TABLE attempts ADD COLUMN runtime_worker_protocol TEXT;

ALTER TABLE runtime_event_inbox
    ADD COLUMN adapter_event_sequence INTEGER CHECK(adapter_event_sequence >= 0);
CREATE UNIQUE INDEX runtime_event_inbox_sequence_idx
    ON runtime_event_inbox(attempt_id, adapter_event_sequence)
    WHERE adapter_event_sequence IS NOT NULL;

ALTER TABLE runtime_pin_releases ADD COLUMN runtime_slot_id TEXT;
ALTER TABLE runtime_pin_releases ADD COLUMN runtime_artifact_digest TEXT;
ALTER TABLE runtime_pin_releases ADD COLUMN runtime_worker_protocol TEXT;
ALTER TABLE runtime_pin_releases
    ADD COLUMN runtime_identity_version INTEGER NOT NULL DEFAULT 0
    CHECK(runtime_identity_version IN (0, 1));

ALTER TABLE runtime_recovery_commands ADD COLUMN runtime_slot_id TEXT;
ALTER TABLE runtime_recovery_commands ADD COLUMN runtime_artifact_digest TEXT;
ALTER TABLE runtime_recovery_commands ADD COLUMN runtime_worker_protocol TEXT;
ALTER TABLE runtime_recovery_commands
    ADD COLUMN runtime_identity_version INTEGER NOT NULL DEFAULT 0
    CHECK(runtime_identity_version IN (0, 1));
"""


_MIGRATION_5 = """
CREATE TABLE source_intents (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    title_claim TEXT,
    locator_claim TEXT,
    locator_claim_kind TEXT,
    locator_canonical_id TEXT,
    locator_version INTEGER CHECK(locator_version IS NULL OR locator_version >= 1),
    locator_sha256 TEXT,
    resume_run_state TEXT NOT NULL,
    resume_attempt_state TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'resolved', 'canceled')),
    decision_id TEXT REFERENCES decisions(id),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(title_claim IS NOT NULL OR locator_claim IS NOT NULL),
    CHECK(
        (locator_claim IS NULL AND locator_claim_kind IS NULL
            AND locator_canonical_id IS NULL AND locator_version IS NULL
            AND locator_sha256 IS NULL)
        OR
        (locator_claim IS NOT NULL AND locator_claim_kind IS NOT NULL
            AND locator_canonical_id IS NOT NULL)
    )
);
CREATE INDEX source_intents_run_idx
    ON source_intents(run_id, created_at, id);

CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    authority TEXT NOT NULL,
    authority_id TEXT NOT NULL,
    canonical_id TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL,
    official_title TEXT NOT NULL,
    engine_ref TEXT UNIQUE,
    import_state TEXT NOT NULL
        CHECK(import_state IN ('existing', 'pending', 'imported', 'failed')),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(authority, authority_id),
    CHECK(
        (import_state IN ('existing', 'imported') AND engine_ref IS NOT NULL)
        OR
        (import_state IN ('pending', 'failed') AND engine_ref IS NULL)
    )
);

CREATE TABLE source_candidates (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES source_intents(id) ON DELETE CASCADE,
    claim_kind TEXT NOT NULL
        CHECK(claim_kind IN ('title', 'url', 'doi', 'arxiv', 'local_file')),
    authority TEXT NOT NULL,
    authority_id TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    official_title TEXT NOT NULL,
    version INTEGER CHECK(version IS NULL OR version >= 1),
    locator TEXT,
    evidence_json TEXT NOT NULL,
    source_id TEXT REFERENCES sources(id),
    created_at TEXT NOT NULL,
    UNIQUE(intent_id, claim_kind, authority, authority_id)
);
CREATE INDEX source_candidates_intent_idx
    ON source_candidates(intent_id, created_at, id);

CREATE TABLE source_aliases (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    authority TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    display_value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(authority, normalized_value)
);
CREATE INDEX source_aliases_source_idx ON source_aliases(source_id, id);

CREATE TABLE source_resolutions (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE
        REFERENCES source_intents(id) ON DELETE CASCADE,
    decision_id TEXT NOT NULL UNIQUE REFERENCES decisions(id),
    choice TEXT NOT NULL
        CHECK(choice IN (
            'use_source', 'keep_both', 'replace_url_with_echo', 'cancel'
        )),
    selected_candidate_ids_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE run_source_bindings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(id),
    resolution_id TEXT NOT NULL REFERENCES source_resolutions(id),
    disposition TEXT NOT NULL CHECK(disposition IN ('reused', 'imported')),
    created_at TEXT NOT NULL,
    UNIQUE(run_id, source_id)
);
CREATE INDEX run_source_bindings_run_idx
    ON run_source_bindings(run_id, created_at, id);

CREATE TABLE source_import_actions (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL UNIQUE REFERENCES sources(id),
    resolution_id TEXT NOT NULL REFERENCES source_resolutions(id),
    request_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'completed', 'failed', 'canceled')),
    claim_owner TEXT,
    claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK(claim_epoch >= 0),
    claim_expires_at TEXT,
    engine_ref TEXT,
    result_manifest_json TEXT,
    failure_category TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX source_import_actions_delivery_idx
    ON source_import_actions(state, claim_expires_at, created_at, id);

CREATE TABLE source_import_waiters (
    id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES source_import_actions(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    resolution_id TEXT NOT NULL REFERENCES source_resolutions(id),
    state TEXT NOT NULL CHECK(state IN ('waiting', 'bound', 'canceled')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(action_id, run_id)
);
CREATE INDEX source_import_waiters_action_idx
    ON source_import_waiters(action_id, state, created_at, id);
"""


_MIGRATION_6 = """
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK(length(kind) BETWEEN 1 AND 128),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 2000),
    head_artifact_version_id TEXT REFERENCES artifact_versions(id) ON DELETE RESTRICT,
    head_revision INTEGER NOT NULL DEFAULT 0 CHECK(head_revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX artifacts_thread_idx ON artifacts(thread_id, created_at, id);
CREATE INDEX artifacts_workspace_idx ON artifacts(workspace_id, created_at, id);

CREATE TABLE artifact_versions (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE RESTRICT,
    logical_version INTEGER NOT NULL CHECK(logical_version >= 1),
    resource_uri TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK(
        length(sha256) = 64 AND sha256 = lower(sha256)
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    byte_length INTEGER NOT NULL CHECK(byte_length >= 0),
    media_type TEXT NOT NULL CHECK(length(media_type) BETWEEN 3 AND 200),
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
    generator_name TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    lineage_sealed INTEGER NOT NULL DEFAULT 0 CHECK(lineage_sealed IN (0, 1)),
    state TEXT NOT NULL CHECK(state IN (
        'pending_materialization', 'committed', 'failed'
    )),
    provenance_json TEXT,
    created_at TEXT NOT NULL,
    committed_at TEXT,
    UNIQUE(artifact_id, logical_version),
    CHECK(
        (state = 'committed' AND provenance_json IS NOT NULL
            AND committed_at IS NOT NULL)
        OR
        (state IN ('pending_materialization', 'failed')
            AND provenance_json IS NULL AND committed_at IS NULL)
    )
);
CREATE INDEX artifact_versions_artifact_idx
    ON artifact_versions(artifact_id, logical_version, id);
CREATE INDEX artifact_versions_run_idx
    ON artifact_versions(run_id, attempt_id, created_at, id);

CREATE TABLE artifact_version_parents (
    artifact_version_id TEXT NOT NULL
        REFERENCES artifact_versions(id) ON DELETE RESTRICT,
    parent_artifact_version_id TEXT NOT NULL
        REFERENCES artifact_versions(id) ON DELETE RESTRICT,
    expected_sha256 TEXT NOT NULL CHECK(
        length(expected_sha256) = 64 AND expected_sha256 = lower(expected_sha256)
        AND expected_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    position INTEGER NOT NULL CHECK(position >= 0),
    PRIMARY KEY(artifact_version_id, parent_artifact_version_id),
    UNIQUE(artifact_version_id, position),
    CHECK(artifact_version_id != parent_artifact_version_id)
);

CREATE TABLE artifact_version_sources (
    artifact_version_id TEXT NOT NULL
        REFERENCES artifact_versions(id) ON DELETE RESTRICT,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL CHECK(position >= 0),
    PRIMARY KEY(artifact_version_id, source_id),
    UNIQUE(artifact_version_id, position)
);

CREATE TABLE artifact_version_engine_refs (
    artifact_version_id TEXT NOT NULL
        REFERENCES artifact_versions(id) ON DELETE RESTRICT,
    engine_ref TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    PRIMARY KEY(artifact_version_id, engine_ref),
    UNIQUE(artifact_version_id, position)
);

CREATE TABLE artifact_materialization_actions (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    artifact_version_id TEXT NOT NULL UNIQUE
        REFERENCES artifact_versions(id) ON DELETE RESTRICT,
    request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
    root_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK(
        length(sha256) = 64 AND sha256 = lower(sha256)
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    byte_length INTEGER NOT NULL CHECK(byte_length >= 0),
    media_type TEXT NOT NULL,
    advance_head INTEGER NOT NULL CHECK(advance_head IN (0, 1)),
    expected_head_revision INTEGER
        CHECK(expected_head_revision IS NULL OR expected_head_revision >= 0),
    head_advanced INTEGER CHECK(head_advanced IS NULL OR head_advanced IN (0, 1)),
    observed_head_revision INTEGER
        CHECK(observed_head_revision IS NULL OR observed_head_revision >= 0),
    state TEXT NOT NULL CHECK(state IN (
        'pending', 'claimed', 'materialized', 'completed', 'failed'
    )),
    claim_owner TEXT,
    claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK(claim_epoch >= 0),
    claim_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    result_json TEXT,
    failure_category TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK(
        (advance_head = 1 AND expected_head_revision IS NOT NULL)
        OR (advance_head = 0 AND expected_head_revision IS NULL)
    ),
    CHECK(
        (state = 'pending' AND claim_owner IS NULL AND claim_expires_at IS NULL
            AND result_json IS NULL AND completed_at IS NULL
            AND head_advanced IS NULL AND observed_head_revision IS NULL)
        OR (state = 'claimed' AND claim_owner IS NOT NULL
            AND claim_expires_at IS NOT NULL AND result_json IS NULL
            AND completed_at IS NULL
            AND head_advanced IS NULL AND observed_head_revision IS NULL)
        OR (state = 'materialized' AND claim_owner IS NOT NULL
            AND claim_expires_at IS NOT NULL AND result_json IS NOT NULL
            AND failure_category IS NULL AND completed_at IS NULL
            AND head_advanced IS NULL AND observed_head_revision IS NULL)
        OR (state = 'completed' AND claim_owner IS NULL
            AND claim_expires_at IS NULL AND result_json IS NOT NULL
            AND failure_category IS NULL AND completed_at IS NOT NULL
            AND head_advanced IS NOT NULL
            AND (
                (advance_head = 1 AND observed_head_revision IS NOT NULL)
                OR (advance_head = 0 AND head_advanced = 0
                    AND observed_head_revision IS NULL)
            ))
        OR (state = 'failed' AND claim_owner IS NULL
            AND claim_expires_at IS NULL AND result_json IS NULL
            AND failure_category IS NOT NULL AND completed_at IS NOT NULL
            AND head_advanced IS NULL AND observed_head_revision IS NULL)
    )
);
CREATE INDEX artifact_materialization_delivery_idx
    ON artifact_materialization_actions(
        state, claim_expires_at, created_at, id
    );

CREATE TABLE artifact_snapshots (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 2000),
    member_count INTEGER NOT NULL CHECK(member_count BETWEEN 1 AND 500),
    state TEXT NOT NULL CHECK(state IN ('building', 'committed')),
    created_at TEXT NOT NULL
);
CREATE INDEX artifact_snapshots_workspace_idx
    ON artifact_snapshots(workspace_id, created_at, id);

CREATE TABLE artifact_snapshot_members (
    snapshot_id TEXT NOT NULL
        REFERENCES artifact_snapshots(id) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE RESTRICT,
    artifact_version_id TEXT NOT NULL
        REFERENCES artifact_versions(id) ON DELETE RESTRICT,
    logical_version INTEGER NOT NULL CHECK(logical_version >= 1),
    sha256 TEXT NOT NULL CHECK(
        length(sha256) = 64 AND sha256 = lower(sha256)
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    position INTEGER NOT NULL CHECK(position >= 0),
    PRIMARY KEY(snapshot_id, artifact_id),
    UNIQUE(snapshot_id, artifact_version_id),
    UNIQUE(snapshot_id, position)
);

CREATE TRIGGER artifacts_workspace_thread_insert_guard
BEFORE INSERT ON artifacts
WHEN NOT EXISTS (
    SELECT 1 FROM threads
    WHERE threads.id = NEW.thread_id
      AND threads.workspace_id = NEW.workspace_id
)
BEGIN
    SELECT RAISE(ABORT, 'artifact thread does not belong to workspace');
END;

CREATE TRIGGER artifacts_workspace_thread_update_guard
BEFORE UPDATE OF workspace_id, thread_id ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifact ownership is immutable');
END;

CREATE TRIGGER artifacts_delete_guard
BEFORE DELETE ON artifacts
BEGIN SELECT RAISE(ABORT, 'artifact deletion is not supported'); END;

CREATE TRIGGER artifacts_head_insert_guard
BEFORE INSERT ON artifacts
WHEN NEW.head_artifact_version_id IS NOT NULL OR NEW.head_revision != 0
BEGIN
    SELECT RAISE(ABORT, 'artifact head must be assigned after creation');
END;

CREATE TRIGGER artifacts_head_update_guard
BEFORE UPDATE OF head_artifact_version_id ON artifacts
WHEN NEW.head_artifact_version_id IS NULL
  OR NEW.head_artifact_version_id IS OLD.head_artifact_version_id
  OR NEW.head_revision != OLD.head_revision + 1
  OR NOT EXISTS (
      SELECT 1 FROM artifact_versions
      WHERE artifact_versions.id = NEW.head_artifact_version_id
        AND artifact_versions.artifact_id = NEW.id
        AND artifact_versions.state = 'committed'
  )
BEGIN
    SELECT RAISE(ABORT, 'artifact head must advance to its committed version');
END;

CREATE TRIGGER artifacts_head_revision_guard
BEFORE UPDATE OF head_revision ON artifacts
WHEN NEW.head_revision != OLD.head_revision + 1
  OR NEW.head_artifact_version_id IS OLD.head_artifact_version_id
BEGIN
    SELECT RAISE(ABORT, 'artifact head revision must advance with its version');
END;

CREATE TRIGGER artifact_versions_ownership_insert_guard
BEFORE INSERT ON artifact_versions
WHEN NEW.state != 'pending_materialization'
  OR NEW.lineage_sealed != 0
  OR NEW.provenance_json IS NOT NULL
  OR NEW.committed_at IS NOT NULL
  OR NOT EXISTS (
    SELECT 1
    FROM artifacts
    JOIN runs ON runs.id = NEW.run_id
    JOIN attempts ON attempts.id = NEW.attempt_id
    JOIN threads ON threads.id = runs.thread_id
    WHERE artifacts.id = NEW.artifact_id
      AND runs.thread_id = artifacts.thread_id
      AND attempts.run_id = runs.id
      AND runs.active_attempt_id = attempts.id
      AND threads.active_run_id = runs.id
      AND runs.state IN (
          'queued', 'starting', 'running',
          'waiting_for_decision', 'resuming', 'retrying'
      )
      AND attempts.state IN (
          'queued', 'starting', 'running',
          'waiting_for_decision', 'resuming', 'retrying'
      )
)
BEGIN
    SELECT RAISE(ABORT, 'artifact version run ownership is invalid');
END;

CREATE TRIGGER artifact_versions_immutable_inputs_guard
BEFORE UPDATE ON artifact_versions
WHEN NEW.id IS NOT OLD.id
  OR NEW.artifact_id IS NOT OLD.artifact_id
  OR NEW.logical_version IS NOT OLD.logical_version
  OR NEW.resource_uri IS NOT OLD.resource_uri
  OR NEW.sha256 IS NOT OLD.sha256
  OR NEW.byte_length IS NOT OLD.byte_length
  OR NEW.media_type IS NOT OLD.media_type
  OR NEW.run_id IS NOT OLD.run_id
  OR NEW.attempt_id IS NOT OLD.attempt_id
  OR NEW.generator_name IS NOT OLD.generator_name
  OR NEW.generator_version IS NOT OLD.generator_version
  OR NEW.tool_name IS NOT OLD.tool_name
  OR NEW.tool_version IS NOT OLD.tool_version
  OR OLD.state = 'committed'
BEGIN
    SELECT RAISE(ABORT, 'artifact version inputs are immutable');
END;

CREATE TRIGGER artifact_versions_state_guard
BEFORE UPDATE OF state ON artifact_versions
WHEN NOT (
    (OLD.state = 'pending_materialization' AND NEW.state = 'committed'
        AND OLD.lineage_sealed = 1
        AND EXISTS (
            SELECT 1 FROM artifact_materialization_actions
            WHERE artifact_version_id = OLD.id AND state = 'materialized'
        ))
    OR (OLD.state = 'pending_materialization' AND NEW.state = 'failed'
        AND EXISTS (
            SELECT 1 FROM artifact_materialization_actions
            WHERE artifact_version_id = OLD.id AND state = 'failed'
        ))
    OR (OLD.state = 'failed' AND NEW.state = 'pending_materialization'
        AND EXISTS (
            SELECT 1 FROM artifact_materialization_actions
            WHERE artifact_version_id = OLD.id AND state = 'pending'
        ))
)
BEGIN
    SELECT RAISE(ABORT, 'artifact version state transition is invalid');
END;

CREATE TRIGGER artifact_versions_delete_guard
BEFORE DELETE ON artifact_versions
BEGIN SELECT RAISE(ABORT, 'artifact version deletion is not supported'); END;

CREATE TRIGGER artifact_versions_lineage_seal_guard
BEFORE UPDATE OF lineage_sealed ON artifact_versions
WHEN NOT (
    OLD.lineage_sealed = 0 AND NEW.lineage_sealed = 1
    AND OLD.state = 'pending_materialization'
)
BEGIN
    SELECT RAISE(ABORT, 'artifact version lineage seal is immutable');
END;

CREATE TRIGGER artifact_version_parents_insert_guard
BEFORE INSERT ON artifact_version_parents
WHEN NOT EXISTS (
    SELECT 1
    FROM artifact_versions AS child_version
    JOIN artifacts AS child_artifact
      ON child_artifact.id = child_version.artifact_id
    JOIN artifact_versions AS parent_version
      ON parent_version.id = NEW.parent_artifact_version_id
    JOIN artifacts AS parent_artifact
      ON parent_artifact.id = parent_version.artifact_id
    WHERE child_version.id = NEW.artifact_version_id
      AND child_version.state = 'pending_materialization'
      AND child_version.lineage_sealed = 0
      AND parent_version.state = 'committed'
      AND parent_version.sha256 = NEW.expected_sha256
      AND parent_artifact.workspace_id = child_artifact.workspace_id
      AND parent_artifact.thread_id = child_artifact.thread_id
)
BEGIN SELECT RAISE(ABORT, 'artifact version lineage is sealed'); END;

CREATE TRIGGER artifact_version_parents_update_guard
BEFORE UPDATE ON artifact_version_parents
BEGIN SELECT RAISE(ABORT, 'artifact version parents are immutable'); END;
CREATE TRIGGER artifact_version_parents_delete_guard
BEFORE DELETE ON artifact_version_parents
BEGIN SELECT RAISE(ABORT, 'artifact version parents are immutable'); END;
CREATE TRIGGER artifact_version_sources_update_guard
BEFORE UPDATE ON artifact_version_sources
BEGIN SELECT RAISE(ABORT, 'artifact version sources are immutable'); END;
CREATE TRIGGER artifact_version_sources_insert_guard
BEFORE INSERT ON artifact_version_sources
WHEN NOT EXISTS (
    SELECT 1
    FROM artifact_versions
    JOIN run_source_bindings
      ON run_source_bindings.run_id = artifact_versions.run_id
     AND run_source_bindings.source_id = NEW.source_id
    JOIN sources ON sources.id = NEW.source_id
    WHERE artifact_versions.id = NEW.artifact_version_id
      AND artifact_versions.state = 'pending_materialization'
      AND artifact_versions.lineage_sealed = 0
      AND sources.import_state IN ('existing', 'imported')
      AND sources.engine_ref IS NOT NULL
)
BEGIN SELECT RAISE(ABORT, 'artifact version lineage is sealed'); END;
CREATE TRIGGER artifact_version_sources_delete_guard
BEFORE DELETE ON artifact_version_sources
BEGIN SELECT RAISE(ABORT, 'artifact version sources are immutable'); END;
CREATE TRIGGER artifact_version_engine_refs_update_guard
BEFORE UPDATE ON artifact_version_engine_refs
BEGIN SELECT RAISE(ABORT, 'artifact version engine refs are immutable'); END;
CREATE TRIGGER artifact_version_engine_refs_insert_guard
BEFORE INSERT ON artifact_version_engine_refs
WHEN NOT EXISTS (
    SELECT 1
    FROM artifact_versions
    JOIN artifact_version_sources
      ON artifact_version_sources.artifact_version_id = artifact_versions.id
    JOIN sources ON sources.id = artifact_version_sources.source_id
    WHERE artifact_versions.id = NEW.artifact_version_id
      AND artifact_versions.state = 'pending_materialization'
      AND artifact_versions.lineage_sealed = 0
      AND sources.engine_ref = NEW.engine_ref
)
BEGIN SELECT RAISE(ABORT, 'artifact version lineage is sealed'); END;
CREATE TRIGGER artifact_version_engine_refs_delete_guard
BEFORE DELETE ON artifact_version_engine_refs
BEGIN SELECT RAISE(ABORT, 'artifact version engine refs are immutable'); END;

CREATE TRIGGER artifact_materialization_request_guard
BEFORE UPDATE ON artifact_materialization_actions
WHEN NEW.id IS NOT OLD.id
  OR NEW.operation_id IS NOT OLD.operation_id
  OR NEW.artifact_version_id IS NOT OLD.artifact_version_id
  OR NEW.request_hash IS NOT OLD.request_hash
  OR NEW.root_id IS NOT OLD.root_id
  OR NEW.relative_path IS NOT OLD.relative_path
  OR NEW.sha256 IS NOT OLD.sha256
  OR NEW.byte_length IS NOT OLD.byte_length
  OR NEW.media_type IS NOT OLD.media_type
  OR NEW.advance_head IS NOT OLD.advance_head
  OR NEW.expected_head_revision IS NOT OLD.expected_head_revision
  OR OLD.state = 'completed'
BEGIN
    SELECT RAISE(ABORT, 'artifact materialization request is immutable');
END;

CREATE TRIGGER artifact_materialization_insert_guard
BEFORE INSERT ON artifact_materialization_actions
WHEN NEW.state != 'pending'
  OR NEW.claim_owner IS NOT NULL
  OR NEW.claim_epoch != 0
  OR NEW.claim_expires_at IS NOT NULL
  OR NEW.attempt_count != 0
  OR NEW.result_json IS NOT NULL
  OR NEW.head_advanced IS NOT NULL
  OR NEW.observed_head_revision IS NOT NULL
  OR NEW.failure_category IS NOT NULL
  OR NEW.completed_at IS NOT NULL
  OR NOT EXISTS (
      SELECT 1 FROM artifact_versions
      WHERE id = NEW.artifact_version_id
        AND state = 'pending_materialization'
        AND lineage_sealed = 1
        AND sha256 = NEW.sha256
        AND byte_length = NEW.byte_length
        AND media_type = NEW.media_type
  )
BEGIN
    SELECT RAISE(ABORT, 'artifact materialization must start pending');
END;

CREATE TRIGGER artifact_materialization_state_guard
BEFORE UPDATE OF state ON artifact_materialization_actions
WHEN NOT (
    (OLD.state = 'pending' AND NEW.state = 'claimed'
        AND EXISTS (
            SELECT 1 FROM artifact_versions
            WHERE id = OLD.artifact_version_id
              AND state = 'pending_materialization'
        ))
    OR (OLD.state = 'claimed' AND NEW.state = 'claimed'
        AND EXISTS (
            SELECT 1 FROM artifact_versions
            WHERE id = OLD.artifact_version_id
              AND state = 'pending_materialization'
        ))
    OR (OLD.state = 'claimed' AND NEW.state = 'materialized'
        AND EXISTS (
            SELECT 1 FROM artifact_versions
            WHERE id = OLD.artifact_version_id
              AND state = 'pending_materialization'
        ))
    OR (OLD.state = 'materialized' AND NEW.state = 'completed'
        AND EXISTS (
            SELECT 1 FROM artifact_versions
            WHERE id = OLD.artifact_version_id AND state = 'committed'
        ))
    OR (OLD.state IN ('pending', 'claimed') AND NEW.state = 'failed'
        AND EXISTS (
            SELECT 1 FROM artifact_versions
            WHERE id = OLD.artifact_version_id
              AND state = 'pending_materialization'
        ))
    OR (OLD.state = 'failed' AND NEW.state = 'pending'
        AND EXISTS (
            SELECT 1 FROM artifact_versions
            WHERE id = OLD.artifact_version_id AND state = 'failed'
        ))
)
BEGIN
    SELECT RAISE(ABORT, 'artifact materialization state transition is invalid');
END;

CREATE TRIGGER artifact_materialization_delete_guard
BEFORE DELETE ON artifact_materialization_actions
BEGIN SELECT RAISE(ABORT, 'artifact materialization deletion is not supported'); END;

CREATE TRIGGER artifact_snapshots_update_guard
BEFORE UPDATE ON artifact_snapshots
WHEN NOT (
    OLD.state = 'building' AND NEW.state = 'committed'
    AND NEW.id IS OLD.id AND NEW.workspace_id IS OLD.workspace_id
    AND NEW.name IS OLD.name AND NEW.member_count IS OLD.member_count
    AND NEW.created_at IS OLD.created_at
    AND (SELECT COUNT(*) FROM artifact_snapshot_members
         WHERE snapshot_id = OLD.id) = OLD.member_count
)
BEGIN SELECT RAISE(ABORT, 'artifact snapshots are immutable'); END;
CREATE TRIGGER artifact_snapshots_insert_guard
BEFORE INSERT ON artifact_snapshots
WHEN NEW.state != 'building'
BEGIN SELECT RAISE(ABORT, 'artifact snapshot must start building'); END;
CREATE TRIGGER artifact_snapshots_delete_guard
BEFORE DELETE ON artifact_snapshots
BEGIN SELECT RAISE(ABORT, 'artifact snapshots are immutable'); END;
CREATE TRIGGER artifact_snapshot_members_update_guard
BEFORE UPDATE ON artifact_snapshot_members
BEGIN SELECT RAISE(ABORT, 'artifact snapshot members are immutable'); END;
CREATE TRIGGER artifact_snapshot_members_insert_guard
BEFORE INSERT ON artifact_snapshot_members
WHEN NOT EXISTS (
    SELECT 1
    FROM artifact_snapshots
    JOIN artifacts ON artifacts.id = NEW.artifact_id
    JOIN artifact_versions
      ON artifact_versions.id = NEW.artifact_version_id
     AND artifact_versions.artifact_id = artifacts.id
    WHERE artifact_snapshots.id = NEW.snapshot_id
      AND artifact_snapshots.state = 'building'
      AND artifacts.workspace_id = artifact_snapshots.workspace_id
      AND artifact_versions.state = 'committed'
      AND artifact_versions.logical_version = NEW.logical_version
      AND artifact_versions.sha256 = NEW.sha256
      AND NEW.position >= 0
      AND NEW.position < artifact_snapshots.member_count
)
BEGIN SELECT RAISE(ABORT, 'artifact snapshot is sealed'); END;
CREATE TRIGGER artifact_snapshot_members_delete_guard
BEFORE DELETE ON artifact_snapshot_members
BEGIN SELECT RAISE(ABORT, 'artifact snapshot members are immutable'); END;
"""


_MIGRATION_7 = """
CREATE TABLE workflow_instances (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE RESTRICT,
    definition_id TEXT NOT NULL,
    definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
    definition_sealed INTEGER NOT NULL DEFAULT 0
        CHECK(definition_sealed IN (0, 1)),
    state TEXT NOT NULL CHECK(state IN (
        'running', 'waiting', 'completed', 'failed', 'manual_recovery'
    )),
    current_stage_key TEXT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(state != 'completed' OR current_stage_key IS NULL),
    FOREIGN KEY(id, current_stage_key)
        REFERENCES workflow_stage_instances(workflow_id, stage_key)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX workflow_instances_state_idx
    ON workflow_instances(state, updated_at, id);

CREATE TABLE workflow_stage_instances (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL
        REFERENCES workflow_instances(id) ON DELETE RESTRICT,
    stage_key TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    effect TEXT NOT NULL CHECK(effect IN (
        'control', 'decision', 'engine_query',
        'engine_mutation', 'runtime', 'artifact'
    )),
    checkpoint_enabled INTEGER NOT NULL CHECK(checkpoint_enabled IN (0, 1)),
    input_hash TEXT CHECK(
        input_hash IS NULL OR (
            length(input_hash) = 64 AND input_hash = lower(input_hash)
            AND input_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    state TEXT NOT NULL CHECK(state IN (
        'pending', 'ready', 'active', 'waiting',
        'completed', 'failed', 'manual_recovery'
    )),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(workflow_id, stage_key),
    UNIQUE(workflow_id, position),
    UNIQUE(workflow_id, id),
    CHECK(
        (state IN ('pending', 'ready') AND input_hash IS NULL
            AND started_at IS NULL AND completed_at IS NULL)
        OR (state IN ('active', 'waiting', 'failed', 'manual_recovery')
            AND input_hash IS NOT NULL AND started_at IS NOT NULL
            AND completed_at IS NULL)
        OR (state = 'completed' AND input_hash IS NOT NULL
            AND started_at IS NOT NULL AND completed_at IS NOT NULL)
    )
);
CREATE INDEX workflow_stage_instances_state_idx
    ON workflow_stage_instances(workflow_id, state, position, id);

CREATE TABLE workflow_stage_dependencies (
    workflow_id TEXT NOT NULL
        REFERENCES workflow_instances(id) ON DELETE RESTRICT,
    stage_id TEXT NOT NULL,
    dependency_stage_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    PRIMARY KEY(stage_id, dependency_stage_id),
    UNIQUE(stage_id, position),
    CHECK(stage_id != dependency_stage_id),
    FOREIGN KEY(workflow_id, stage_id)
        REFERENCES workflow_stage_instances(workflow_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(workflow_id, dependency_stage_id)
        REFERENCES workflow_stage_instances(workflow_id, id) ON DELETE RESTRICT
);

CREATE TABLE workflow_stage_receipt_requirements (
    workflow_id TEXT NOT NULL
        REFERENCES workflow_instances(id) ON DELETE RESTRICT,
    stage_id TEXT NOT NULL,
    effect_kind TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    PRIMARY KEY(stage_id, effect_kind),
    UNIQUE(stage_id, position),
    FOREIGN KEY(workflow_id, stage_id)
        REFERENCES workflow_stage_instances(workflow_id, id) ON DELETE RESTRICT
);

CREATE TABLE workflow_stage_result_requirements (
    workflow_id TEXT NOT NULL
        REFERENCES workflow_instances(id) ON DELETE RESTRICT,
    stage_id TEXT NOT NULL,
    result_kind TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    PRIMARY KEY(stage_id, result_kind),
    UNIQUE(stage_id, position),
    FOREIGN KEY(workflow_id, stage_id)
        REFERENCES workflow_stage_instances(workflow_id, id) ON DELETE RESTRICT
);

CREATE TABLE workflow_decision_refs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL
        REFERENCES workflow_instances(id) ON DELETE RESTRICT,
    stage_id TEXT NOT NULL UNIQUE,
    decision_id TEXT NOT NULL UNIQUE REFERENCES decisions(id) ON DELETE RESTRICT,
    expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
    resolved_revision INTEGER CHECK(resolved_revision IS NULL OR resolved_revision >= 0),
    selected_choice TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending', 'resolved')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(workflow_id, stage_id)
        REFERENCES workflow_stage_instances(workflow_id, id) ON DELETE RESTRICT,
    CHECK(
        (state = 'pending' AND resolved_revision IS NULL
            AND selected_choice IS NULL AND resolved_at IS NULL)
        OR (state = 'resolved' AND resolved_revision IS NOT NULL
            AND selected_choice IS NOT NULL AND resolved_at IS NOT NULL)
    )
);

CREATE TABLE workflow_effect_commands (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    workflow_id TEXT NOT NULL
        REFERENCES workflow_instances(id) ON DELETE RESTRICT,
    stage_id TEXT NOT NULL,
    effect_class TEXT NOT NULL CHECK(effect_class IN ('mutation', 'query')),
    effect_kind TEXT NOT NULL,
    effect_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK(
        length(request_hash) = 64 AND request_hash = lower(request_hash)
        AND request_hash NOT GLOB '*[^0-9a-f]*'
    ),
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    state TEXT NOT NULL CHECK(state IN (
        'pending', 'claimed', 'outcome_unknown', 'reconciling',
        'completed', 'failed', 'manual_required'
    )),
    claim_owner TEXT,
    claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK(claim_epoch >= 0),
    delivery_epoch INTEGER NOT NULL DEFAULT 0 CHECK(delivery_epoch >= 0),
    claim_expires_at TEXT,
    result_identity TEXT CHECK(
        result_identity IS NULL OR (
            length(result_identity) = 64 AND result_identity = lower(result_identity)
            AND result_identity NOT GLOB '*[^0-9a-f]*'
        )
    ),
    receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
    failure_category TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(workflow_id, stage_id, effect_kind, effect_key),
    FOREIGN KEY(workflow_id, stage_id)
        REFERENCES workflow_stage_instances(workflow_id, id) ON DELETE RESTRICT,
    CHECK(
        (state = 'pending' AND claim_owner IS NULL AND claim_expires_at IS NULL
            AND result_identity IS NULL AND receipt_json IS NULL
            AND failure_category IS NULL AND completed_at IS NULL)
        OR (state IN ('claimed', 'reconciling') AND claim_owner IS NOT NULL
            AND claim_expires_at IS NOT NULL AND result_identity IS NULL
            AND receipt_json IS NULL AND failure_category IS NULL
            AND completed_at IS NULL)
        OR (state = 'outcome_unknown' AND claim_owner IS NULL
            AND claim_expires_at IS NULL AND result_identity IS NULL
            AND receipt_json IS NULL AND failure_category IS NULL
            AND completed_at IS NULL)
        OR (state = 'completed' AND claim_owner IS NULL
            AND claim_expires_at IS NULL AND result_identity IS NOT NULL
            AND receipt_json IS NOT NULL AND failure_category IS NULL
            AND completed_at IS NOT NULL)
        OR (state IN ('failed', 'manual_required') AND claim_owner IS NULL
            AND claim_expires_at IS NULL AND result_identity IS NULL
            AND receipt_json IS NULL AND failure_category IS NOT NULL
            AND completed_at IS NOT NULL)
    )
);
CREATE INDEX workflow_effect_commands_delivery_idx
    ON workflow_effect_commands(
        state, claim_expires_at, created_at, id
    );

CREATE TABLE workflow_stage_references (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL
        REFERENCES workflow_instances(id) ON DELETE RESTRICT,
    stage_id TEXT NOT NULL,
    reference_kind TEXT NOT NULL CHECK(reference_kind IN (
        'decision', 'source_resolution', 'source', 'source_binding',
        'engine_source', 'lineage_node', 'artifact_version',
        'snapshot', 'event', 'runtime_event'
    )),
    reference_id TEXT NOT NULL,
    identity_hash TEXT NOT NULL CHECK(
        length(identity_hash) = 64 AND identity_hash = lower(identity_hash)
        AND identity_hash NOT GLOB '*[^0-9a-f]*'
    ),
    metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
    created_at TEXT NOT NULL,
    UNIQUE(workflow_id, stage_id, reference_kind, reference_id),
    FOREIGN KEY(workflow_id, stage_id)
        REFERENCES workflow_stage_instances(workflow_id, id) ON DELETE RESTRICT
);
CREATE INDEX workflow_stage_references_stage_idx
    ON workflow_stage_references(stage_id, reference_kind, reference_id);

CREATE TABLE workflow_checkpoints (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL
        REFERENCES workflow_instances(id) ON DELETE RESTRICT,
    stage_id TEXT NOT NULL UNIQUE,
    workflow_revision INTEGER NOT NULL CHECK(workflow_revision >= 0),
    state_hash TEXT NOT NULL CHECK(
        length(state_hash) = 64 AND state_hash = lower(state_hash)
        AND state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    state_json TEXT NOT NULL CHECK(json_valid(state_json)),
    references_hash TEXT NOT NULL CHECK(
        length(references_hash) = 64 AND references_hash = lower(references_hash)
        AND references_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    FOREIGN KEY(workflow_id, stage_id)
        REFERENCES workflow_stage_instances(workflow_id, id) ON DELETE RESTRICT
);

CREATE TRIGGER workflow_instances_identity_guard
BEFORE UPDATE OF id, run_id, definition_id, definition_version
ON workflow_instances
BEGIN SELECT RAISE(ABORT, 'workflow identity is immutable'); END;

CREATE TRIGGER workflow_instances_initial_seal_guard
BEFORE INSERT ON workflow_instances
WHEN NEW.definition_sealed != 0
BEGIN SELECT RAISE(ABORT, 'workflow definition must start unsealed'); END;

CREATE TRIGGER workflow_instances_replacement_guard
BEFORE INSERT ON workflow_instances
WHEN EXISTS (
    SELECT 1 FROM workflow_instances
    WHERE id = NEW.id OR run_id = NEW.run_id
)
BEGIN SELECT RAISE(ABORT, 'workflow replacement is not supported'); END;

CREATE TRIGGER workflow_instances_seal_guard
BEFORE UPDATE OF definition_sealed ON workflow_instances
WHEN OLD.definition_sealed = 1 OR NEW.definition_sealed != 1
BEGIN SELECT RAISE(ABORT, 'workflow definition seal is irreversible'); END;

CREATE TRIGGER workflow_instances_seal_completeness_guard
BEFORE UPDATE OF definition_sealed ON workflow_instances
WHEN OLD.definition_sealed = 0 AND NEW.definition_sealed = 1 AND (
    NEW.current_stage_key IS NULL
    OR NOT EXISTS (
        SELECT 1 FROM workflow_stage_instances
        WHERE workflow_id = NEW.id
          AND stage_key = NEW.current_stage_key
          AND state = 'ready'
    )
    OR NOT EXISTS (
        SELECT 1 FROM workflow_stage_instances WHERE workflow_id = NEW.id
    )
    OR EXISTS (
        SELECT 1
        FROM workflow_stage_instances AS stage
        WHERE stage.workflow_id = NEW.id AND (
            (stage.effect IN ('engine_mutation', 'runtime', 'artifact') AND (
                NOT EXISTS (
                    SELECT 1 FROM workflow_stage_receipt_requirements
                    WHERE workflow_id = NEW.id AND stage_id = stage.id
                )
                OR EXISTS (
                    SELECT 1 FROM workflow_stage_result_requirements
                    WHERE workflow_id = NEW.id AND stage_id = stage.id
                )
            ))
            OR (stage.effect = 'engine_query' AND (
                NOT EXISTS (
                    SELECT 1 FROM workflow_stage_result_requirements
                    WHERE workflow_id = NEW.id AND stage_id = stage.id
                )
                OR EXISTS (
                    SELECT 1 FROM workflow_stage_receipt_requirements
                    WHERE workflow_id = NEW.id AND stage_id = stage.id
                )
            ))
            OR (stage.effect IN ('control', 'decision') AND (
                EXISTS (
                    SELECT 1 FROM workflow_stage_receipt_requirements
                    WHERE workflow_id = NEW.id AND stage_id = stage.id
                )
                OR EXISTS (
                    SELECT 1 FROM workflow_stage_result_requirements
                    WHERE workflow_id = NEW.id AND stage_id = stage.id
                )
            ))
        )
    )
)
BEGIN SELECT RAISE(ABORT, 'workflow definition is incomplete'); END;

CREATE TRIGGER workflow_instances_delete_guard
BEFORE DELETE ON workflow_instances
BEGIN SELECT RAISE(ABORT, 'workflow deletion is not supported'); END;

CREATE TRIGGER workflow_stage_insert_guard
BEFORE INSERT ON workflow_stage_instances
WHEN NOT EXISTS (
    SELECT 1 FROM workflow_instances
    WHERE id = NEW.workflow_id AND definition_sealed = 0
)
BEGIN SELECT RAISE(ABORT, 'workflow definition is sealed'); END;

CREATE TRIGGER workflow_stage_replacement_guard
BEFORE INSERT ON workflow_stage_instances
WHEN EXISTS (
    SELECT 1
    FROM workflow_stage_instances
    WHERE id = NEW.id
       OR (workflow_id = NEW.workflow_id AND stage_key = NEW.stage_key)
       OR (workflow_id = NEW.workflow_id AND position = NEW.position)
)
BEGIN SELECT RAISE(ABORT, 'workflow stage replacement is not supported'); END;

CREATE TRIGGER workflow_stage_definition_guard
BEFORE UPDATE OF id, workflow_id, stage_key, position, effect, checkpoint_enabled
ON workflow_stage_instances
BEGIN SELECT RAISE(ABORT, 'workflow stage definition is immutable'); END;

CREATE TRIGGER workflow_stage_delete_guard
BEFORE DELETE ON workflow_stage_instances
BEGIN SELECT RAISE(ABORT, 'workflow stage deletion is not supported'); END;

CREATE TRIGGER workflow_stage_dependencies_insert_guard
BEFORE INSERT ON workflow_stage_dependencies
WHEN NOT EXISTS (
    SELECT 1
    FROM workflow_instances
    JOIN workflow_stage_instances AS stage
      ON stage.workflow_id = workflow_instances.id
     AND stage.id = NEW.stage_id
    JOIN workflow_stage_instances AS dependency
      ON dependency.workflow_id = workflow_instances.id
     AND dependency.id = NEW.dependency_stage_id
     AND dependency.position < stage.position
    WHERE workflow_instances.id = NEW.workflow_id
      AND workflow_instances.definition_sealed = 0
)
BEGIN SELECT RAISE(ABORT, 'workflow dependency is invalid'); END;
CREATE TRIGGER workflow_stage_dependencies_replacement_guard
BEFORE INSERT ON workflow_stage_dependencies
WHEN EXISTS (
    SELECT 1
    FROM workflow_stage_dependencies
    WHERE (
        stage_id = NEW.stage_id
        AND dependency_stage_id = NEW.dependency_stage_id
    ) OR (stage_id = NEW.stage_id AND position = NEW.position)
)
BEGIN SELECT RAISE(ABORT, 'workflow dependency replacement is not supported'); END;
CREATE TRIGGER workflow_stage_dependencies_update_guard
BEFORE UPDATE ON workflow_stage_dependencies
BEGIN SELECT RAISE(ABORT, 'workflow dependencies are immutable'); END;
CREATE TRIGGER workflow_stage_dependencies_delete_guard
BEFORE DELETE ON workflow_stage_dependencies
BEGIN SELECT RAISE(ABORT, 'workflow dependencies are immutable'); END;

CREATE TRIGGER workflow_receipt_requirements_insert_guard
BEFORE INSERT ON workflow_stage_receipt_requirements
WHEN NOT EXISTS (
    SELECT 1
    FROM workflow_instances
    JOIN workflow_stage_instances
      ON workflow_stage_instances.workflow_id = workflow_instances.id
     AND workflow_stage_instances.id = NEW.stage_id
     AND workflow_stage_instances.effect IN (
         'engine_mutation', 'runtime', 'artifact'
     )
    WHERE workflow_instances.id = NEW.workflow_id
      AND workflow_instances.definition_sealed = 0
)
BEGIN SELECT RAISE(ABORT, 'workflow receipt requirement is invalid'); END;
CREATE TRIGGER workflow_receipt_requirements_replacement_guard
BEFORE INSERT ON workflow_stage_receipt_requirements
WHEN EXISTS (
    SELECT 1
    FROM workflow_stage_receipt_requirements
    WHERE (stage_id = NEW.stage_id AND effect_kind = NEW.effect_kind)
       OR (stage_id = NEW.stage_id AND position = NEW.position)
)
BEGIN SELECT RAISE(ABORT, 'workflow receipt replacement is not supported'); END;
CREATE TRIGGER workflow_receipt_requirements_update_guard
BEFORE UPDATE ON workflow_stage_receipt_requirements
BEGIN SELECT RAISE(ABORT, 'workflow receipt requirements are immutable'); END;
CREATE TRIGGER workflow_receipt_requirements_delete_guard
BEFORE DELETE ON workflow_stage_receipt_requirements
BEGIN SELECT RAISE(ABORT, 'workflow receipt requirements are immutable'); END;

CREATE TRIGGER workflow_result_requirements_insert_guard
BEFORE INSERT ON workflow_stage_result_requirements
WHEN NOT EXISTS (
    SELECT 1
    FROM workflow_instances
    JOIN workflow_stage_instances
      ON workflow_stage_instances.workflow_id = workflow_instances.id
     AND workflow_stage_instances.id = NEW.stage_id
     AND workflow_stage_instances.effect = 'engine_query'
    WHERE workflow_instances.id = NEW.workflow_id
      AND workflow_instances.definition_sealed = 0
)
BEGIN SELECT RAISE(ABORT, 'workflow result requirement is invalid'); END;
CREATE TRIGGER workflow_result_requirements_replacement_guard
BEFORE INSERT ON workflow_stage_result_requirements
WHEN EXISTS (
    SELECT 1
    FROM workflow_stage_result_requirements
    WHERE (stage_id = NEW.stage_id AND result_kind = NEW.result_kind)
       OR (stage_id = NEW.stage_id AND position = NEW.position)
)
BEGIN SELECT RAISE(ABORT, 'workflow result replacement is not supported'); END;
CREATE TRIGGER workflow_result_requirements_update_guard
BEFORE UPDATE ON workflow_stage_result_requirements
BEGIN SELECT RAISE(ABORT, 'workflow result requirements are immutable'); END;
CREATE TRIGGER workflow_result_requirements_delete_guard
BEFORE DELETE ON workflow_stage_result_requirements
BEGIN SELECT RAISE(ABORT, 'workflow result requirements are immutable'); END;

CREATE TRIGGER workflow_decision_refs_insert_guard
BEFORE INSERT ON workflow_decision_refs
WHEN NOT EXISTS (
    SELECT 1
    FROM workflow_instances
    JOIN workflow_stage_instances
      ON workflow_stage_instances.workflow_id = workflow_instances.id
     AND workflow_stage_instances.id = NEW.stage_id
     AND workflow_stage_instances.effect = 'decision'
    JOIN decisions
      ON decisions.id = NEW.decision_id
     AND decisions.run_id = workflow_instances.run_id
    JOIN attempts
      ON attempts.id = decisions.attempt_id
     AND attempts.run_id = workflow_instances.run_id
    WHERE workflow_instances.id = NEW.workflow_id
)
BEGIN SELECT RAISE(ABORT, 'workflow decision reference is inconsistent'); END;

CREATE TRIGGER workflow_decision_refs_replacement_guard
BEFORE INSERT ON workflow_decision_refs
WHEN EXISTS (
    SELECT 1
    FROM workflow_decision_refs
    WHERE id = NEW.id OR stage_id = NEW.stage_id OR decision_id = NEW.decision_id
)
BEGIN SELECT RAISE(ABORT, 'workflow decision replacement is not supported'); END;

CREATE TRIGGER workflow_decision_refs_identity_guard
BEFORE UPDATE OF id, workflow_id, stage_id, decision_id, expected_revision,
    created_at
ON workflow_decision_refs
BEGIN SELECT RAISE(ABORT, 'workflow decision reference identity is immutable'); END;

CREATE TRIGGER workflow_decision_refs_terminal_guard
BEFORE UPDATE ON workflow_decision_refs
WHEN OLD.state = 'resolved'
BEGIN SELECT RAISE(ABORT, 'terminal workflow decision is immutable'); END;

CREATE TRIGGER workflow_decision_refs_delete_guard
BEFORE DELETE ON workflow_decision_refs
BEGIN SELECT RAISE(ABORT, 'workflow decision deletion is not supported'); END;

CREATE TRIGGER workflow_effect_commands_insert_guard
BEFORE INSERT ON workflow_effect_commands
WHEN
    (NEW.effect_class = 'mutation' AND NOT EXISTS (
        SELECT 1 FROM workflow_stage_receipt_requirements
        WHERE workflow_id = NEW.workflow_id
          AND stage_id = NEW.stage_id
          AND effect_kind = NEW.effect_kind
    ))
    OR (NEW.effect_class = 'query' AND NOT EXISTS (
        SELECT 1 FROM workflow_stage_result_requirements
        WHERE workflow_id = NEW.workflow_id
          AND stage_id = NEW.stage_id
          AND result_kind = NEW.effect_kind
    ))
BEGIN SELECT RAISE(ABORT, 'workflow effect is not required by the stage'); END;

CREATE TRIGGER workflow_effect_commands_replacement_guard
BEFORE INSERT ON workflow_effect_commands
WHEN EXISTS (
    SELECT 1
    FROM workflow_effect_commands
    WHERE id = NEW.id
       OR operation_id = NEW.operation_id
       OR (
           workflow_id = NEW.workflow_id
           AND stage_id = NEW.stage_id
           AND effect_kind = NEW.effect_kind
           AND effect_key = NEW.effect_key
       )
)
BEGIN SELECT RAISE(ABORT, 'workflow effect replacement is not supported'); END;

CREATE TRIGGER workflow_effect_identity_guard
BEFORE UPDATE OF id, operation_id, workflow_id, stage_id, effect_class,
    effect_kind, effect_key, request_hash, request_json
ON workflow_effect_commands
BEGIN SELECT RAISE(ABORT, 'workflow effect identity is immutable'); END;

CREATE TRIGGER workflow_effect_terminal_guard
BEFORE UPDATE ON workflow_effect_commands
WHEN OLD.state IN ('completed', 'failed', 'manual_required')
BEGIN SELECT RAISE(ABORT, 'terminal workflow effect is immutable'); END;

CREATE TRIGGER workflow_effect_delete_guard
BEFORE DELETE ON workflow_effect_commands
BEGIN SELECT RAISE(ABORT, 'workflow effect deletion is not supported'); END;

CREATE TRIGGER workflow_stage_engine_references_insert_guard
BEFORE INSERT ON workflow_stage_references
WHEN
    (NEW.reference_kind = 'engine_source' AND (
        length(NEW.reference_id) < 7
        OR length(NEW.reference_id) > 473
        OR substr(NEW.reference_id, 1, 6) != 'paper:'
        OR substr(NEW.reference_id, 7, 1) NOT GLOB '[A-Za-z0-9]'
        OR substr(NEW.reference_id, 7) GLOB '*[^A-Za-z0-9._/-]*'
        OR instr(NEW.reference_id, char(0)) != 0
        OR instr(NEW.reference_id, '..') != 0
        OR instr(NEW.reference_id, '//') != 0
    ))
    OR (NEW.reference_kind = 'lineage_node' AND (
        length(NEW.reference_id) < 6
        OR length(NEW.reference_id) > 472
        OR substr(NEW.reference_id, 1, 5) != 'idea:'
        OR substr(NEW.reference_id, 6, 1) NOT GLOB '[A-Za-z0-9]'
        OR substr(NEW.reference_id, 6) GLOB '*[^A-Za-z0-9._/-]*'
        OR instr(NEW.reference_id, char(0)) != 0
        OR instr(NEW.reference_id, '..') != 0
        OR instr(NEW.reference_id, '//') != 0
    ))
BEGIN SELECT RAISE(ABORT, 'workflow engine reference is invalid'); END;

CREATE TRIGGER workflow_stage_references_replacement_guard
BEFORE INSERT ON workflow_stage_references
WHEN EXISTS (
    SELECT 1
    FROM workflow_stage_references
    WHERE id = NEW.id
       OR (
           workflow_id = NEW.workflow_id
           AND stage_id = NEW.stage_id
           AND reference_kind = NEW.reference_kind
           AND reference_id = NEW.reference_id
       )
)
BEGIN SELECT RAISE(ABORT, 'workflow reference replacement is not supported'); END;

CREATE TRIGGER workflow_stage_references_insert_guard
BEFORE INSERT ON workflow_stage_references
WHEN
    (NEW.reference_kind = 'decision' AND NOT EXISTS (
        SELECT 1
        FROM workflow_instances JOIN decisions
          ON decisions.run_id = workflow_instances.run_id
         AND decisions.id = NEW.reference_id
        WHERE workflow_instances.id = NEW.workflow_id
    ))
    OR (NEW.reference_kind = 'source_resolution' AND NOT EXISTS (
        SELECT 1
        FROM workflow_instances
        JOIN source_intents ON source_intents.run_id = workflow_instances.run_id
        JOIN source_resolutions
          ON source_resolutions.intent_id = source_intents.id
         AND source_resolutions.id = NEW.reference_id
        WHERE workflow_instances.id = NEW.workflow_id
    ))
    OR (NEW.reference_kind = 'source' AND NOT EXISTS (
        SELECT 1
        FROM workflow_instances
        JOIN run_source_bindings
          ON run_source_bindings.run_id = workflow_instances.run_id
         AND run_source_bindings.source_id = NEW.reference_id
        WHERE workflow_instances.id = NEW.workflow_id
    ))
    OR (NEW.reference_kind = 'source_binding' AND NOT EXISTS (
        SELECT 1
        FROM workflow_instances
        JOIN run_source_bindings
          ON run_source_bindings.run_id = workflow_instances.run_id
         AND run_source_bindings.id = NEW.reference_id
        WHERE workflow_instances.id = NEW.workflow_id
    ))
    OR (NEW.reference_kind = 'artifact_version' AND NOT EXISTS (
        SELECT 1
        FROM workflow_instances
        JOIN artifact_versions
          ON artifact_versions.run_id = workflow_instances.run_id
         AND artifact_versions.id = NEW.reference_id
        WHERE workflow_instances.id = NEW.workflow_id
    ))
    OR (NEW.reference_kind = 'snapshot' AND NOT EXISTS (
        SELECT 1
        FROM workflow_instances
        JOIN artifact_versions
          ON artifact_versions.run_id = workflow_instances.run_id
        JOIN artifact_snapshot_members
          ON artifact_snapshot_members.artifact_version_id = artifact_versions.id
         AND artifact_snapshot_members.snapshot_id = NEW.reference_id
        WHERE workflow_instances.id = NEW.workflow_id
    ))
    OR (NEW.reference_kind IN ('event', 'runtime_event') AND NOT EXISTS (
        SELECT 1
        FROM workflow_instances
        JOIN run_events
          ON run_events.run_id = workflow_instances.run_id
         AND run_events.id = NEW.reference_id
        WHERE workflow_instances.id = NEW.workflow_id
    ))
BEGIN SELECT RAISE(ABORT, 'workflow stage reference is inconsistent'); END;

CREATE TRIGGER workflow_stage_references_update_guard
BEFORE UPDATE ON workflow_stage_references
BEGIN SELECT RAISE(ABORT, 'workflow stage references are immutable'); END;
CREATE TRIGGER workflow_stage_references_delete_guard
BEFORE DELETE ON workflow_stage_references
BEGIN SELECT RAISE(ABORT, 'workflow stage references are immutable'); END;

CREATE TRIGGER workflow_checkpoints_replacement_guard
BEFORE INSERT ON workflow_checkpoints
WHEN EXISTS (
    SELECT 1
    FROM workflow_checkpoints
    WHERE id = NEW.id OR stage_id = NEW.stage_id
)
BEGIN SELECT RAISE(ABORT, 'workflow checkpoint replacement is not supported'); END;

CREATE TRIGGER workflow_checkpoints_update_guard
BEFORE UPDATE ON workflow_checkpoints
BEGIN SELECT RAISE(ABORT, 'workflow checkpoints are immutable'); END;
CREATE TRIGGER workflow_checkpoints_delete_guard
BEFORE DELETE ON workflow_checkpoints
BEGIN SELECT RAISE(ABORT, 'workflow checkpoints are immutable'); END;

CREATE TRIGGER workflow_checkpoints_insert_guard
BEFORE INSERT ON workflow_checkpoints
WHEN NOT EXISTS (
    SELECT 1 FROM workflow_stage_instances
    WHERE workflow_id = NEW.workflow_id
      AND id = NEW.stage_id
      AND checkpoint_enabled = 1
      AND state = 'completed'
)
BEGIN SELECT RAISE(ABORT, 'workflow checkpoint stage is not complete'); END;
"""


_MIGRATION_8 = """
CREATE TABLE transport_command_receipts (
    transport TEXT NOT NULL,
    command_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK(
        length(request_hash) = 64 AND request_hash = lower(request_hash)
        AND request_hash NOT GLOB '*[^0-9a-f]*'
    ),
    response_json TEXT NOT NULL CHECK(json_valid(response_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(transport, command_key)
);

CREATE TABLE transport_deliveries (
    transport TEXT NOT NULL,
    destination_digest TEXT NOT NULL CHECK(
        length(destination_digest) = 76
        AND substr(destination_digest, 1, 12) = 'hmac-sha256:'
        AND substr(destination_digest, 13) = lower(substr(destination_digest, 13))
        AND substr(destination_digest, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    event_id TEXT NOT NULL,
    projection_version INTEGER NOT NULL CHECK(projection_version >= 1),
    state TEXT NOT NULL CHECK(state IN ('pending', 'claimed', 'delivered')),
    claim_owner TEXT,
    claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK(claim_epoch >= 0),
    claim_expires_at TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    PRIMARY KEY(transport, destination_digest, event_id, projection_version),
    CHECK(
        (state = 'pending' AND claim_owner IS NULL
            AND claim_expires_at IS NULL AND delivered_at IS NULL)
        OR (state = 'claimed' AND claim_owner IS NOT NULL
            AND claim_expires_at IS NOT NULL AND delivered_at IS NULL)
        OR (state = 'delivered' AND claim_owner IS NULL
            AND claim_expires_at IS NULL AND delivered_at IS NOT NULL)
    )
);
CREATE INDEX transport_deliveries_claim_idx
    ON transport_deliveries(state, claim_expires_at, created_at);

CREATE TABLE transport_opaque_targets (
    token_digest TEXT PRIMARY KEY CHECK(
        length(token_digest) = 64 AND token_digest = lower(token_digest)
        AND token_digest NOT GLOB '*[^0-9a-f]*'
    ),
    namespace TEXT NOT NULL CHECK(namespace IN ('action', 'deep_link')),
    purpose TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    expected_revision INTEGER CHECK(
        expected_revision IS NULL OR expected_revision >= 0
    ),
    choice TEXT,
    scope_digest TEXT CHECK(
        scope_digest IS NULL OR (
            length(scope_digest) = 76
            AND substr(scope_digest, 1, 12) = 'hmac-sha256:'
            AND substr(scope_digest, 13) = lower(substr(scope_digest, 13))
            AND substr(scope_digest, 13) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    expires_at TEXT NOT NULL,
    consumed_by TEXT,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    CHECK(
        (consumed_by IS NULL AND consumed_at IS NULL)
        OR (consumed_by IS NOT NULL AND consumed_at IS NOT NULL)
    )
);
CREATE INDEX transport_opaque_targets_expiry_idx
    ON transport_opaque_targets(expires_at, token_digest);
"""


_MIGRATION_9 = """
CREATE TABLE asset_roots (
    root_id TEXT NOT NULL PRIMARY KEY,
    private_path TEXT NOT NULL,
    max_bytes INTEGER NOT NULL CHECK(max_bytes >= 1),
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TRIGGER asset_roots_replacement_guard
BEFORE INSERT ON asset_roots
WHEN EXISTS (
    SELECT 1 FROM asset_roots WHERE root_id = NEW.root_id
)
BEGIN SELECT RAISE(ABORT, 'asset root replacement is not supported'); END;

CREATE TRIGGER asset_roots_identity_guard
BEFORE UPDATE OF root_id ON asset_roots
BEGIN SELECT RAISE(ABORT, 'asset root identity is immutable'); END;

CREATE TRIGGER asset_roots_delete_guard
BEFORE DELETE ON asset_roots
BEGIN SELECT RAISE(ABORT, 'asset root deletion is not supported'); END;

CREATE TABLE connectors (
    id TEXT NOT NULL PRIMARY KEY,
    kind TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    credential_alias TEXT,
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX connectors_kind_idx
    ON connectors(kind, enabled, id);

CREATE TRIGGER connectors_replacement_guard
BEFORE INSERT ON connectors
WHEN EXISTS (
    SELECT 1 FROM connectors WHERE id = NEW.id
)
BEGIN SELECT RAISE(ABORT, 'connector replacement is not supported'); END;

CREATE TRIGGER connectors_identity_guard
BEFORE UPDATE OF id, kind ON connectors
BEGIN SELECT RAISE(ABORT, 'connector identity is immutable'); END;

CREATE TRIGGER connectors_delete_guard
BEFORE DELETE ON connectors
BEGIN SELECT RAISE(ABORT, 'connector deletion is not supported'); END;

CREATE TABLE paired_backup_proofs (
    id TEXT NOT NULL PRIMARY KEY,
    backup_set_digest TEXT NOT NULL CHECK(
        length(backup_set_digest) = 64
        AND backup_set_digest = lower(backup_set_digest)
        AND backup_set_digest NOT GLOB '*[^0-9a-f]*'
    ),
    protected_set_manifest_json TEXT NOT NULL CHECK(
        json_valid(protected_set_manifest_json)
    ),
    primary_completed_at TEXT NOT NULL,
    primary_snapshot_count INTEGER NOT NULL CHECK(primary_snapshot_count >= 1),
    independent_completed_at TEXT NOT NULL,
    independent_snapshot_count INTEGER NOT NULL CHECK(independent_snapshot_count >= 1),
    restore_completed_at TEXT NOT NULL,
    restored_database_count INTEGER NOT NULL CHECK(restored_database_count >= 1),
    verified_sample_count INTEGER NOT NULL CHECK(verified_sample_count >= 0),
    created_at TEXT NOT NULL,
    CHECK(restore_completed_at >= primary_completed_at),
    CHECK(restore_completed_at >= independent_completed_at)
);

CREATE INDEX paired_backup_proofs_recency_idx
    ON paired_backup_proofs(restore_completed_at, id);

CREATE TRIGGER paired_backup_proofs_replacement_guard
BEFORE INSERT ON paired_backup_proofs
WHEN EXISTS (
    SELECT 1 FROM paired_backup_proofs WHERE id = NEW.id
)
BEGIN SELECT RAISE(ABORT, 'paired backup proof replacement is not supported'); END;

CREATE TRIGGER paired_backup_proofs_update_guard
BEFORE UPDATE ON paired_backup_proofs
BEGIN SELECT RAISE(ABORT, 'paired backup proof is immutable'); END;

CREATE TRIGGER paired_backup_proofs_delete_guard
BEFORE DELETE ON paired_backup_proofs
BEGIN SELECT RAISE(ABORT, 'paired backup proof is immutable'); END;

CREATE TABLE system_health_observations (
    id TEXT NOT NULL PRIMARY KEY,
    asset_root_id TEXT REFERENCES asset_roots(root_id) ON DELETE RESTRICT,
    connector_id TEXT REFERENCES connectors(id) ON DELETE RESTRICT,
    backup_proof_id TEXT REFERENCES paired_backup_proofs(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK(status IN ('ok', 'degraded', 'unavailable', 'unknown')),
    category TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL CHECK(json_valid(metrics_json)),
    created_at TEXT NOT NULL,
    CHECK(
        (asset_root_id IS NOT NULL)
        + (connector_id IS NOT NULL)
        + (backup_proof_id IS NOT NULL) = 1
    )
);

CREATE INDEX system_health_subject_recency_idx
    ON system_health_observations(
        asset_root_id, connector_id, backup_proof_id, observed_at, id
    );

CREATE TRIGGER system_health_observations_replacement_guard
BEFORE INSERT ON system_health_observations
WHEN EXISTS (
    SELECT 1 FROM system_health_observations WHERE id = NEW.id
)
BEGIN SELECT RAISE(ABORT, 'system health observation replacement is not supported'); END;

CREATE TRIGGER system_health_observations_update_guard
BEFORE UPDATE ON system_health_observations
BEGIN SELECT RAISE(ABORT, 'system health observation is immutable'); END;

CREATE TRIGGER system_health_observations_delete_guard
BEFORE DELETE ON system_health_observations
BEGIN SELECT RAISE(ABORT, 'system health observation is immutable'); END;
"""


_MIGRATION_10 = """
CREATE TABLE transport_delivery_projections (
    transport TEXT NOT NULL,
    destination_digest TEXT NOT NULL CHECK(
        length(destination_digest) = 76
        AND substr(destination_digest, 1, 12) = 'hmac-sha256:'
        AND substr(destination_digest, 13) = lower(substr(destination_digest, 13))
        AND substr(destination_digest, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    event_id TEXT NOT NULL CHECK(length(event_id) BETWEEN 1 AND 500),
    projection_version INTEGER NOT NULL CHECK(projection_version >= 1),
    operation_id TEXT NOT NULL UNIQUE CHECK(length(operation_id) BETWEEN 1 AND 200),
    request_hash TEXT NOT NULL CHECK(
        length(request_hash) = 64 AND request_hash = lower(request_hash)
        AND request_hash NOT GLOB '*[^0-9a-f]*'
    ),
    projection_hash TEXT NOT NULL CHECK(
        length(projection_hash) = 64 AND projection_hash = lower(projection_hash)
        AND projection_hash NOT GLOB '*[^0-9a-f]*'
    ),
    destination_binding_digest TEXT NOT NULL CHECK(
        length(destination_binding_digest) = 76
        AND substr(destination_binding_digest, 1, 12) = 'hmac-sha256:'
        AND substr(destination_binding_digest, 13) = lower(substr(destination_binding_digest, 13))
        AND substr(destination_binding_digest, 13) NOT GLOB '*[^0-9a-f]*'
        AND destination_binding_digest = destination_digest
    ),
    routing TEXT NOT NULL CHECK(routing IN ('root', 'topic')),
    capability_binding_digest TEXT NOT NULL CHECK(
        length(capability_binding_digest) = 64
        AND capability_binding_digest = lower(capability_binding_digest)
        AND capability_binding_digest NOT GLOB '*[^0-9a-f]*'
    ),
    rpc_timeout_seconds INTEGER NOT NULL CHECK(rpc_timeout_seconds BETWEEN 1 AND 120),
    chunk_count INTEGER NOT NULL CHECK(chunk_count BETWEEN 1 AND 100),
    state TEXT NOT NULL CHECK(
        state IN ('pending', 'delivered', 'failed', 'manual_required')
    ),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    created_at TEXT NOT NULL CHECK(
        length(created_at) = 27
        AND substr(created_at, 5, 1) = '-'
        AND substr(created_at, 8, 1) = '-'
        AND substr(created_at, 11, 1) = 'T'
        AND substr(created_at, 14, 1) = ':'
        AND substr(created_at, 17, 1) = ':'
        AND substr(created_at, 20, 1) = '.'
        AND substr(created_at, 27, 1) = 'Z'
        AND replace(replace(replace(replace(replace(
            created_at, '-', ''), 'T', ''), ':', ''), '.', ''), 'Z', '')
            NOT GLOB '*[^0-9]*'
    ),
    completed_at TEXT CHECK(
        completed_at IS NULL OR (
            length(completed_at) = 27
            AND substr(completed_at, 5, 1) = '-'
            AND substr(completed_at, 8, 1) = '-'
            AND substr(completed_at, 11, 1) = 'T'
            AND substr(completed_at, 14, 1) = ':'
            AND substr(completed_at, 17, 1) = ':'
            AND substr(completed_at, 20, 1) = '.'
            AND substr(completed_at, 27, 1) = 'Z'
            AND replace(replace(replace(replace(replace(
                completed_at, '-', ''), 'T', ''), ':', ''), '.', ''), 'Z', '')
                NOT GLOB '*[^0-9]*'
        )
    ),
    PRIMARY KEY(transport, destination_digest, event_id, projection_version),
    CHECK(
        (state IN ('pending', 'manual_required') AND completed_at IS NULL)
        OR (state IN ('delivered', 'failed') AND completed_at IS NOT NULL)
    )
);
CREATE INDEX transport_delivery_projections_state_idx
    ON transport_delivery_projections(state, created_at, operation_id);

CREATE TRIGGER transport_delivery_projections_replacement_guard
BEFORE INSERT ON transport_delivery_projections
WHEN EXISTS (
    SELECT 1 FROM transport_delivery_projections
    WHERE (
        transport = NEW.transport
        AND destination_digest = NEW.destination_digest
        AND event_id = NEW.event_id
        AND projection_version = NEW.projection_version
    ) OR operation_id = NEW.operation_id
)
BEGIN SELECT RAISE(ABORT, 'transport delivery projection replacement is not supported'); END;

CREATE TRIGGER transport_delivery_projections_frozen_update_guard
BEFORE UPDATE OF
    transport, destination_digest, event_id, projection_version, operation_id,
    request_hash, projection_hash, destination_binding_digest, routing,
    capability_binding_digest, rpc_timeout_seconds, chunk_count, created_at
ON transport_delivery_projections
BEGIN SELECT RAISE(ABORT, 'transport delivery projection frozen fields are immutable'); END;

CREATE TRIGGER transport_delivery_projections_delete_guard
BEFORE DELETE ON transport_delivery_projections
BEGIN SELECT RAISE(ABORT, 'transport delivery projection deletion is not supported'); END;

CREATE TABLE transport_delivery_chunks (
    transport TEXT NOT NULL,
    destination_digest TEXT NOT NULL,
    event_id TEXT NOT NULL,
    projection_version INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL CHECK(chunk_index BETWEEN 0 AND 99),
    operation_id TEXT NOT NULL UNIQUE CHECK(length(operation_id) BETWEEN 1 AND 200),
    text TEXT NOT NULL CHECK(length(CAST(text AS BLOB)) BETWEEN 1 AND 4096),
    parse_mode TEXT NOT NULL CHECK(parse_mode = 'MarkdownV2'),
    chunk_hash TEXT NOT NULL CHECK(
        length(chunk_hash) = 64 AND chunk_hash = lower(chunk_hash)
        AND chunk_hash NOT GLOB '*[^0-9a-f]*'
    ),
    state TEXT NOT NULL CHECK(
        state IN ('pending', 'claimed', 'sending_unknown', 'delivered', 'failed')
    ),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    claim_owner TEXT,
    claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK(claim_epoch >= 0),
    claim_expires_at TEXT CHECK(
        claim_expires_at IS NULL OR (
            length(claim_expires_at) = 27
            AND substr(claim_expires_at, 5, 1) = '-'
            AND substr(claim_expires_at, 8, 1) = '-'
            AND substr(claim_expires_at, 11, 1) = 'T'
            AND substr(claim_expires_at, 14, 1) = ':'
            AND substr(claim_expires_at, 17, 1) = ':'
            AND substr(claim_expires_at, 20, 1) = '.'
            AND substr(claim_expires_at, 27, 1) = 'Z'
            AND replace(replace(replace(replace(replace(
                claim_expires_at, '-', ''), 'T', ''), ':', ''), '.', ''), 'Z', '')
                NOT GLOB '*[^0-9]*'
        )
    ),
    retry_not_before TEXT CHECK(
        retry_not_before IS NULL OR (
            length(retry_not_before) = 27
            AND substr(retry_not_before, 5, 1) = '-'
            AND substr(retry_not_before, 8, 1) = '-'
            AND substr(retry_not_before, 11, 1) = 'T'
            AND substr(retry_not_before, 14, 1) = ':'
            AND substr(retry_not_before, 17, 1) = ':'
            AND substr(retry_not_before, 20, 1) = '.'
            AND substr(retry_not_before, 27, 1) = 'Z'
            AND replace(replace(replace(replace(replace(
                retry_not_before, '-', ''), 'T', ''), ':', ''), '.', ''), 'Z', '')
                NOT GLOB '*[^0-9]*'
        )
    ),
    provider_receipt_digest TEXT CHECK(
        provider_receipt_digest IS NULL OR (
            length(provider_receipt_digest) = 76
            AND substr(provider_receipt_digest, 1, 12) = 'hmac-sha256:'
            AND substr(provider_receipt_digest, 13) = lower(substr(provider_receipt_digest, 13))
            AND substr(provider_receipt_digest, 13) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    failure_category TEXT CHECK(
        failure_category IS NULL OR length(failure_category) BETWEEN 1 AND 100
    ),
    manual_resolution_id TEXT REFERENCES transport_delivery_chunk_resolutions(id)
        ON DELETE RESTRICT,
    completed_at TEXT CHECK(
        completed_at IS NULL OR (
            length(completed_at) = 27
            AND substr(completed_at, 5, 1) = '-'
            AND substr(completed_at, 8, 1) = '-'
            AND substr(completed_at, 11, 1) = 'T'
            AND substr(completed_at, 14, 1) = ':'
            AND substr(completed_at, 17, 1) = ':'
            AND substr(completed_at, 20, 1) = '.'
            AND substr(completed_at, 27, 1) = 'Z'
            AND replace(replace(replace(replace(replace(
                completed_at, '-', ''), 'T', ''), ':', ''), '.', ''), 'Z', '')
                NOT GLOB '*[^0-9]*'
        )
    ),
    PRIMARY KEY(
        transport, destination_digest, event_id, projection_version, chunk_index
    ),
    FOREIGN KEY(transport, destination_digest, event_id, projection_version)
        REFERENCES transport_delivery_projections(
            transport, destination_digest, event_id, projection_version
        ) ON DELETE RESTRICT,
    CHECK(
        (state = 'pending' AND claim_owner IS NULL
            AND claim_expires_at IS NULL AND provider_receipt_digest IS NULL
            AND failure_category IS NULL AND manual_resolution_id IS NULL
            AND completed_at IS NULL)
        OR (state = 'claimed' AND claim_owner IS NOT NULL
            AND claim_expires_at IS NOT NULL AND retry_not_before IS NULL
            AND provider_receipt_digest IS NULL AND failure_category IS NULL
            AND manual_resolution_id IS NULL AND completed_at IS NULL)
        OR (state = 'sending_unknown' AND claim_owner IS NOT NULL
            AND claim_expires_at IS NULL AND retry_not_before IS NULL
            AND provider_receipt_digest IS NULL AND failure_category IS NULL
            AND manual_resolution_id IS NULL AND completed_at IS NULL)
        OR (state = 'delivered' AND claim_owner IS NULL
            AND claim_expires_at IS NULL AND retry_not_before IS NULL
            AND failure_category IS NULL AND completed_at IS NOT NULL
            AND (provider_receipt_digest IS NOT NULL OR manual_resolution_id IS NOT NULL))
        OR (state = 'failed' AND claim_owner IS NULL
            AND claim_expires_at IS NULL AND retry_not_before IS NULL
            AND provider_receipt_digest IS NULL AND failure_category IS NOT NULL
            AND completed_at IS NOT NULL)
    )
);
CREATE INDEX transport_delivery_chunks_claim_idx
    ON transport_delivery_chunks(
        state, retry_not_before, claim_expires_at, transport,
        destination_digest, event_id, projection_version, chunk_index
    );

CREATE TRIGGER transport_delivery_chunks_replacement_guard
BEFORE INSERT ON transport_delivery_chunks
WHEN EXISTS (
    SELECT 1 FROM transport_delivery_chunks
    WHERE (
        transport = NEW.transport
        AND destination_digest = NEW.destination_digest
        AND event_id = NEW.event_id
        AND projection_version = NEW.projection_version
        AND chunk_index = NEW.chunk_index
    ) OR operation_id = NEW.operation_id
)
BEGIN SELECT RAISE(ABORT, 'transport delivery chunk replacement is not supported'); END;

CREATE TRIGGER transport_delivery_chunks_frozen_update_guard
BEFORE UPDATE OF
    transport, destination_digest, event_id, projection_version, chunk_index,
    operation_id, text, parse_mode, chunk_hash
ON transport_delivery_chunks
BEGIN SELECT RAISE(ABORT, 'transport delivery chunk frozen fields are immutable'); END;

CREATE TRIGGER transport_delivery_chunks_delete_guard
BEFORE DELETE ON transport_delivery_chunks
BEGIN SELECT RAISE(ABORT, 'transport delivery chunk deletion is not supported'); END;

CREATE TABLE transport_delivery_chunk_buttons (
    transport TEXT NOT NULL,
    destination_digest TEXT NOT NULL,
    event_id TEXT NOT NULL,
    projection_version INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 5),
    label TEXT NOT NULL CHECK(length(label) BETWEEN 1 AND 64),
    callback_data TEXT NOT NULL CHECK(
        length(CAST(callback_data AS BLOB)) BETWEEN 1 AND 64
    ),
    token_digest TEXT NOT NULL CHECK(
        length(token_digest) = 64 AND token_digest = lower(token_digest)
        AND token_digest NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY(
        transport, destination_digest, event_id, projection_version,
        chunk_index, position
    ),
    FOREIGN KEY(
        transport, destination_digest, event_id, projection_version, chunk_index
    ) REFERENCES transport_delivery_chunks(
        transport, destination_digest, event_id, projection_version, chunk_index
    ) ON DELETE RESTRICT
);

CREATE TRIGGER transport_delivery_chunk_buttons_replacement_guard
BEFORE INSERT ON transport_delivery_chunk_buttons
WHEN EXISTS (
    SELECT 1 FROM transport_delivery_chunk_buttons
    WHERE transport = NEW.transport
      AND destination_digest = NEW.destination_digest
      AND event_id = NEW.event_id
      AND projection_version = NEW.projection_version
      AND chunk_index = NEW.chunk_index
      AND position = NEW.position
)
BEGIN SELECT RAISE(ABORT, 'transport delivery button replacement is not supported'); END;

CREATE TRIGGER transport_delivery_chunk_buttons_update_guard
BEFORE UPDATE ON transport_delivery_chunk_buttons
BEGIN SELECT RAISE(ABORT, 'transport delivery button is immutable'); END;

CREATE TRIGGER transport_delivery_chunk_buttons_delete_guard
BEFORE DELETE ON transport_delivery_chunk_buttons
BEGIN SELECT RAISE(ABORT, 'transport delivery button deletion is not supported'); END;

CREATE TABLE transport_delivery_chunk_capabilities (
    transport TEXT NOT NULL,
    destination_digest TEXT NOT NULL,
    event_id TEXT NOT NULL,
    projection_version INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 31),
    target_position INTEGER NOT NULL CHECK(target_position BETWEEN 0 AND 3199),
    namespace TEXT NOT NULL CHECK(namespace IN ('action', 'deep_link')),
    token_digest TEXT NOT NULL CHECK(
        length(token_digest) = 64 AND token_digest = lower(token_digest)
        AND token_digest NOT GLOB '*[^0-9a-f]*'
    ),
    expires_at TEXT NOT NULL CHECK(
        length(expires_at) = 27
        AND substr(expires_at, 5, 1) = '-'
        AND substr(expires_at, 8, 1) = '-'
        AND substr(expires_at, 11, 1) = 'T'
        AND substr(expires_at, 14, 1) = ':'
        AND substr(expires_at, 17, 1) = ':'
        AND substr(expires_at, 20, 1) = '.'
        AND substr(expires_at, 27, 1) = 'Z'
        AND replace(replace(replace(replace(replace(
            expires_at, '-', ''), 'T', ''), ':', ''), '.', ''), 'Z', '')
            NOT GLOB '*[^0-9]*'
    ),
    start_offset INTEGER,
    end_offset INTEGER,
    PRIMARY KEY(
        transport, destination_digest, event_id, projection_version,
        chunk_index, position
    ),
    UNIQUE(
        transport, destination_digest, event_id, projection_version, token_digest
    ),
    UNIQUE(
        transport, destination_digest, event_id, projection_version, target_position
    ),
    FOREIGN KEY(
        transport, destination_digest, event_id, projection_version, chunk_index
    ) REFERENCES transport_delivery_chunks(
        transport, destination_digest, event_id, projection_version, chunk_index
    ) ON DELETE RESTRICT,
    CHECK(
        (namespace = 'action' AND start_offset IS NULL AND end_offset IS NULL)
        OR (namespace = 'deep_link' AND start_offset IS NOT NULL
            AND end_offset IS NOT NULL AND start_offset >= 0
            AND end_offset > start_offset)
    )
);
CREATE INDEX transport_delivery_chunk_capabilities_expiry_idx
    ON transport_delivery_chunk_capabilities(expires_at, token_digest);

CREATE TRIGGER transport_delivery_chunk_capabilities_replacement_guard
BEFORE INSERT ON transport_delivery_chunk_capabilities
WHEN EXISTS (
    SELECT 1 FROM transport_delivery_chunk_capabilities
    WHERE (
        transport = NEW.transport
        AND destination_digest = NEW.destination_digest
        AND event_id = NEW.event_id
        AND projection_version = NEW.projection_version
        AND chunk_index = NEW.chunk_index
        AND position = NEW.position
    ) OR (
        transport = NEW.transport
        AND destination_digest = NEW.destination_digest
        AND event_id = NEW.event_id
        AND projection_version = NEW.projection_version
        AND token_digest = NEW.token_digest
    ) OR (
        transport = NEW.transport
        AND destination_digest = NEW.destination_digest
        AND event_id = NEW.event_id
        AND projection_version = NEW.projection_version
        AND target_position = NEW.target_position
    )
)
BEGIN SELECT RAISE(ABORT, 'transport delivery capability replacement is not supported'); END;

CREATE TRIGGER transport_delivery_chunk_capabilities_update_guard
BEFORE UPDATE ON transport_delivery_chunk_capabilities
BEGIN SELECT RAISE(ABORT, 'transport delivery capability is immutable'); END;

CREATE TRIGGER transport_delivery_chunk_capabilities_delete_guard
BEFORE DELETE ON transport_delivery_chunk_capabilities
BEGIN SELECT RAISE(ABORT, 'transport delivery capability deletion is not supported'); END;

CREATE TABLE transport_delivery_chunk_resolutions (
    id TEXT NOT NULL PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 200),
    transport TEXT NOT NULL,
    destination_digest TEXT NOT NULL,
    event_id TEXT NOT NULL,
    projection_version INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 1 AND 200),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 16 AND 128),
    request_hash TEXT NOT NULL CHECK(
        length(request_hash) = 64 AND request_hash = lower(request_hash)
        AND request_hash NOT GLOB '*[^0-9a-f]*'
    ),
    expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
    resolution TEXT NOT NULL CHECK(resolution IN ('assume_delivered', 'retry')),
    prior_state TEXT NOT NULL CHECK(prior_state = 'sending_unknown'),
    prior_revision INTEGER NOT NULL CHECK(prior_revision >= 0),
    prior_claim_epoch INTEGER NOT NULL CHECK(prior_claim_epoch >= 1),
    result_state TEXT NOT NULL CHECK(result_state IN ('delivered', 'pending')),
    result_revision INTEGER NOT NULL CHECK(result_revision >= 1),
    created_at TEXT NOT NULL CHECK(
        length(created_at) = 27
        AND substr(created_at, 5, 1) = '-'
        AND substr(created_at, 8, 1) = '-'
        AND substr(created_at, 11, 1) = 'T'
        AND substr(created_at, 14, 1) = ':'
        AND substr(created_at, 17, 1) = ':'
        AND substr(created_at, 20, 1) = '.'
        AND substr(created_at, 27, 1) = 'Z'
        AND replace(replace(replace(replace(replace(
            created_at, '-', ''), 'T', ''), ':', ''), '.', ''), 'Z', '')
            NOT GLOB '*[^0-9]*'
    ),
    UNIQUE(actor_id, idempotency_key),
    FOREIGN KEY(
        transport, destination_digest, event_id, projection_version, chunk_index
    ) REFERENCES transport_delivery_chunks(
        transport, destination_digest, event_id, projection_version, chunk_index
    ) ON DELETE RESTRICT
);
CREATE TRIGGER transport_delivery_chunk_resolutions_replacement_guard
BEFORE INSERT ON transport_delivery_chunk_resolutions
WHEN EXISTS (
    SELECT 1 FROM transport_delivery_chunk_resolutions
    WHERE id = NEW.id
       OR (actor_id = NEW.actor_id AND idempotency_key = NEW.idempotency_key)
)
BEGIN SELECT RAISE(ABORT, 'transport delivery resolution replacement is not supported'); END;

CREATE TRIGGER transport_delivery_chunk_resolutions_update_guard
BEFORE UPDATE ON transport_delivery_chunk_resolutions
BEGIN SELECT RAISE(ABORT, 'transport delivery resolution is immutable'); END;

CREATE TRIGGER transport_delivery_chunk_resolutions_delete_guard
BEFORE DELETE ON transport_delivery_chunk_resolutions
BEGIN SELECT RAISE(ABORT, 'transport delivery resolution is immutable'); END;

CREATE INDEX transport_delivery_chunk_resolutions_chunk_idx
    ON transport_delivery_chunk_resolutions(
        transport, destination_digest, event_id, projection_version,
        chunk_index, created_at, id
    );
"""


_MIGRATION_11 = """
CREATE TABLE adoption_manifests (
    manifest_id TEXT NOT NULL PRIMARY KEY CHECK(
        length(manifest_id) = 64 AND manifest_id = lower(manifest_id)
        AND manifest_id NOT GLOB '*[^0-9a-f]*'
    ),
    corpus_root_id TEXT NOT NULL REFERENCES asset_roots(root_id),
    actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 1 AND 200),
    entry_count INTEGER NOT NULL CHECK(entry_count >= 1),
    adopted_count INTEGER NOT NULL CHECK(adopted_count >= 0),
    committed_at TEXT NOT NULL,
    CHECK(adopted_count <= entry_count)
);

CREATE TABLE adoption_entries (
    id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL REFERENCES adoption_manifests(manifest_id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    paper_dir TEXT NOT NULL CHECK(length(paper_dir) BETWEEN 1 AND 500),
    engine_ref TEXT NOT NULL,
    content_digest TEXT NOT NULL CHECK(
        length(content_digest) = 64 AND content_digest = lower(content_digest)
        AND content_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    UNIQUE(manifest_id, source_id),
    UNIQUE(manifest_id, paper_dir)
);
CREATE INDEX adoption_entries_manifest_idx
    ON adoption_entries(manifest_id, paper_dir, id);
CREATE INDEX adoption_entries_source_idx ON adoption_entries(source_id, id);

CREATE TRIGGER adoption_manifests_immutable_guard
BEFORE UPDATE ON adoption_manifests
BEGIN SELECT RAISE(ABORT, 'adoption manifest is immutable'); END;

CREATE TRIGGER adoption_manifests_delete_guard
BEFORE DELETE ON adoption_manifests
BEGIN SELECT RAISE(ABORT, 'adoption manifest is immutable'); END;

CREATE TRIGGER adoption_entries_immutable_guard
BEFORE UPDATE ON adoption_entries
BEGIN SELECT RAISE(ABORT, 'adoption entry is immutable'); END;

CREATE TRIGGER adoption_entries_delete_guard
BEFORE DELETE ON adoption_entries
BEGIN SELECT RAISE(ABORT, 'adoption entry is immutable'); END;

-- Foreign keys are a per-CONNECTION pragma, so the FK above protects an
-- adopted source only from writers that remembered to enable it. A trigger
-- does not care which connection is asking, and raw sqlite3 access to this
-- database is a documented recurring hazard.
CREATE TRIGGER sources_adopted_delete_guard
BEFORE DELETE ON sources
WHEN EXISTS (SELECT 1 FROM adoption_entries WHERE source_id = OLD.id)
BEGIN SELECT RAISE(ABORT, 'adopted source cannot be deleted'); END;
"""


_MIGRATION_12 = """
CREATE TABLE runtime_activation_decisions (
    id TEXT PRIMARY KEY,
    decision TEXT NOT NULL CHECK(decision IN ('enable', 'disable')),
    mode TEXT CHECK(mode IS NULL OR mode IN ('permanent', 'window')),
    expires_at TEXT,
    actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 1 AND 200),
    decided_at TEXT NOT NULL,
    CHECK(
        (decision = 'enable' AND mode IS NOT NULL)
        OR (decision = 'disable' AND mode IS NULL AND expires_at IS NULL)
    ),
    CHECK(
        (mode = 'window' AND expires_at IS NOT NULL)
        OR (mode IS NULL OR mode = 'permanent') AND
           (mode IS NULL OR expires_at IS NULL)
    )
);
CREATE INDEX runtime_activation_decisions_order_idx
    ON runtime_activation_decisions(decided_at, id);

CREATE TRIGGER runtime_activation_decisions_immutable_guard
BEFORE UPDATE ON runtime_activation_decisions
BEGIN SELECT RAISE(ABORT, 'activation decision is immutable'); END;

CREATE TRIGGER runtime_activation_decisions_delete_guard
BEFORE DELETE ON runtime_activation_decisions
BEGIN SELECT RAISE(ABORT, 'activation decision is immutable'); END;
"""


_MIGRATION_CAPTURES = """
CREATE TABLE captures (
    id TEXT PRIMARY KEY,
    capture_key TEXT NOT NULL,
    -- The payload is stored exactly as submitted and is never rewritten:
    -- capture performs no resolution, so any normalization beyond the
    -- capture_key would be a decision the operator has not made yet.
    payload TEXT NOT NULL CHECK(length(payload) BETWEEN 1 AND 16384),
    kind TEXT NOT NULL CHECK(kind IN ('url', 'text')),
    note TEXT NOT NULL CHECK(length(note) <= 2000),
    state TEXT NOT NULL CHECK(state IN (
        'pending', 'approved', 'claimed', 'uncertain',
        'consumed', 'dismissed', 'failed'
    )),
    claim_owner TEXT,
    claim_epoch INTEGER NOT NULL DEFAULT 0,
    -- The lease columns carry the names the public projection denylist
    -- already strips, so a capture DTO cannot leak the consumer fence.
    claim_expires_at TEXT,
    known_source_id TEXT,
    consumed_source_ids TEXT,
    -- A frozen store allowlist, never raw exception text.
    failure_category TEXT CHECK(
        failure_category IS NULL
        OR length(failure_category) BETWEEN 1 AND 100
    ),
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Covers both dedup reads: the open-state refusal and the consumed-corpus
-- hint, which differ only in the state predicate.
CREATE INDEX captures_key_state_idx ON captures(capture_key, state);

-- Foreign keys are a per-connection pragma and raw sqlite3 access to this
-- database is a documented recurring hazard, so the durability of an
-- operator submission gets a trigger instead.
CREATE TRIGGER captures_delete_guard
BEFORE DELETE ON captures
BEGIN SELECT RAISE(ABORT, 'capture is durable'); END;
"""


#: ⟦S3.4/D6⟧ The operator's approval of one exact release.
#:
#: Deliberately the same shape as `runtime_activation_decisions` (migration 12):
#: append-only rows, immutable and undeletable by trigger, latest-wins read by
#: rowid. Two decisions, not one enum with the activation gate: D6 says approving
#: a *release* and enabling *dispatch* stay separate, so this table cannot say
#: anything about dispatch and that one cannot say anything about a release.
#:
#: The identity is `(release_id, manifest_sha256)` and not `release_id` alone.
#: A release id is a name the packager chooses; the manifest digest is what the
#: slot, the registry and `measure_identity` all already bind, so approving a
#: release id would approve any future bytes that reused the name.
_MIGRATION_RELEASE_APPROVAL = """
CREATE TABLE runtime_release_approvals (
    id TEXT PRIMARY KEY,
    decision TEXT NOT NULL CHECK(decision IN ('approve', 'revoke')),
    release_id TEXT NOT NULL CHECK(length(release_id) BETWEEN 1 AND 200),
    manifest_sha256 TEXT NOT NULL CHECK(
        length(manifest_sha256) = 64 AND manifest_sha256 = lower(manifest_sha256)
        AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 1 AND 200),
    decided_at TEXT NOT NULL
);
CREATE INDEX runtime_release_approvals_release_idx
    ON runtime_release_approvals(release_id, manifest_sha256, id);

CREATE TRIGGER runtime_release_approvals_immutable_guard
BEFORE UPDATE ON runtime_release_approvals
BEGIN SELECT RAISE(ABORT, 'release approval is immutable'); END;

CREATE TRIGGER runtime_release_approvals_delete_guard
BEFORE DELETE ON runtime_release_approvals
BEGIN SELECT RAISE(ABORT, 'release approval is immutable'); END;
"""


_MIGRATION_TRANSPORT_ACTIVATION = """
-- A mirror of migration 12's activation gate, one transport wide. It is a
-- copy rather than a generalization of `runtime_activation_decisions`
-- because that table is already in production and its rowid order is the
-- load-bearing part of the read: rebuilding it to add a `transport` column
-- would re-order an audit log that decisions are read back from.
CREATE TABLE transport_activation_decisions (
    id TEXT PRIMARY KEY,
    -- One transport today. The column exists so a second one cannot be
    -- smuggled in under the telegram gate's decisions.
    transport TEXT NOT NULL CHECK(transport = 'telegram'),
    decision TEXT NOT NULL CHECK(decision IN ('enable', 'disable')),
    scope TEXT CHECK(scope IS NULL OR scope IN ('permanent', 'window')),
    expires_at TEXT,
    actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 1 AND 200),
    decided_at TEXT NOT NULL,
    CHECK(
        (decision = 'enable' AND scope IS NOT NULL)
        OR (decision = 'disable' AND scope IS NULL AND expires_at IS NULL)
    ),
    CHECK(
        (scope = 'window' AND expires_at IS NOT NULL)
        OR (scope IS NULL OR scope = 'permanent') AND
           (scope IS NULL OR expires_at IS NULL)
    )
);
CREATE INDEX transport_activation_decisions_order_idx
    ON transport_activation_decisions(transport, decided_at, id);

CREATE TRIGGER transport_activation_decisions_immutable_guard
BEFORE UPDATE ON transport_activation_decisions
BEGIN SELECT RAISE(ABORT, 'transport activation decision is immutable'); END;

CREATE TRIGGER transport_activation_decisions_delete_guard
BEFORE DELETE ON transport_activation_decisions
BEGIN SELECT RAISE(ABORT, 'transport activation decision is immutable'); END;

-- Every send inside a window has to name the window it was authorized by.
-- The outbound receipt is `provider_receipt_digest`, an HMAC with no JSON
-- body to carry the id, so the window rides in its own column on the same
-- row -- nullable because every chunk written before this migration, and every
-- chunk sent outside a window, legitimately has none.
ALTER TABLE transport_delivery_chunks
    ADD COLUMN transport_window_id TEXT
    REFERENCES transport_activation_decisions(id);

-- Write-once, not frozen: the id is unknown when the chunk row is frozen at
-- projection time and is stamped when the send completes. Re-pointing a
-- delivered chunk at a different window would rewrite the audit answer to
-- "which authorization sent this".
CREATE TRIGGER transport_delivery_chunks_window_immutable_guard
BEFORE UPDATE OF transport_window_id ON transport_delivery_chunks
WHEN OLD.transport_window_id IS NOT NULL
     AND NEW.transport_window_id IS NOT OLD.transport_window_id
BEGIN SELECT RAISE(ABORT, 'transport delivery window id is immutable'); END;
"""

_MIGRATION_RESEARCH_SCHEDULES = """
-- D7's smallest honest scheduler: next-due timestamps, not cron expressions.
-- A cron expression would have to be evaluated by something, and the only
-- thing that could evaluate it is the jail P4 exists to replace. A row says
-- when it is next due and how long until the one after that; the tick reads
-- the activation gate first and does the rest.
CREATE TABLE research_schedules (
    job_key TEXT PRIMARY KEY CHECK(length(job_key) BETWEEN 1 AND 100),
    -- What the tick would actually run. `legacy` is a job that exists on paper
    -- and has no product implementation yet: represented so the inventory is
    -- complete, and never runnable.
    operation TEXT NOT NULL CHECK(operation IN ('capture_drain', 'legacy')),
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    interval_seconds INTEGER NOT NULL CHECK(
        interval_seconds BETWEEN 60 AND 604800
    ),
    next_due_at TEXT NOT NULL,
    last_started_at TEXT,
    last_finished_at TEXT,
    last_outcome TEXT CHECK(
        last_outcome IS NULL
        OR last_outcome IN ('ran', 'skipped', 'refused', 'failed')
    ),
    -- Where the cadence came from. `migrated` means it was read off a live
    -- jobs.json during a declared window; `unknown` means the live file was not
    -- reachable and the cadence was NOT transcribed from documentation.
    cadence_source TEXT NOT NULL CHECK(
        cadence_source IN ('product', 'migrated', 'unknown')
    ),
    legacy_schedule TEXT CHECK(
        legacy_schedule IS NULL OR length(legacy_schedule) <= 200
    ),
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX research_schedules_due_idx
    ON research_schedules(enabled, next_due_at);

-- Same durability discipline as the sibling decision tables: raw sqlite3
-- access to this database is a documented recurring hazard, and foreign keys
-- are a per-connection pragma, so the guard is a trigger.
CREATE TRIGGER research_schedules_delete_guard
BEFORE DELETE ON research_schedules
BEGIN SELECT RAISE(ABORT, 'research schedule is durable'); END;

-- Identity is immutable. A row may change its cadence, its enablement and its
-- outcome; it may never change which job it is or when it was created, because
-- the whole point of representing the twelve disabled jobs is that the
-- inventory cannot be quietly rewritten into something else.
CREATE TRIGGER research_schedules_identity_guard
BEFORE UPDATE ON research_schedules
WHEN NEW.job_key <> OLD.job_key
  OR NEW.operation <> OLD.operation
  OR NEW.created_at <> OLD.created_at
BEGIN SELECT RAISE(ABORT, 'research schedule identity is immutable'); END;
"""


_MIGRATION_THREAD_ARCHIVE = """
ALTER TABLE threads ADD COLUMN archived_at TEXT;
CREATE INDEX threads_workspace_archived_idx ON threads(workspace_id, archived_at, created_at, id);
"""


def migration_scripts() -> tuple[tuple[int, str], ...]:
    """Every migration `apply_migrations` can apply, in application order.

    Declared once so nothing else has to spell a version range. Slices land
    out of order and get renumbered at merge, so a hardcoded list of tuples in
    five test files is wrong the moment that happens. `test_schema` pins this
    against what `apply_migrations` actually records, so the two cannot drift.
    """

    return (
        (1, _MIGRATION_1),
        (2, _MIGRATION_2),
        (3, _MIGRATION_3),
        (4, _MIGRATION_4),
        (5, _MIGRATION_5),
        (6, _MIGRATION_6),
        (7, _MIGRATION_7),
        (8, _MIGRATION_8),
        (9, _MIGRATION_9),
        (10, _MIGRATION_10),
        (11, _MIGRATION_11),
        (RUNTIME_ACTIVATION_MIGRATION, _MIGRATION_12),
        (CAPTURES_MIGRATION, _MIGRATION_CAPTURES),
        (RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION, _MIGRATION_RELEASE_APPROVAL),
        (TRANSPORT_ACTIVATION_MIGRATION, _MIGRATION_TRANSPORT_ACTIVATION),
        (RESEARCH_SCHEDULES_MIGRATION, _MIGRATION_RESEARCH_SCHEDULES),
        (RESEARCH_CONTEXTS_MIGRATION, _MIGRATION_RESEARCH_CONTEXTS),
        (THREAD_ARCHIVE_MIGRATION, _MIGRATION_THREAD_ARCHIVE),
        (RESEARCH_ITEMS_MIGRATION, _MIGRATION_RESEARCH_ITEMS),
    )


#: The versions a fully migrated control store carries, in order.
MIGRATION_VERSIONS = tuple(version for version, _ in migration_scripts())


def apply_migrations(conn: sqlite3.Connection, *, now: str) -> None:
    """Apply every missing schema migration in one exclusive transaction."""

    conn.execute("BEGIN EXCLUSIVE")
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        versions = {
            int(row[0])
            for row in conn.execute("SELECT version FROM schema_migrations")
        }
        unknown = [version for version in versions if version > SCHEMA_VERSION]
        if unknown:
            raise RuntimeError("control store schema is newer than this Cortex build")
        for version, script in migration_scripts():
            if version not in versions:
                _execute_script_in_transaction(conn, script)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, now),
                )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _execute_script_in_transaction(conn: sqlite3.Connection, script: str) -> None:
    """Execute complete SQLite statements without executescript's implicit commit."""

    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("incomplete control-store migration statement")
