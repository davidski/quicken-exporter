#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Export a Quicken Home Inventory QHI.IDB directly to standalone SQLite."""

# Purpose:
#   Export a Quicken Home Inventory QHI.IDB file to standalone SQLite.

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="source QHI.IDB file")
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="destination SQLite database (default: output/home_inventory.sqlite)",
    )
    args = parser.parse_args()
    sys.path.insert(0, str(repo_root / "src"))
    from qdf_tools.qhi_idb import export_qhi_to_sqlite

    source = args.source.expanduser().resolve()
    output = (
        (args.output if args.output is not None else repo_root / "output" / "home_inventory.sqlite")
        .expanduser()
        .resolve()
    )
    if not source.is_file():
        raise SystemExit(f"source does not exist: {source}")
    counts = export_qhi_to_sqlite(source, output)
    print(
        f"wrote {counts['items']} items and {counts['rooms']} rooms from {source.name} to {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
