def test_schema_creates_paper_tables(research_db):
    import sqlite3, sqlite_vec
    conn = sqlite3.connect(str(research_db))
    conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"papers", "chunks", "chunk_embeddings", "chunks_fts"} <= names
    conn.close()
