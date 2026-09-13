def test_relations_table_exists(research_db):
    import sqlite3, sqlite_vec
    conn = sqlite3.connect(str(research_db))
    conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "relations" in names and "relations_fts" in names
    conn.close()
