#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["olefile==0.47"]
# ///
"""Portable financial-and-saved-report SQLite export.

This script runs on macOS, Linux, and Windows.  Direct protected-QDF extraction
is intentionally not implemented here: the current extraction boundary is
Quicken's 32-bit Windows qdb.dll.  On macOS, provide either a Quicken QIF
export or the complete extraction directory produced by the Windows helper.
"""

# Purpose:
#   Export portable Quicken financial data and saved reports to SQLite.

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path


def _load_project() -> None:
    """Make the checkout's src layout importable without installing a package."""
    repo_root = Path(__file__).resolve().parents[1]
    src = repo_root / "src"
    sys.path.insert(0, str(src))


def _format_for(source: Path, requested: str) -> str:
    if requested != "auto":
        return requested
    if source.is_dir() and (source / "qdb-type-13c.bin").is_file():
        return "qdb"
    suffix = source.suffix.lower()
    if suffix in {".qdf", ".backup"} or source.name.lower().endswith(".qdf-backup"):
        return "qdf"
    return "qif"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="QIF or complete Windows extraction directory")
    parser.add_argument("output", type=Path, help="destination SQLite database")
    parser.add_argument(
        "--format",
        choices=("auto", "qif", "qdb", "qdf"),
        default="auto",
        help="source format (default: infer from the filename)",
    )
    parser.add_argument("--start-date", type=dt.date.fromisoformat)
    parser.add_argument("--end-date", type=dt.date.fromisoformat)
    parser.add_argument(
        "--qdf-path",
        type=Path,
        help="original structured QDF; enables extraction from its embedded .QPH quote stream",
    )
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not source.exists():
        parser.error(f"source does not exist: {source}")
    qdf_path = args.qdf_path.expanduser().resolve() if args.qdf_path else None
    if qdf_path is not None and not qdf_path.is_file():
        parser.error(f"QDF path does not exist: {qdf_path}")

    if source.is_file() and source.suffix.lower() == ".bin":
        parser.error(
            "standalone .bin imports are not supported; provide a QIF or a "
            "complete Windows extraction directory"
        )

    source_format = _format_for(source, args.format)
    if source_format == "qdf":
        parser.error(
            "direct QDF extraction is Windows-only in this project; run "
            "Export-QdfFinancial.ps1 on Windows and provide its complete "
            "extraction directory, or export a QIF from Quicken"
        )
    if qdf_path is not None and source_format != "qdb":
        parser.error("--qdf-path is only valid with --format qdb")

    if source_format == "qdb":
        if not source.is_dir():
            parser.error(
                "QDB materialization requires a complete Windows extraction "
                "directory, not a standalone file"
            )
        required = (
            "qdb-type-13c.bin",
            "qdb-type-080.bin",
            "qdb-type-134.bin",
            "qdb-account-map-134.bin",
            "qdb-string-map.tsv",
            "qdb-type-144.bin",
            "qdb-type-14b-full.bin",
        )
        missing = [name for name in required if not (source / name).is_file()]
        if missing:
            parser.error("incomplete QDB extraction directory; missing: " + ", ".join(missing))

    _load_project()
    if source_format == "qif":
        from qdf_tools.sqlite_export import export_qif_to_sqlite

        count = export_qif_to_sqlite(
            source,
            output,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    else:
        from qdf_tools.qdb_financial import export_qdb_financial_to_sqlite

        count = export_qdb_financial_to_sqlite(
            source,
            output,
            start_date=args.start_date,
            end_date=args.end_date,
            qdf_path=qdf_path,
        )
    print(f"SQLite export: {count:,} transactions written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
