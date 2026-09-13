-- profiles/research/src/cortex_research/m1c_schema.sql
-- M1c additive schema: judgments, challenges (reflection debt), spar_runs.

CREATE TABLE IF NOT EXISTS judgments (
  judgment_id  TEXT PRIMARY KEY,
  project_ref  TEXT NOT NULL,
  text         TEXT NOT NULL,
  source_line  INTEGER,
  status       TEXT NOT NULL DEFAULT 'active'
               CHECK(status IN ('active', 'challenged', 'user_resolved')),
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_judgments_project ON judgments(project_ref);
CREATE INDEX IF NOT EXISTS idx_judgments_status ON judgments(status);

CREATE TABLE IF NOT EXISTS spar_runs (
  run_id        TEXT PRIMARY KEY,
  project_ref   TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'running'
                CHECK(status IN ('running', 'completed', 'failed')),
  model_pass_1  TEXT,
  model_pass_2  TEXT,
  n_judgments   INTEGER DEFAULT 0,
  n_challenges  INTEGER DEFAULT 0,
  n_both        INTEGER DEFAULT 0,
  n_pushed      INTEGER DEFAULT 0,
  started_at    TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at  TEXT,
  error         TEXT
);

CREATE TABLE IF NOT EXISTS challenges (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  judgment_id      TEXT NOT NULL REFERENCES judgments(judgment_id),
  challenge_text   TEXT NOT NULL,
  evidence_papers  TEXT NOT NULL,
  evidence_quotes  TEXT,
  importance       INTEGER NOT NULL CHECK(importance BETWEEN 1 AND 5),
  confidence       INTEGER NOT NULL CHECK(confidence BETWEEN 1 AND 5),
  model_agreement  TEXT NOT NULL CHECK(model_agreement IN ('both', 'opus_only', 'gpt_only')),
  status           TEXT NOT NULL DEFAULT 'open'
                   CHECK(status IN ('open', 'user_resolved', 'auto_downgraded', 'dismissed')),
  user_verdict     TEXT,
  resolved_at      TEXT,
  created_at       TEXT NOT NULL DEFAULT (datetime('now')),
  spar_run_id      TEXT NOT NULL REFERENCES spar_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_challenges_judgment ON challenges(judgment_id);
CREATE INDEX IF NOT EXISTS idx_challenges_status ON challenges(status);
CREATE INDEX IF NOT EXISTS idx_challenges_run ON challenges(spar_run_id);

-- M1c extension: spar monitoring pool for M1d/M1f integration.
-- Registered projects are polled by M1d (idea graduation) and M1f (radar recheck).
CREATE TABLE IF NOT EXISTS spar_monitored_projects (
  project_ref   TEXT PRIMARY KEY,
  registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);
