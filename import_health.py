"""Load a Health Auto Export JSON file by hand, e.g. to backfill history.

  python import_health.py HealthAutoExport-2026-01-01-2026-10-04.json

In the app: Manual Export → choose a date range → Format: JSON →
include Weight & Body Mass, Lean Body Mass, Body Fat Percentage, and Workouts.
Re-importing the same data is safe.
"""
import sys
from pathlib import Path

import health
from db import connect

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    conn = connect()
    counts = health.ingest(conn, health.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig")))
    print(f"Imported: {counts}")
