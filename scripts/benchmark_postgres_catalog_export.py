"""Measure PostgreSQL catalog export time, artifact size, counts and peak RSS.

Run against a disposable production-sized rehearsal database. The command
creates one consistent read-only PostgreSQL snapshot and a local SQLite
compatibility artifact at the requested path.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import time
from pathlib import Path

from postgres_app_db import connect_app_database
from postgres_catalog_export import export_mtg_legacy_art_catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Path for the generated SQLite artifact")
    args = parser.parse_args(argv)

    output = Path(args.output).expanduser().resolve()
    if output.exists():
        parser.error(f"Refusing to overwrite existing output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    conn = connect_app_database()
    try:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        result = export_mtg_legacy_art_catalog(conn, output)
        conn.commit()
    finally:
        conn.close()

    elapsed = time.perf_counter() - started
    # Linux reports ru_maxrss in KiB. This is the benchmark process's peak RSS,
    # not a substitute for measuring total container/cgroup memory.
    peak_rss_kib = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    report = {
        "output": str(output),
        "elapsed_seconds": round(elapsed, 3),
        "artifact_size_bytes": output.stat().st_size,
        "peak_process_rss_kib": peak_rss_kib,
        "container_memory_limit_bytes": os.environ.get("MEMORY_LIMIT_BYTES"),
        "export": result,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
