-- src/cortex_research/crux_schema.sql
-- Layer A of the idea-evolution engine: the contested-belief ledger.
-- Additive; loaded via db._SCHEMAS (executescript, all IF NOT EXISTS). The
-- idea_seeds.origin 'revival' widen lives in m1d_schema.py (guarded rebuild), NOT here.

CREATE TABLE IF NOT EXISTS cruxes (
    crux_id            INTEGER PRIMARY KEY REFERENCES idea_challenges(id),
    canonical_claim    TEXT,
    scope_assumptions  TEXT,
    scope_tags         TEXT NOT NULL DEFAULT '[]',
    belief_state       TEXT NOT NULL DEFAULT 'active'
                        CHECK(belief_state IN ('active','contested','overturned')),
    adjudication       TEXT NOT NULL DEFAULT 'unadjudicated'
                        CHECK(adjudication IN ('unadjudicated','human_resolved','model_contested')),
    last_ledger_id     INTEGER,
    promoted_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS crux_ledger (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    crux_id             INTEGER NOT NULL REFERENCES cruxes(crux_id),
    idea_id             TEXT,
    source_challenge_id INTEGER,
    round_n             INTEGER,
    event               TEXT NOT NULL CHECK(event IN ('promote','bind','contest','revival')),
    role                TEXT CHECK(role IN ('fatal','survived','open','inherited') OR role IS NULL),
    contest_verdict     TEXT CHECK(contest_verdict IN ('upheld','weakened','overturned') OR contest_verdict IS NULL),
    new_technique       TEXT,
    scope_change        TEXT,
    scope_change_tags   TEXT,
    trigger             TEXT CHECK(trigger IN ('crux_overturned','new_evidence','human') OR trigger IS NULL),
    resumed_round       INTEGER,
    gate_audit_ref      TEXT,
    actor               TEXT CHECK(actor IN ('director','contest_gate','promotion','human','scheduler')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_crux_ledger_crux ON crux_ledger(crux_id, idea_id, id);
CREATE INDEX IF NOT EXISTS idx_crux_ledger_event ON crux_ledger(event);

CREATE TRIGGER IF NOT EXISTS crux_ledger_no_update
BEFORE UPDATE ON crux_ledger BEGIN SELECT RAISE(ABORT, 'crux_ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS crux_ledger_no_delete
BEFORE DELETE ON crux_ledger BEGIN SELECT RAISE(ABORT, 'crux_ledger is append-only'); END;

CREATE TABLE IF NOT EXISTS crux_evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    crux_id       INTEGER NOT NULL REFERENCES cruxes(crux_id),
    paper_key     TEXT NOT NULL,
    paper_dir     TEXT,
    quote         TEXT,
    polarity      TEXT NOT NULL CHECK(polarity IN ('supports','refutes')),
    strength      INTEGER CHECK(strength BETWEEN 1 AND 5),
    ledger_id     INTEGER REFERENCES crux_ledger(id),
    superseded_by INTEGER REFERENCES crux_evidence(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(crux_id, paper_key, polarity)
);

CREATE VIRTUAL TABLE IF NOT EXISTS crux_embeddings USING vec0(
    crux_id INTEGER PRIMARY KEY,
    embedding FLOAT[4096]
);

CREATE TABLE IF NOT EXISTS crux_fatal_set (
    idea_id        TEXT NOT NULL,
    challenge_id   INTEGER NOT NULL,
    gate_audit_ref TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (idea_id, challenge_id)
);

CREATE VIEW IF NOT EXISTS v_crux_current_edge AS
SELECT le.crux_id, le.idea_id,
       CASE
         WHEN EXISTS (SELECT 1 FROM crux_fatal_set f
                      JOIN crux_ledger lg ON lg.source_challenge_id = f.challenge_id
                      WHERE f.idea_id = le.idea_id AND lg.crux_id = le.crux_id) THEN 'fatal'
         WHEN s.status = 'graduated' THEN 'survived'
         WHEN s.status IN ('killed','dormant') THEN 'survived'
         WHEN s.status IN ('incubating','exploring') THEN 'open'
         ELSE 'open'
       END AS role
FROM (SELECT DISTINCT crux_id, idea_id FROM crux_ledger WHERE idea_id IS NOT NULL) le
JOIN idea_seeds s ON s.idea_id = le.idea_id;

CREATE VIEW IF NOT EXISTS v_crux_provenance AS
SELECT c.crux_id, c.belief_state, c.adjudication, c.promoted_at,
       (SELECT COUNT(*) FROM v_crux_current_edge e
          WHERE e.crux_id = c.crux_id AND e.role='fatal') AS n_fatal,
       (SELECT COALESCE(SUM(CASE polarity WHEN 'supports' THEN strength ELSE -strength END),0)
          FROM crux_evidence ev WHERE ev.crux_id = c.crux_id AND ev.superseded_by IS NULL)
          AS polarity_balance
FROM cruxes c;
