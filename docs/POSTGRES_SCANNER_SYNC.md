# PostgreSQL scanner synchronization

When `CARD_SCANNER_DATABASE_BACKEND=postgres`, the server builds the desktop scanner's legacy `mtg_art_hashes.db` artifact from a PostgreSQL repeatable-read, read-only snapshot. The seven compatibility tables are streamed from cursors in bounded batches (currently 2,000 rows), written to a temporary SQLite file, checked for schema and SQLite integrity, and atomically published. A catalog revision and update timestamp form the cache key so one request downloads a consistent revision and the next request reuses the cached artifact until the PostgreSQL catalog changes.

The manifest and allow-listed downloads remain protected by the scanner API key. The manifest advertises file size, SHA-256, modification time, catalog row/hash counts, and revision. Download responses include a matching checksum header and disable shared caching. Export failure is reported only by the scanner manifest/download endpoint; it does not change normal inventory, deck, pricing, or PostgreSQL catalog transactions.

The desktop client already downloads to a temporary sibling, bounds the received bytes, checks manifest and response checksums, validates SQLite integrity/schema/row counts (and the TCGCSV JSON shape), then uses atomic replacement. The UI reloads in-memory card hashes and prices after a successful download. Regression coverage is in `scanner-client/tests/test_server_data_sync.py`.

The Android/PWA routes query and save through the PostgreSQL catalog/application modules rather than depending on the downloadable desktop artifact. The PostgreSQL scanner integration tests cover catalog search, account scoping, and idempotent scan writes in `tests/test_postgres_scanner.py`.

## Production sizing check

Do not use unit fixtures as proof of full production load capacity. Before enabling desktop synchronization for a large production catalog, run the benchmark against a disposable production-sized rehearsal database:

```bash
docker compose exec web python scripts/benchmark_postgres_catalog_export.py --output /data/.scanner-client-data/benchmark-mtg-art.db
```

The JSON report records elapsed export time, generated artifact size, printing/tag/hash row counts, and the benchmark process's peak resident memory. Also record the container's cgroup peak memory, CPU usage, free disk space, and available I/O while the run is active; process RSS does not include the rest of the container. Use the same `DATA_DIR` storage class and Docker memory/CPU limits planned for production. Remove the benchmark artifact after capturing the report.

The exporter uses named PostgreSQL server-side cursors and fetches bounded 2,000-row batches, so large query results are not buffered in full by the application cursor. The temporary SQLite file and final cached revision still require disk space, and the first export after a catalog update can be I/O intensive. Cache files live under `DATA_DIR/.scanner-client-data`; the server retains the newest two revision artifacts and prunes older revisions after a new snapshot is published.

**Not yet verified in this change:** a production-sized rehearsal database and its resource limits were not available in the repository workflow. Run the command above before treating the scale-validation requirement as complete.
