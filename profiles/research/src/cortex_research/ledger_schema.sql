CREATE TABLE IF NOT EXISTS relations (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  source_paper    TEXT NOT NULL,
  target_paper    TEXT NOT NULL,
  relation_type   TEXT NOT NULL CHECK (relation_type IN
                   ('extends','contradicts','supersedes','shares-mechanism')),
  evidence        TEXT NOT NULL,
  evidence_paper  TEXT NOT NULL,
  evidence_locator TEXT NOT NULL,
  importance      INTEGER NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
  confidence      INTEGER NOT NULL DEFAULT 3 CHECK (confidence BETWEEN 1 AND 5),
  project_ref     TEXT,
  status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','user_resolved','superseded')),
  created_at      TEXT NOT NULL,
  superseded_by   INTEGER REFERENCES relations(id),
  model_used      TEXT NOT NULL,
  run_id          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rel_source  ON relations(source_paper);
CREATE INDEX IF NOT EXISTS idx_rel_target  ON relations(target_paper);
CREATE INDEX IF NOT EXISTS idx_rel_type    ON relations(relation_type);
CREATE INDEX IF NOT EXISTS idx_rel_project ON relations(project_ref);

CREATE VIRTUAL TABLE IF NOT EXISTS relations_fts USING fts5(evidence, content='relations', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS relations_fts_ai AFTER INSERT ON relations BEGIN
    INSERT INTO relations_fts(rowid, evidence) VALUES (new.id, new.evidence);
END;
CREATE TRIGGER IF NOT EXISTS relations_fts_ad AFTER DELETE ON relations BEGIN
    INSERT INTO relations_fts(relations_fts, rowid, evidence) VALUES('delete', old.id, old.evidence);
END;
CREATE TRIGGER IF NOT EXISTS relations_fts_au AFTER UPDATE ON relations BEGIN
    INSERT INTO relations_fts(relations_fts, rowid, evidence) VALUES('delete', old.id, old.evidence);
    INSERT INTO relations_fts(rowid, evidence) VALUES (new.id, new.evidence);
END;
