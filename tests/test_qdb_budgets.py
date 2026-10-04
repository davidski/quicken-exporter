import datetime as dt
import sqlite3
import struct

from qdf_tools.qdb_budgets import (
    parse_qdb_budget_headers,
    parse_qdb_budget_years,
    write_qdb_budgets_to_sqlite,
)
from qdf_tools.qdb_financial import write_qdb_category_catalog_to_sqlite
from qdf_tools.sqlite_export import write_transactions


def _variable_extract(base_size, records):
    data = bytearray(struct.pack("<4sIII", b"QVAR", 1, base_size, len(records)))
    for key, record in records:
        data.extend(struct.pack("<II", key, len(record)))
        data.extend(record)
    return bytes(data)


def test_parse_budget_header_and_variable_year(tmp_path):
    header = bytearray(256)
    struct.pack_into("<I", header, 0, 15)
    header[4:10] = b"CARVE\0"
    header_path = tmp_path / "headers.bin"
    header_path.write_bytes(struct.pack("<II", 256, 1) + header)

    year = bytearray(512)
    struct.pack_into("<IIIH", year, 0, 122, 15, 0x007E0101, 1)
    struct.pack_into("<II", year, 256, 4172, 0x10)
    struct.pack_into("<12q", year, 288, *range(12))
    year_path = tmp_path / "years.bin"
    year_path.write_bytes(struct.pack("<4sIIIII", b"QVAR", 1, 512, 1, 28, len(year)) + year)

    headers = parse_qdb_budget_headers(header_path)
    years = parse_qdb_budget_years(year_path)
    assert [(header.qid, header.name) for header in headers] == [(15, "CARVE")]
    assert years[0].qid == 122
    assert years[0].budget_qid == 15
    assert years[0].date == dt.date(2026, 1, 1)
    assert years[0].item_count == 1


def test_parse_budget_headers_accepts_variable_extract(tmp_path):
    header = bytearray(256)
    struct.pack_into("<I", header, 0, 15)
    header[4:10] = b"CARVE\0"
    path = tmp_path / "headers.bin"
    path.write_bytes(_variable_extract(256, [(2, header)]))

    assert [(item.key, item.qid, item.name) for item in parse_qdb_budget_headers(path)] == [
        (2, 15, "CARVE")
    ]


def test_write_qdb_budgets_materializes_all_budgets_and_preserves_category_join(tmp_path):
    header_one = bytearray(256)
    struct.pack_into("<I", header_one, 0, 15)
    header_one[4:10] = b"CARVE\0"
    header_two = bytearray(256)
    struct.pack_into("<I", header_two, 0, 16)
    header_two[4:14] = b"SECONDARY\0"
    (tmp_path / "qdb-type-144.bin").write_bytes(
        _variable_extract(256, [(2, header_one), (3, header_two)])
    )

    year_one = bytearray(512)
    struct.pack_into("<IIIH", year_one, 0, 101, 15, 0x007E0101, 1)
    struct.pack_into("<II", year_one, 256, 4172, 0x810)
    struct.pack_into("<12q", year_one, 288, 1234, *([0] * 11))
    struct.pack_into("<12q", year_one, 384, 56, *([0] * 11))
    year_two = bytearray(512)
    struct.pack_into("<IIIH", year_two, 0, 102, 16, 0x007D0101, 1)
    struct.pack_into("<II", year_two, 256, 4173, 0x200)
    struct.pack_into("<12q", year_two, 288, -987, *([0] * 11))
    (tmp_path / "qdb-type-14b-full.bin").write_bytes(
        _variable_extract(512, [(109, year_one), (110, year_two)])
    )

    catalog = bytearray(850 * 2)
    for offset, qid, name in ((0, 4172, "Groceries"), (850, 4173, "Utilities")):
        struct.pack_into("<I", catalog, offset, qid)
        catalog[offset + 4] = 1
        encoded = name.encode() + b"\0"
        catalog[offset + 5 : offset + 5 + len(encoded)] = encoded
    (tmp_path / "qdb-type-080.bin").write_bytes(struct.pack("<II", 850, 2) + catalog)

    destination = tmp_path / "budgets.sqlite"
    write_transactions(destination, [], source_format="test-qdb")
    write_qdb_category_catalog_to_sqlite(tmp_path, destination)
    assert write_qdb_budgets_to_sqlite(tmp_path, destination) == (2, 2, 24)

    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT COUNT(DISTINCT budget_qid) FROM budgets").fetchone() == (
            2,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT budget_qid, year FROM budgets)"
        ).fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM budgets").fetchone() == (24,)
        assert connection.execute(
            "SELECT budget_name, year, category_qid, month, "
            "budget_amount, secondary_amount "
            "FROM budgets JOIN categories ON categories.qdb_handle = budgets.category_qid "
            "WHERE month = 1 ORDER BY budget_qid"
        ).fetchall() == [
            ("CARVE", 2026, 4172, 1, "12.34", "0.56"),
            ("SECONDARY", 2025, 4173, 1, "-9.87", "0"),
        ]
        assert connection.execute(
            "SELECT budget_name, year, category_name, budget_amount, "
            "budget_amount_numeric, secondary_amount, secondary_amount_numeric "
            "FROM budget_analytics WHERE month = 1 ORDER BY budget_qid"
        ).fetchall() == [
            ("CARVE", 2026, "Groceries", "12.34", 12.34, "0.56", 0.56),
            ("SECONDARY", 2025, "Utilities", "-9.87", -9.87, "0", 0.0),
        ]
        assert connection.execute(
            "SELECT category_name, rollover_enabled, has_manual_rollover_amount "
            "FROM budget_analytics WHERE month = 1 ORDER BY budget_qid"
        ).fetchall() == [("Groceries", 1, 1), ("Utilities", 0, 0)]
        table_columns = [row[1] for row in connection.execute("PRAGMA table_info(budgets)")]
        assert "category_name" not in table_columns
        assert table_columns.index("month") == table_columns.index("year") + 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name IN ('budget_amounts', 'budget_years')"
        ).fetchone() == (0,)
        generated_columns = {row[1] for row in connection.execute("PRAGMA table_xinfo(budgets)")}
        assert {"budget_amount_numeric", "secondary_amount_numeric"} <= generated_columns
        view_columns = [row[1] for row in connection.execute("PRAGMA table_info(budget_analytics)")]
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'view' AND name = 'budget_amounts_analytics'"
        ).fetchone() == (0,)
        assert view_columns == [
            "budget_qid",
            "budget_name",
            "year",
            "month",
            "item_index",
            "category_qid",
            "category_name",
            "budget_amount",
            "secondary_amount",
            "budget_amount_numeric",
            "secondary_amount_numeric",
            "rollover_enabled",
            "has_manual_rollover_amount",
        ]


def test_category_catalog_uses_handles_for_account_exclusion(tmp_path):
    def catalog_record(handle, type_code, name):
        record = bytearray(850)
        struct.pack_into("<I", record, 0, handle)
        record[4] = type_code
        encoded = name.encode() + b"\0"
        record[5 : 5 + len(encoded)] = encoded
        return record

    catalog = b"".join(
        (
            catalog_record(7, 3, "Checking"),
            catalog_record(4172, 1, "Checking"),
        )
    )
    (tmp_path / "qdb-type-080.bin").write_bytes(struct.pack("<II", 850, 2) + catalog)

    destination = tmp_path / "categories.sqlite"
    write_transactions(destination, [], source_format="test-qdb")
    assert write_qdb_category_catalog_to_sqlite(tmp_path, destination) == 1

    with sqlite3.connect(destination) as connection:
        assert connection.execute(
            "SELECT name, qdb_handle FROM categories ORDER BY qdb_handle"
        ).fetchall() == [("Checking", 4172)]
