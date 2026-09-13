# Legacy catalog DDL (test data only)

`cortex_platform/product/research/catalog.py` reads the preserved legacy
`research.db` query-only, by raw SQL, and never imports `cortex_research`.
Its test (`cortex_platform/tests/product/research/test_catalog.py`) builds its
fixtures by executing the REAL legacy DDL rather than hand-written tables, so
the catalog is tested against the shape the retired research engine actually
wrote.

The three files here are the exact bytes of
`profiles/research/src/cortex_research/{m1c,m1d,m1h}_schema.sql` at extraction
base `196f69bb`. They are retained as TEST DATA because the modules that applied
them are excluded from the supported surface and their `.sql` resources are not
among the five the nine kept modules read (`db.py` reads `paper_index_schema`,
`ledger_schema`, `crux_schema`, `teaching_schema`; `radar_schema.py` reads
`radar_schema.sql`). Nothing imports or applies these files at runtime.
