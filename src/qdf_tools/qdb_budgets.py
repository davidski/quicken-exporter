"""Parsers for Quicken's API-decoded budget record families."""

from __future__ import annotations

import csv
import datetime as dt
import sqlite3
import struct
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

BUDGET_HEADER_SIZE = 256
BUDGET_ITEM_SIZE = 256


def _dollars(cents: int) -> str:
    return format(Decimal(cents) / Decimal(100), "f")


@dataclass(frozen=True)
class QdbBudgetHeader:
    """Represent one fixed-size or variable-size QDB budget header record."""

    key: int
    qid: int
    name: str
    record: bytes


@dataclass(frozen=True)
class QdbBudgetYear:
    """Represent one budget year record and its variable item payload."""

    key: int
    qid: int
    budget_qid: int
    date: dt.date
    item_count: int
    record: bytes


def _cstring(value: bytes) -> str:
    return value.split(b"\0", 1)[0].decode("cp1252", errors="replace").strip()


def _read_fixed_records(path: Path, expected_size: int) -> list[bytes]:
    data = path.read_bytes()
    if len(data) < 8:
        raise ValueError(f"truncated fixed-record extract: {path}")
    size, count = struct.unpack_from("<II", data)
    if size != expected_size or len(data) != 8 + size * count:
        raise ValueError(f"invalid fixed-record extract: {path}")
    return [data[8 + index * size : 8 + (index + 1) * size] for index in range(count)]


def _read_variable_records(path: Path, expected_base_size: int = 512) -> list[tuple[int, bytes]]:
    data = path.read_bytes()
    if len(data) < 16:
        raise ValueError(f"truncated variable-record extract: {path}")
    magic, version, base_size, count = struct.unpack_from("<4sIII", data)
    if magic != b"QVAR" or version != 1 or base_size != expected_base_size:
        raise ValueError(f"invalid variable-record header: {path}")
    offset = 16
    records = []
    for _ in range(count):
        if offset + 8 > len(data):
            raise ValueError(f"truncated variable-record index: {path}")
        key, size = struct.unpack_from("<II", data, offset)
        offset += 8
        if size < base_size or offset + size > len(data):
            raise ValueError(f"invalid variable record {key} in {path}")
        records.append((key, data[offset : offset + size]))
        offset += size
    if offset != len(data):
        raise ValueError(f"trailing data in variable-record extract: {path}")
    return records


def parse_qdb_budget_headers(path: str | Path) -> list[QdbBudgetHeader]:
    """Parse fixed or QVAR budget-header records from an extracted file."""
    path = Path(path)
    if path.read_bytes()[:4] == b"QVAR":
        records = _read_variable_records(path, BUDGET_HEADER_SIZE)
    else:
        records = [
            (key, record)
            for key, record in enumerate(_read_fixed_records(path, BUDGET_HEADER_SIZE), start=1)
        ]
    result = []
    for key, record in records:
        if len(record) != BUDGET_HEADER_SIZE:
            raise ValueError(f"budget header {key} size {len(record)} != 256")
        result.append(
            QdbBudgetHeader(
                key=key,
                qid=struct.unpack_from("<I", record)[0],
                name=_cstring(record[4:45]),
                record=record,
            )
        )
    return result


def write_qdb_budgets_to_sqlite(
    source: str | Path, destination: str | Path
) -> tuple[int, int, int]:
    """Materialize every extracted budget, year, and monthly amount row."""
    directory = Path(source)
    header_path = directory / "qdb-type-144.bin"
    year_path = directory / "qdb-type-14b-full.bin"
    if not header_path.exists() and not year_path.exists():
        return 0, 0, 0
    if not header_path.is_file() or not year_path.is_file():
        raise ValueError("budget extracts must include both type 0x144 and type 0x14b")

    headers = parse_qdb_budget_headers(header_path)
    years = parse_qdb_budget_years(year_path)
    header_qids = {header.qid for header in headers}
    missing_headers = sorted({year.budget_qid for year in years} - header_qids)
    if missing_headers:
        raise ValueError(
            "budget years reference missing budget headers: "
            + ", ".join(str(qid) for qid in missing_headers)
        )

    header_by_qid = {header.qid: header for header in headers}
    amount_rows = []
    for year in years:
        for item_index in range(year.item_count):
            start = BUDGET_ITEM_SIZE * (item_index + 1)
            item = year.record[start : start + BUDGET_ITEM_SIZE]
            category_qid, flags = struct.unpack_from("<II", item)
            budget_amounts = struct.unpack_from("<12q", item, 32)
            secondary_amounts = struct.unpack_from("<12q", item, 128)
            amount_rows.extend(
                (
                    year.budget_qid,
                    header_by_qid[year.budget_qid].name,
                    year.date.isoformat(),
                    year.date.year,
                    month,
                    year.item_count,
                    item_index,
                    category_qid,
                    flags,
                    _dollars(budget_amount),
                    _dollars(secondary_amount),
                )
                for month, (budget_amount, secondary_amount) in enumerate(
                    zip(budget_amounts, secondary_amounts, strict=True), start=1
                )
            )

    with sqlite3.connect(destination) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM budgets")
        connection.executemany(
            "INSERT INTO budgets("
            "budget_qid, budget_name, budget_date, year, month, item_count, item_index, "
            "category_qid, flags, budget_amount, secondary_amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            amount_rows,
        )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (
                ("budget_count", str(len(headers))),
                ("budget_year_count", str(len(years))),
                ("budget_amount_count", str(len(amount_rows))),
            ),
        )
    return len(headers), len(years), len(amount_rows)


def parse_qdb_budget_years(path: str | Path) -> list[QdbBudgetYear]:
    """Parse variable budget-year records and validate their item sizes."""
    result = []
    for key, record in _read_variable_records(Path(path)):
        qid, budget_qid, date_word = struct.unpack_from("<III", record)
        item_count = struct.unpack_from("<H", record, 12)[0]
        expected_size = (item_count + 1) * BUDGET_ITEM_SIZE
        if len(record) != expected_size:
            raise ValueError(f"budget year {key} size {len(record)} != expected {expected_size}")
        day = date_word & 0xFF
        month = (date_word >> 8) & 0xFF
        year = ((date_word >> 16) & 0xFF) + 1900
        try:
            date = dt.date(year, month, day)
        except ValueError as error:
            raise ValueError(f"invalid budget date 0x{date_word:08x}") from error
        result.append(
            QdbBudgetYear(
                key=key,
                qid=qid,
                budget_qid=budget_qid,
                date=date,
                item_count=item_count,
                record=record,
            )
        )
    return result


def parse_qdb_catalog_names(path: str | Path) -> dict[int, str]:
    """Parse category IDs and names from a fixed-size QDB catalog extract."""
    result = {}
    for record in _read_fixed_records(Path(path), 850):
        qid = struct.unpack_from("<I", record)[0]
        name = _cstring(record[5:])
        if name:
            result[qid] = name
    return result


def write_budget_exports(
    header_path: str | Path,
    year_path: str | Path,
    catalog_path: str | Path,
    output_directory: str | Path,
    selected_name: str | None = None,
) -> tuple[int, int]:
    """Write exact and tabular budget carves; return year and month-row counts."""
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    headers = parse_qdb_budget_headers(header_path)
    years = parse_qdb_budget_years(year_path)
    names = parse_qdb_catalog_names(catalog_path)
    if selected_name is not None:
        headers = [
            header for header in headers if header.name.casefold() == selected_name.casefold()
        ]
        if not headers:
            raise ValueError(f"budget not found: {selected_name}")
    selected_qids = {header.qid for header in headers}
    years = [year for year in years if year.budget_qid in selected_qids]
    header_by_qid = {header.qid: header for header in headers}

    with (output_directory / "budgets.tsv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(["key", "budget_qid", "name"])
        writer.writerows((header.key, header.qid, header.name) for header in headers)

    month_rows = 0
    with (output_directory / "budget-amounts.tsv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(
            [
                "budget_qid",
                "budget_name",
                "year_qid",
                "year",
                "item_index",
                "category_qid",
                "category_name",
                "flags",
                "month",
                "budget_amount",
                "secondary_amount",
            ]
        )
        for year in years:
            for item_index in range(year.item_count):
                start = BUDGET_ITEM_SIZE * (item_index + 1)
                item = year.record[start : start + BUDGET_ITEM_SIZE]
                category_qid, flags = struct.unpack_from("<II", item)
                budget_amounts = struct.unpack_from("<12q", item, 32)
                secondary_amounts = struct.unpack_from("<12q", item, 128)
                for month, (budget_amount, secondary_amount) in enumerate(
                    zip(budget_amounts, secondary_amounts, strict=True), start=1
                ):
                    writer.writerow(
                        [
                            year.budget_qid,
                            header_by_qid[year.budget_qid].name,
                            year.qid,
                            year.date.year,
                            item_index,
                            category_qid,
                            names.get(category_qid, ""),
                            f"0x{flags:08x}",
                            month,
                            _dollars(budget_amount),
                            _dollars(secondary_amount),
                        ]
                    )
                    month_rows += 1

    carve_name = selected_name or "all"
    safe_name = "".join(character if character.isalnum() else "_" for character in carve_name)
    with (output_directory / f"budget-{safe_name}.bin").open("wb") as stream:
        stream.write(struct.pack("<4sIII", b"QBUD", 1, len(headers), len(years)))
        for header in headers:
            stream.write(struct.pack("<II", header.key, len(header.record)))
            stream.write(header.record)
        for year in years:
            stream.write(struct.pack("<II", year.key, len(year.record)))
            stream.write(year.record)
    return len(years), month_rows
