import csv
import sqlite3
import struct

import pytest

from qdf_tools.qdb_reports import write_qdb_reports_to_sqlite
from qdf_tools.report_reproduction import (
    GROCERY_REPORT_NAME,
    UnsupportedReportError,
    get_report_renderer,
    reproduce_grocery_expenses,
    reproduce_saved_report,
    reproduce_type4_transactions,
    reproduce_uncleared_transactions,
    supported_report_types,
    write_grocery_expenses_tsv,
    write_uncleared_transactions_tsv,
)


def _grocery_report() -> bytes:
    report_filter = bytearray(155)
    struct.pack_into("<H", report_filter, 0x18 + 7 * 2, 1)
    struct.pack_into("<H", report_filter, 0x98, 101)

    record = bytearray(466 + len(report_filter))
    struct.pack_into("<IIIIII", record, 0, 66, 6381, 12, 124, 1, len(record))
    record[0x78 : 0x78 + len(b"Grocery expenses\0")] = b"Grocery expenses\0"
    component_start = 0x78
    struct.pack_into("<I", record, component_start + 0x46, 4)
    struct.pack_into("<H", record, component_start + 0x42, 0)
    struct.pack_into("<H", record, component_start + 0x4E, 0x020B)
    struct.pack_into("<H", record, component_start + 0x52, 0xFFFF)
    struct.pack_into("<H", record, component_start + 0x56, 0)
    struct.pack_into("<H", record, component_start + 0x9E, 0)
    for offset, value in ((0x5A, 0x0101), (0x5C, 0x007C), (0x5E, 0x0C1F), (0x60, 0x007C)):
        struct.pack_into("<H", record, component_start + offset, value)
    struct.pack_into("<H", record, component_start + 0x96, 1)
    struct.pack_into("<H", record, component_start + 0x9A, 3)
    struct.pack_into("<I", record, component_start + 0x112, len(report_filter))
    record[466:] = report_filter
    return struct.pack("<4sIII", b"QRPT", 1, 466, 1) + struct.pack("<II", 21, len(record)) + record


def _uncleared_report() -> bytes:
    report_filter = bytearray(0x98 + 4)
    struct.pack_into("<H", report_filter, 0x18 + 6 * 2, 2)
    struct.pack_into("<H", report_filter, 0x98, 10)
    struct.pack_into("<H", report_filter, 0x9A, 11)
    struct.pack_into("<H", report_filter, 0x0C, 0x9FEE)
    struct.pack_into("<H", report_filter, 0x10, 0x9FFF)

    record = bytearray(466 + len(report_filter))
    struct.pack_into("<IIIIII", record, 0, 66, 6381, 12, 124, 1, len(record))
    record[0x78 : 0x78 + len(b"Uncleared transactions\0")] = b"Uncleared transactions\0"
    component_start = 0x78
    struct.pack_into("<I", record, component_start + 0x46, 4)
    struct.pack_into("<H", record, component_start + 0x4E, 0x020D)
    struct.pack_into("<H", record, component_start + 0x52, 23)
    struct.pack_into("<H", record, component_start + 0x9E, 0)
    struct.pack_into("<H", record, component_start + 0x96, 2)
    struct.pack_into("<H", record, component_start + 0x9A, 3)
    struct.pack_into("<I", record, component_start + 0x112, len(report_filter))
    record[466:] = report_filter
    return struct.pack("<4sIII", b"QRPT", 1, 466, 1) + struct.pack("<II", 21, len(record)) + record


def _materialize_report(database, source):
    assert write_qdb_reports_to_sqlite(source, database) == 1


def _database(tmp_path):
    database = tmp_path / "financial.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('extract_date', '2024-01-04');
            CREATE TABLE accounts(id INTEGER PRIMARY KEY, name TEXT NOT NULL, qdb_handle INTEGER);
            CREATE TABLE categories(id INTEGER PRIMARY KEY, name TEXT NOT NULL, qdb_handle INTEGER);
            CREATE TABLE budgets(
                budget_qid INTEGER, budget_name TEXT, budget_date TEXT, year INTEGER,
                month INTEGER, item_count INTEGER, item_index INTEGER, category_qid INTEGER,
                flags INTEGER, budget_amount TEXT, secondary_amount TEXT
            );
            CREATE TABLE transactions(
                id INTEGER PRIMARY KEY, account_id INTEGER, transaction_date TEXT,
                amount TEXT, payee TEXT, category TEXT, memo TEXT, transfer_account TEXT, cleared TEXT
            );
            CREATE TABLE transaction_splits(
                id INTEGER PRIMARY KEY, transaction_id INTEGER, line_number INTEGER,
                category TEXT, transfer_account TEXT, memo TEXT, amount TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO accounts VALUES (?, ?, ?)", [(1, "Alpha", 10), (2, "Beta", 11)]
        )
        connection.executemany(
            "INSERT INTO categories VALUES (?, ?, ?)", [(1, "Food", 100), (2, "Utilities", 101)]
        )
        connection.executemany(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 2, "2024-01-02", "-10.00", "Market", "Food", "weekly", "", None),
                (2, 1, "2024-01-01", "-5.00", "Store", "Food", "produce", "", "R"),
                (3, 1, "2022-12-31", "-99.00", "Old", "Food", "outside", "", None),
                (4, 1, "2024-01-04", "-7.00", "Utility", "Utilities", "outside", "", None),
                (5, 2, "2024-01-03", "-5.00", "Market", "--Split--", "split", "", None),
                (6, 1, "2024-01-04", "-3.00", "Transfer", "", "", "Beta", None),
            ],
        )
        connection.executemany(
            "INSERT INTO transaction_splits VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 5, 1, "Food", "", "split produce", "-2.00"),
                (2, 5, 2, "Utilities", "", "split utility", "-3.00"),
            ],
        )
    return database


def test_reproduce_grocery_expenses_filters_dates_categories_and_splits(tmp_path):
    database = _database(tmp_path)
    source = tmp_path / "qdb-reports.bin"
    source.write_bytes(_grocery_report())
    _materialize_report(database, source)

    with sqlite3.connect(database) as connection:
        rows = reproduce_grocery_expenses(connection)

    assert [(row.transaction_id, row.split_line, row.category, row.amount) for row in rows] == [
        (2, None, "Food", -5),
        (1, None, "Food", -10),
        (5, 1, "Food", -2),
    ]
    assert [row.account for row in rows] == ["Alpha", "Beta", "Beta"]


def test_write_grocery_expenses_tsv(tmp_path):
    database = _database(tmp_path)
    source = tmp_path / "qdb-reports.bin"
    source.write_bytes(_grocery_report())
    _materialize_report(database, source)
    output = tmp_path / "grocery.tsv"

    with sqlite3.connect(database) as connection:
        assert write_grocery_expenses_tsv(connection, output) == 3

    with output.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.reader(stream, delimiter="\t"))
    assert rows[0] == [
        "Date",
        "Account",
        "Payee",
        "Category",
        "Memo",
        "Amount",
        "Transaction ID",
        "Split Line",
    ]
    assert rows[1][0:6] == ["2024-01-01", "Alpha", "Store", "Food", "produce", "-5.00"]


def test_reproduce_uncleared_transactions_applies_status_accounts_and_transfers(tmp_path):
    database = _database(tmp_path)
    source = tmp_path / "qdb-reports.bin"
    source.write_bytes(_uncleared_report())
    _materialize_report(database, source)

    with sqlite3.connect(database) as connection:
        rows = reproduce_uncleared_transactions(connection)

    assert [(row.transaction_id, row.split_line, row.category, row.amount) for row in rows] == [
        (1, None, "Food", -10),
        (5, 1, "Food", -2),
        (5, 2, "Utilities", -3),
        (4, None, "Utilities", -7),
    ]


def test_write_uncleared_transactions_tsv(tmp_path):
    database = _database(tmp_path)
    source = tmp_path / "qdb-reports.bin"
    source.write_bytes(_uncleared_report())
    _materialize_report(database, source)
    output = tmp_path / "uncleared.tsv"

    with sqlite3.connect(database) as connection:
        assert write_uncleared_transactions_tsv(connection, output) == 4


def test_reproduce_type4_transactions_is_name_independent(tmp_path):
    database = _database(tmp_path)
    source = tmp_path / "qdb-reports.bin"
    data = bytearray(_grocery_report())
    component_start = 16 + 8 + 0x78
    replacement = b"Any transaction report\0"
    data[component_start : component_start + 0x42] = replacement.ljust(0x42, b"\0")
    source.write_bytes(data)
    _materialize_report(database, source)
    source.unlink()

    with sqlite3.connect(database) as connection:
        rows = reproduce_type4_transactions(connection, "Any transaction report")

    output = tmp_path / "any-type4.tsv"
    assert reproduce_saved_report(database, "Any transaction report", output) == 3
    assert output.is_file()
    assert [(row.transaction_id, row.split_line) for row in rows] == [
        (2, None),
        (1, None),
        (5, 1),
    ]


def test_budget_renderer_preserves_budget_detail_rows(tmp_path):
    database = _database(tmp_path)
    source = tmp_path / "qdb-reports.bin"
    data = bytearray(_grocery_report())
    component_start = 16 + 8 + 0x78
    struct.pack_into("<I", data, component_start + 0x46, 20)
    source.write_bytes(data)
    _materialize_report(database, source)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO budgets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (7, "Test budget", "2024-01-01", 2024, 1, 1, 0, 100, 0x0800, "25.00", "2.50"),
        )
        connection.commit()
    output = tmp_path / "budget.tsv"

    assert reproduce_saved_report(database, GROCERY_REPORT_NAME, output) == 1
    with output.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.reader(stream, delimiter="\t"))

    assert rows[0] == [
        "Period",
        "Budget QID",
        "Budget",
        "Entity QID",
        "Entity Kind",
        "Entity Name",
        "Item Index",
        "Flags",
        "Budget Amount",
        "Secondary Amount",
        "Rollover Enabled",
    ]
    assert rows[1] == [
        "2024-01",
        "7",
        "Test budget",
        "100",
        "category",
        "Food",
        "0",
        "2048",
        "25.00",
        "2.50",
        "True",
    ]


def test_reproduce_saved_report_rejects_unknown_type4_transfer_setting(tmp_path):
    database = _database(tmp_path)
    source = tmp_path / "qdb-reports.bin"
    data = bytearray(_grocery_report())
    component_start = 16 + 8 + 0x78
    struct.pack_into("<H", data, component_start + 0x96, 99)
    source.write_bytes(data)
    _materialize_report(database, source)
    source.unlink()

    with sqlite3.connect(database) as connection:
        with pytest.raises(UnsupportedReportError, match="unsupported transfer setting"):
            reproduce_type4_transactions(connection, GROCERY_REPORT_NAME)


def test_renderer_registry_exposes_type4_renderer():
    renderer = get_report_renderer(4)

    assert renderer.report_type == 4
    assert renderer.output_format == "tsv"
    assert supported_report_types() == (1, 2, 4, 7, 13, 14, 20, 29, 32, 47, 55)


def test_reproduce_saved_report_rejects_unregistered_report_type(tmp_path):
    database = _database(tmp_path)
    source = tmp_path / "qdb-reports.bin"
    data = bytearray(_grocery_report())
    component_start = 16 + 8 + 0x78
    struct.pack_into("<I", data, component_start + 0x46, 99)
    source.write_bytes(data)
    _materialize_report(database, source)

    with pytest.raises(UnsupportedReportError, match="no renderer registered for report type 99"):
        reproduce_saved_report(database, GROCERY_REPORT_NAME, tmp_path / "report.tsv")
