-- profiles/research/src/cortex_research/m1d_schema.sql
-- M1d schema: idea_seeds + idea_attempts + idea_challenges. Additive on top
-- of M1a/M1b/M1c. All tables prefixed idea_* per interface-contracts §3.1.

-- Status enum keeps 'aborted' (retired, never written — see m1d_schema.py) plus
-- the active 'dormant' (Phase B). Phase B columns (awaiting_fork_decision,
-- dormant_reason) and Phase C lineage columns (parent_idea_id, origin,
-- derived_from_round, depth) are baked in here so a brand-new DB needs no rebuild;
-- legacy DBs still self-migrate via the guarded rebuilds in m1d_schema.py.
CREATE TABLE IF NOT EXISTS idea_seeds (
    idea_id              TEXT PRIMARY KEY,
    seed_text            TEXT NOT NULL,
    slug                 TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'incubating'
                          CHECK(status IN ('incubating', 'graduated', 'killed', 'aborted', 'dormant', 'awaiting_human')),
    n_rounds_completed   INTEGER NOT NULL DEFAULT 0,
    converged            INTEGER NOT NULL DEFAULT 0,
    graduated_to_project_ref  TEXT,
    md_path              TEXT NOT NULL,
    source_atom_id       TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
    last_attended_at     TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at         TEXT,
    awaiting_fork_decision INTEGER DEFAULT 0,
    dormant_reason       TEXT,
    parent_idea_id       TEXT REFERENCES idea_seeds(idea_id),
    origin               TEXT NOT NULL DEFAULT 'manual'
                          CHECK(origin IN ('manual', 'atom', 'pivot_of_kill', 'fork_of_round')),
    derived_from_round   INTEGER,
    depth                INTEGER NOT NULL DEFAULT 0,
    -- B4: why the idea graduated ('round_limit' = forced at MAX_ROUNDS without
    -- convergence vs 'auto_converged' = converged). Additive nullable; legacy
    -- DBs self-migrate via _add_column_if_missing in m1d_schema.py.
    graduation_reason    TEXT,
    -- Pre-incubation gate: reminder-sweep throttle clock (last_reminded_at) +
    -- drop reason (kill_reason). Additive nullable; legacy DBs self-migrate via
    -- _add_column_if_missing in m1d_schema.py.
    last_reminded_at     TEXT,
    kill_reason          TEXT,
    -- MED-2 (revival round-budget): operator-granted extra rounds beyond
    -- CORTEX_IDEA_MAX_ROUNDS for THIS idea — granted by idea_tree.revive() when
    -- a human revives an at-cap (round_cap_unconverged) idea, read by the FF1
    -- cap gate as effective cap = knob + bonus. Additive default-0; legacy DBs
    -- self-migrate via _add_column_if_missing in m1d_schema.py.
    round_cap_bonus      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_idea_seeds_status ON idea_seeds(status);
CREATE INDEX IF NOT EXISTS idx_idea_seeds_slug ON idea_seeds(slug);
-- idx_idea_seeds_parent is created in m1d_schema.py (_add_lineage_columns), NOT
-- here: on a LEGACY DB this script runs before parent_idea_id is ALTER-added, so
-- indexing it here would fail. The Python migration creates it after the ALTER.

CREATE TABLE IF NOT EXISTS idea_attempts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id              TEXT NOT NULL REFERENCES idea_seeds(idea_id),
    round_n              INTEGER NOT NULL,
    papers_pulled        TEXT NOT NULL,
    fetch_source         TEXT NOT NULL DEFAULT 'paper_search'
                          CHECK(fetch_source IN ('paper_search', 'idea_fetch_arxiv', 'mixed')),
    decompositions       TEXT NOT NULL,
    convergence_verdict  TEXT,
    convergence_reason   TEXT,
    started_at           TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at         TEXT,
    UNIQUE(idea_id, round_n)
);
CREATE INDEX IF NOT EXISTS idx_idea_attempts_idea ON idea_attempts(idea_id);

CREATE TABLE IF NOT EXISTS idea_challenges (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id              TEXT NOT NULL REFERENCES idea_seeds(idea_id),
    round_n              INTEGER NOT NULL,
    challenge_text       TEXT NOT NULL,
    dimension            TEXT NOT NULL
                          CHECK(dimension IN ('novelty', 'feasibility', 'impact', 'risk')),
    evidence_papers      TEXT NOT NULL,
    evidence_quotes      TEXT NOT NULL,
    importance           INTEGER NOT NULL CHECK(importance BETWEEN 1 AND 5),
    confidence           INTEGER NOT NULL CHECK(confidence BETWEEN 1 AND 5),
    model_agreement      TEXT NOT NULL
                          CHECK(model_agreement IN ('both', 'opus_only', 'gpt_only')),
    status               TEXT NOT NULL DEFAULT 'open'
                          CHECK(status IN ('open', 'user_resolved', 'dismissed', 'auto_resolved')),
    user_verdict         TEXT,
    resolved_at          TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_idea_challenges_idea ON idea_challenges(idea_id);
CREATE INDEX IF NOT EXISTS idx_idea_challenges_status ON idea_challenges(status);
CREATE INDEX IF NOT EXISTS idx_idea_challenges_round ON idea_challenges(idea_id, round_n);

-- Intent Anchor (lean v1): per-idea provenance/charter spine. See
-- docs/specs/2026-06-02-intent-anchor-spec.md. No FK to idea_seeds on purpose
-- (idea_seeds is rebuilt by m1d_schema.py's CHECK-widen migrations; we always
-- insert with a live idea_id). No triggers/vec0/views — intent has no overturn
-- dynamic. Lives here (not db._SCHEMAS) because exploration_run._open_conn calls
-- ensure_m1d_schema but NOT db.apply_schema, so the table must ride m1d to exist
-- on the greenlight conn.
-- kind 'evidence' = machine-routed cross-mode evidence (angle router, spec
-- 2026-06-10 §3.4; source='router:<angle_id>') — rendered as a separate
-- INJECTED EVIDENCE block, never mixed into human steering. This note stays
-- OUTSIDE the CREATE body on purpose: SQLite stores the CREATE statement
-- verbatim in sqlite_master, and m1d_schema.py's kind-widen migration probes
-- that SQL for the 'evidence' token — a comment inside the body could
-- false-positive the guard.
CREATE TABLE IF NOT EXISTS idea_anchor (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK(kind IN
                  ('root_question', 'endorsement', 'steering', 'reframe',
                   'evidence')),
    text        TEXT NOT NULL,
    source      TEXT,
    round_n     INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_idea_anchor_idea ON idea_anchor(idea_id, id);

-- Angle router (docs/specs/2026-06-10-angle-router-and-idea-dossier-spec.md
-- §3.6): durable inbox of judged angle->idea matches awaiting digest review.
-- JSONL audit keeps the decision trail; this table keeps the STATE. Rides m1d
-- (NOT db._SCHEMAS) like idea_anchor above, because exploration_run._open_conn /
-- mcp_servers call ensure_m1d_schema but NOT db.apply_schema.
CREATE TABLE IF NOT EXISTS router_candidates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    angle_id    TEXT NOT NULL,
    idea_id     TEXT NOT NULL,
    relation    TEXT NOT NULL CHECK(relation IN ('supports','challenges','extends','duplicates')),
    confidence  TEXT NOT NULL CHECK(confidence IN ('high','medium','low')),
    rationale   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','applied','dismissed')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(angle_id, idea_id)
);

-- Ask-First review digest (docs/specs/2026-06-06-ask-first-digest-spec.md): the
-- number->item snapshot of the LAST digest, so a later numeric pick resolves
-- deterministically against a stable mapping (live state may have renumbered).
-- Append-only; the most-recent row is authoritative for routing. Rides m1d (NOT
-- db._SCHEMAS) like idea_anchor above, because exploration_run._open_conn /
-- mcp_servers call ensure_m1d_schema but NOT db.apply_schema.
CREATE TABLE IF NOT EXISTS review_digest_log (
    digest_id   TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    items_json  TEXT NOT NULL   -- ordered [{n, kind, ref, title, expand_tag}]
);
CREATE INDEX IF NOT EXISTS idx_review_digest_created ON review_digest_log(created_at);

-- Layer B wake loop (spec 2026-06-11): falsifiable wake conditions per idea.
-- status: armed (watchable) -> fired (acted on) / candidate (matched, awaiting
-- human via digest) / retired (superseded by a re-park's new conditions).
CREATE TABLE IF NOT EXISTS wake_conditions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id         TEXT    NOT NULL,
    kind            TEXT    NOT NULL CHECK(kind IN ('crux_ref','prose')),
    crux_id         INTEGER,
    condition_text  TEXT    NOT NULL,
    condition_tags  TEXT,
    resume_allowed  INTEGER NOT NULL DEFAULT 1,
    status          TEXT    NOT NULL DEFAULT 'armed'
                    CHECK(status IN ('armed','fired','candidate','retired')),
    source          TEXT    NOT NULL CHECK(source IN ('cap','kill','dormant','backfill','human')),
    fired_evidence  TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    fired_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_wake_idea ON wake_conditions(idea_id, status);
CREATE INDEX IF NOT EXISTS idx_wake_crux ON wake_conditions(crux_id) WHERE crux_id IS NOT NULL;
