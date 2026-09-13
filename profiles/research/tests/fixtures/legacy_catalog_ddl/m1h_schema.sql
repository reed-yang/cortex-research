-- profiles/research/src/cortex_research/m1h_schema.sql
-- M1h "Exploration Threads" (Mode 1). An exploration thread IS an idea_seeds row
-- with status='exploring' (spec D4) — these two tables hang off idea_seeds(idea_id)
-- and never duplicate the lineage tree. idea_seeds enum-widening is owned by
-- m1d_schema.py (call ensure_m1d_schema first); this file only adds the siblings.

CREATE TABLE IF NOT EXISTS exploration_angles (
    angle_id         TEXT PRIMARY KEY,
    exploration_id   TEXT NOT NULL REFERENCES idea_seeds(idea_id),
    title            TEXT NOT NULL,
    rationale        TEXT NOT NULL,
    evidence_papers  TEXT NOT NULL DEFAULT '[]',
    evidence_quotes  TEXT NOT NULL DEFAULT '[]',
    maturity         TEXT NOT NULL DEFAULT 'proposed' CHECK(maturity IN ('proposed','sharpening','ready')),
    status           TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','greenlit','dismissed','parked')),
    spawned_idea_id  TEXT REFERENCES idea_seeds(idea_id),
    user_note        TEXT,
    -- JSON-encoded float array of the angle's semantic embedding (qwen3-8b), used
    -- by the write-time near-dup fold + the periodic consolidation pre-cluster.
    -- Nullable: an embed outage stores NULL (fail-open) and dedup falls back to
    -- exact-title match.
    embedding        TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_expl_angles_expl ON exploration_angles(exploration_id);
CREATE INDEX IF NOT EXISTS idx_expl_angles_status ON exploration_angles(status);

CREATE TABLE IF NOT EXISTS exploration_rounds (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    exploration_id   TEXT NOT NULL REFERENCES idea_seeds(idea_id),
    round_n          INTEGER NOT NULL,
    mode             TEXT NOT NULL DEFAULT 'discover' CHECK(mode IN ('discover','map','dialogue')),
    queries_fired    TEXT NOT NULL DEFAULT '[]',
    papers_ingested  TEXT NOT NULL DEFAULT '[]',
    angles_touched   TEXT NOT NULL DEFAULT '[]',
    landscape_md     TEXT,
    digest_text      TEXT,
    started_at       TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at     TEXT,
    UNIQUE(exploration_id, round_n)
);
CREATE INDEX IF NOT EXISTS idx_expl_rounds_expl ON exploration_rounds(exploration_id);
