-- Teaching toolchain (dual-register pedagogy) schema — spec
-- docs/specs/2026-06-10-teaching-toolchain-spec.md §6.
--
-- CREATE IF NOT EXISTS ONLY: these are brand-new, pure-additive tables. No
-- rebuild, no m1d_schema.py involvement — which dodges the legacy_alter_table
-- view trap entirely (no dependent views, no drop-rename migrations).

CREATE TABLE IF NOT EXISTS lessons (
    lesson_id            TEXT PRIMARY KEY,
    question             TEXT NOT NULL,
    context_hint         TEXT NOT NULL DEFAULT '',
    topic                TEXT,
    status               TEXT NOT NULL DEFAULT 'queued'
                         CHECK(status IN ('queued','running','done','failed')),
    artifact_path        TEXT,
    grounding_paper_dirs TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at          TEXT
);

CREATE TABLE IF NOT EXISTS learner_topics (
    topic           TEXT PRIMARY KEY,
    taught_concepts TEXT NOT NULL DEFAULT '[]',
    familiarity     TEXT NOT NULL DEFAULT 'novice'
                    CHECK(familiarity IN ('novice','working','fluent')),
    open_questions  TEXT NOT NULL DEFAULT '[]',
    last_lesson_id  TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
