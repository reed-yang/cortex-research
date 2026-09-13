-- profiles/research/src/cortex_research/radar_schema.sql
-- M1f additive schema: radar_signals + radar_runs.
-- The papers.source ALTER TABLE is handled in radar_schema.py (PRAGMA-checked).

CREATE TABLE IF NOT EXISTS radar_signals (
    signal_id            TEXT PRIMARY KEY,
    source               TEXT NOT NULL
                         CHECK(source IN ('arxiv','s2','twitter','xhs','xhs_pull')),
    source_ref           TEXT NOT NULL,
    query_id             TEXT,
    title                TEXT NOT NULL,
    abstract             TEXT,
    authors              TEXT,
    published_at         TEXT,
    arxiv_id             TEXT,
    s2_paper_id          TEXT,
    importance           INTEGER NOT NULL CHECK(importance BETWEEN 1 AND 5),
    importance_breakdown TEXT NOT NULL,
    scoring_method       TEXT NOT NULL
                         CHECK(scoring_method IN ('rule','rule+llm')),
    active_project_hits  TEXT,
    notes                TEXT,
    fetched_at           TEXT NOT NULL DEFAULT (datetime('now')),
    processed_status     TEXT NOT NULL DEFAULT 'pending'
                         CHECK(processed_status IN ('pending','indexed','skipped','failed')),
    paper_dir            TEXT,
    kind                 TEXT,
    source_url           TEXT,
    UNIQUE(source, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_radar_signals_importance ON radar_signals(importance);
CREATE INDEX IF NOT EXISTS idx_radar_signals_fetched ON radar_signals(fetched_at);
CREATE INDEX IF NOT EXISTS idx_radar_signals_paper ON radar_signals(paper_dir);
CREATE INDEX IF NOT EXISTS idx_radar_signals_query ON radar_signals(query_id);

CREATE TABLE IF NOT EXISTS radar_runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at  TEXT,
    -- 'partial': the run finished but a material fraction of queries failed
    -- to fetch (e.g. arxiv 429 / ReadTimeout) — honest signal that n_kept may
    -- be understated, distinct from a clean 'completed' or a fatal 'failed'.
    status        TEXT NOT NULL DEFAULT 'running'
                  CHECK(status IN ('running','completed','partial','failed')),
    source        TEXT NOT NULL
                  CHECK(source IN ('cron','manual','forward','xhs_pull')),
    n_queries     INTEGER DEFAULT 0,
    n_fetched     INTEGER DEFAULT 0,
    n_kept        INTEGER DEFAULT 0,
    n_indexed     INTEGER DEFAULT 0,
    n_pushed      INTEGER DEFAULT 0,
    n_spar_fired  INTEGER DEFAULT 0,
    n_failed_queries INTEGER DEFAULT 0,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_radar_runs_started ON radar_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_radar_runs_status ON radar_runs(status);

CREATE TABLE IF NOT EXISTS xhs_blogger_state (
    profile_id     TEXT PRIMARY KEY,
    nickname       TEXT,
    active         INTEGER NOT NULL DEFAULT 1,
    last_note_id   TEXT,
    last_polled_at TEXT,
    cursor         TEXT,
    seen_note_ids  TEXT NOT NULL DEFAULT '[]',
    added_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Auth canary: periodic liveness probe of the XHS providers. A row is written
-- per probe (T6); on persistent auth_fail a sticky-debounced row lands in
-- review_inbox under kind='corpus_health' for the ask-first digest.
CREATE TABLE IF NOT EXISTS auth_canary_runs (
    run_id     TEXT PRIMARY KEY,           -- 'canary_' + iso8601
    provider   TEXT NOT NULL,              -- 'tikhub' | 'xhs_http'
    status     TEXT NOT NULL,              -- 'ok' | 'auth_fail' | 'transient' | 'unknown'
    http_code  INTEGER,
    latency_ms INTEGER,
    error_text TEXT,
    ran_at     TEXT NOT NULL                -- ISO8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_auth_canary_recent
    ON auth_canary_runs(ran_at DESC);

-- Persistent review-inbox table: writable counterpart to the read-only
-- Python aggregator in review_inbox.py. Canary alerts + future push-based
-- corpus-health findings land here; kind='corpus_health' is the first user.
-- The Python aggregator (gather_inbox) continues to aggregate computed items
-- from idea_seeds/exploration_angles/etc independently.
--
-- dedupe_key: optional stable identity for an alert class (e.g.
-- 'xhs_auth_fail:tikhub') so repeated fires from the same provider replace
-- the existing open row instead of stacking indefinitely. NULL rows never
-- conflict with each other (UNIQUE ignores NULLs in SQLite).
CREATE TABLE IF NOT EXISTS review_inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,              -- 'corpus_health' | future kinds
    payload     TEXT NOT NULL,              -- JSON blob
    created_at  TEXT NOT NULL,             -- ISO8601 UTC
    resolved    INTEGER NOT NULL DEFAULT 0, -- 0=open, 1=resolved
    dedupe_key  TEXT UNIQUE                 -- NULL = always-new row; non-NULL = upsert
);
