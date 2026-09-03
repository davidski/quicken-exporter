#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Reproduce a saved report from a self-contained SQLite export."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="self-contained SQLite financial export")
    parser.add_argument("output", type=Path, help="destination TSV")
    parser.add_argument(
        "--report",
        default="Grocery expenses",
        help="saved report name stored in the SQLite export",
    )
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    from qdf_tools.report_reproduction import UnsupportedReportError, reproduce_saved_report

    try:
        count = reproduce_saved_report(args.database, args.report, args.output)
    except UnsupportedReportError as error:
        parser.error(str(error))
    print(f"wrote {count:,} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
