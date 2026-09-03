#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///

# Purpose:
#   Export API-decoded Quicken budget records.

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qdf_tools.qdb_budgets import write_budget_exports


def main() -> None:
    parser = argparse.ArgumentParser(description="Export API-decoded Quicken budget records")
    parser.add_argument("header_extract", type=Path, help="fixed type-0x144 extract")
    parser.add_argument("year_extract", type=Path, help="QVAR type-0x14b extract")
    parser.add_argument("catalog_extract", type=Path, help="fixed type-0x080 extract")
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--budget", help="case-insensitive budget name filter")
    args = parser.parse_args()
    year_count, month_row_count = write_budget_exports(
        args.header_extract,
        args.year_extract,
        args.catalog_extract,
        args.output_directory,
        args.budget,
    )
    print(f"Budget export: {year_count:,} years and {month_row_count:,} month rows written")


if __name__ == "__main__":
    main()
