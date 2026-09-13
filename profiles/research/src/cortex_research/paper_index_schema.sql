PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS papers (
    paper_dir   TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    date        TEXT,
    keywords    TEXT,            -- JSON array
    summary     TEXT,
    projects    TEXT,            -- JSON array
    indexed_at  TEXT NOT NULL,
    arxiv_id    TEXT,            -- authoritative dedup key; NULL for non-arxiv (reader) papers
    published_at TEXT,           -- arxiv publish date (YYYY-MM-DD); `date`/dir-prefix = ingest date
    full_text_source TEXT,       -- 'html' | 'ocr' | 'pdf' | 'blog' how the body was extracted; NULL for reader/legacy. A 'pdf' row is a plain-text (no-formula, no-image) dump. 'blog' is a retired non-arxiv HTML ingest: preserved rows keep the value, nothing writes it any more.
    source_url  TEXT,            -- normalized origin URL of a retired non-arxiv URL ingest; preserved for existing rows and still written through index_paper's source_url argument. NULL for arxiv/reader papers.
    repo_urls   TEXT,            -- JSON array of resolved repo URLs (Spec 2 repo grounding); NULL until mined
    repo_urls_source TEXT        -- provenance of repo_urls: 'human' | 'agent' | 'mined'; NULL legacy ≈ mined. Pins (human/agent) are never overwritten by mined writes.
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_dir   TEXT NOT NULL,
    section     TEXT NOT NULL,   -- '__catalog__' for the per-paper catalog pseudo-chunk
    chunk_idx   INTEGER NOT NULL,
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_paper ON chunks(paper_dir);

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_embeddings USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    embedding FLOAT[4096]
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
