import sqlite3
import struct

from qdf_tools.qdb_reports import (
    parse_qdb_reports,
    report_type_name,
    write_qdb_reports_to_sqlite,
)


def _report_extract(
    rounding_checked: int = 1,
    date_range: int = 23,
    interval: int = 18,
    header_setting: int = 2,
    report_type: int = 13,
    category_ref: int | None = None,
    report_option: int = 0,
    status_words: tuple[int, int] | None = None,
    group9_ref: int | None = None,
) -> bytes:
    filter_size = (
        281 + (2 if category_ref is not None else 0) + (2 if group9_ref is not None else 0)
    )
    report_filter = bytearray(filter_size)
    struct.pack_into("<H", report_filter, 0x18 + 6 * 2, 64)
    struct.pack_into("<64H", report_filter, 0x98, 1, 14, *([0] * 61), 0xFFFF)
    if category_ref is not None:
        struct.pack_into("<H", report_filter, 0x18 + 7 * 2, 1)
        struct.pack_into("<H", report_filter, 0x118, category_ref)
    if group9_ref is not None:
        struct.pack_into("<H", report_filter, 0x18 + 9 * 2, 1)
        group9_offset = 0x118 + (2 if category_ref is not None else 0)
        struct.pack_into("<H", report_filter, group9_offset, group9_ref)
    if status_words is not None:
        struct.pack_into("<H", report_filter, 0x0C, status_words[0])
        struct.pack_into("<H", report_filter, 0x10, status_words[1])

    record = bytearray(466 + len(report_filter))
    struct.pack_into("<IIIIII", record, 0, 66, 6381, 12, 124, 1, len(record))
    record[0x78 : 0x78 + len(b"CARVINGREPORT\0")] = b"CARVINGREPORT\0"
    struct.pack_into("<I", record, 0x78 + 0x46, report_type)
    struct.pack_into("<H", record, 0x78 + 0x42, header_setting)
    struct.pack_into("<H", record, 0x78 + 0x4A, 1)
    struct.pack_into("<H", record, 0x78 + 0x4E, report_option)
    struct.pack_into("<H", record, 0x78 + 0x52, date_range)
    struct.pack_into("<H", record, 0x78 + 0x56, interval)
    struct.pack_into("<H", record, 0x78 + 0x9E, rounding_checked)
    struct.pack_into("<I", record, 0x78 + 0x112, len(report_filter))
    record[466:] = report_filter
    return struct.pack("<4sIII", b"QRPT", 1, 466, 1) + struct.pack("<II", 21, len(record)) + record


def _catalog() -> bytes:
    records = bytearray(850 * 2)
    struct.pack_into("<I", records, 0, 1)
    records[4] = 3
    records[5 : 5 + len(b"Checking (Main)\0")] = b"Checking (Main)\0"
    struct.pack_into("<I", records, 850, 14)
    records[854] = 3
    records[855 : 855 + len(b"Checking (Pat)\0")] = b"Checking (Pat)\0"
    return struct.pack("<II", 850, 2) + records


def test_parse_qdb_reports_recovers_carving_report(tmp_path):
    source = tmp_path / "qdb-reports.bin"
    source.write_bytes(_report_extract())

    reports = parse_qdb_reports(source)

    assert len(reports) == 1
    report = reports[0]
    assert (report.qdb_index, report.qdb_qid, report.name, report.report_type) == (
        21,
        66,
        "CARVINGREPORT",
        13,
    )
    account_filter = report.components[0].report_filter.groups[6]
    component = report.components[0]
    assert component.organization_code == 2
    assert component.organization == "Cash flow basis"
    assert component.header_setting_code == 2
    assert component.header_setting == "Cash flow basis"
    assert component.rounding_code == 1
    assert component.rounding_checked is True
    assert component.rounding == "Cents (no rounding)"
    assert component.date_range_code == 23
    assert component.date_range == "Last 12 months"
    assert component.interval_code == 18
    assert component.interval == "Month"
    assert component.category_filter_mode_code is None
    assert component.category_filter_mode == "Include values with any categories"
    assert component.account_filter_mode == "Only selected accounts"
    assert dict(component.setting_words)[0x42] == 2
    assert account_filter.kind == "account"
    assert account_filter.capacity == 64
    assert account_filter.values == ((0, 1), (1, 14))
    assert report_type_name(13) == "Cash Flow (graph)"
    assert report_type_name(99) == "Unknown report type 99"


def test_write_qdb_reports_to_existing_sqlite(tmp_path):
    source = tmp_path / "qdb-reports.bin"
    source.write_bytes(_report_extract())
    catalog = tmp_path / "qdb-type-080.bin"
    catalog.write_bytes(_catalog())
    destination = tmp_path / "financial.sqlite"
    with sqlite3.connect(destination) as connection:
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES ('schema_version', '2')")

    assert write_qdb_reports_to_sqlite(source, destination, catalog) == 1

    with sqlite3.connect(destination) as connection:
        assert connection.execute(
            "SELECT name, report_type, report_type_name, record_size FROM reports"
        ).fetchone() == ("CARVINGREPORT", 13, "Cash Flow (graph)", 747)
        assert connection.execute(
            """SELECT entity_ref, entity_name
               FROM report_filter_values AS value
               JOIN report_filter_groups AS filter_group
                 ON filter_group.id = value.filter_group_id
               WHERE filter_group.semantic_kind = 'account'
               ORDER BY value.slot"""
        ).fetchall() == [(1, "Checking (Main)"), (14, "Checking (Pat)")]
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'saved_report_count'"
        ).fetchone() == ("1",)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone() == ("38",)
        for table in ("reports", "report_components", "report_filter_groups"):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            assert not any("raw" in column for column in columns)
        assert connection.execute(
            """SELECT report_name, report_type_name, selected_account_count,
                      selected_accounts, selected_category_count, selected_categories
               FROM report_readable"""
        ).fetchone() == (
            "CARVINGREPORT",
            "Cash Flow (graph)",
            2,
            "Checking (Main), Checking (Pat)",
            0,
            "",
        )
        assert connection.execute(
            """SELECT organization_code, organization,
                      header_setting_code, header_setting,
                      rounding_code, rounding, rounding_checked,
                      date_range_code, date_range, interval_code, interval,
                      account_filter_mode,
                      category_filter_mode_code, category_filter_mode,
                      category_filter_value_count, category_filter_values
               FROM report_readable"""
        ).fetchone() == (
            2,
            "Cash flow basis",
            2,
            "Cash flow basis",
            1,
            "Cents (no rounding)",
            1,
            23,
            "Last 12 months",
            18,
            "Month",
            "Only selected accounts",
            None,
            "Include values with any categories",
            0,
            "",
        )
        assert connection.execute(
            """SELECT subtotal, sort_by, transfer_mode_code, transfer_mode,
                          subcategory_mode_code, subcategory_mode,
                          status_not_cleared, status_newly_cleared, status_reconciled
                   FROM report_readable"""
        ).fetchone() == (None, None, 0, None, 0, None, None, None, None)
        assert connection.execute(
            "SELECT COUNT(*) FROM report_component_setting_words"
        ).fetchone() == (141,)
        assert connection.execute("SELECT COUNT(*) FROM report_filter_header_words").fetchone() == (
            76,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'saved_report%'"
        ).fetchone() == (0,)


def test_parse_qdb_reports_decodes_category_filter_modes(tmp_path):
    source = tmp_path / "qdb-reports.bin"
    source.write_bytes(_report_extract(category_ref=14))

    component = parse_qdb_reports(source)[0].components[0]

    assert component.category_filter_mode_code is None
    assert component.category_filter_mode == "Selected categories only"

    source.write_bytes(_report_extract(rounding_checked=0, date_range=2))
    component = parse_qdb_reports(source)[0].components[0]

    assert component.rounding_checked is False
    assert component.rounding is None
    assert component.date_range == "Year to date"


def test_parse_qdb_reports_decodes_custom_dates(tmp_path):
    source = tmp_path / "qdb-reports.bin"
    data = bytearray(_report_extract(date_range=0xFFFF))
    record_start = 16 + 8
    for offset, value in ((0x5A, 0x0101), (0x5C, 0x007C), (0x5E, 0x0C1F), (0x60, 0x007C)):
        struct.pack_into("<H", data, record_start + 0x78 + offset, value)
    source.write_bytes(data)

    component = parse_qdb_reports(source)[0].components[0]

    assert component.date_range == "Custom range"
    assert component.custom_start_date.isoformat() == "2024-01-01"
    assert component.custom_end_date.isoformat() == "2024-12-31"


def test_parse_qdb_reports_decodes_confirmed_display_controls(tmp_path):
    source = tmp_path / "qdb-reports.bin"

    itemized = bytearray(_report_extract(header_setting=10, report_type=7, category_ref=14))
    itemized_record_start = 16 + 8
    itemized_component_start = itemized_record_start + 0x78
    struct.pack_into("<H", itemized, itemized_component_start + 0x96, 2)
    struct.pack_into("<H", itemized, itemized_component_start + 0x9A, 0)
    itemized_filter_start = itemized_record_start + 466
    struct.pack_into("<H", itemized, itemized_filter_start + 0x0C, 0x9FFE)
    struct.pack_into("<H", itemized, itemized_filter_start + 0x10, 0x9FFF)
    source.write_bytes(itemized)

    component = parse_qdb_reports(source)[0].components[0]
    assert component.header_setting == "Sort by: Account/Date"
    assert component.sort_by == "Account/Date"
    assert component.account_filter_mode == "Include all accounts"
    assert component.category_filter_mode == "Include all categories"
    assert component.transfer_mode == "Exclude internal"
    assert component.subcategory_mode == "Show all"
    assert component.status_not_cleared is True
    assert component.status_newly_cleared is True
    assert component.status_reconciled is True

    grocery = bytearray(
        _report_extract(rounding_checked=0, date_range=0xFFFF, report_type=4, category_ref=14)
    )
    grocery_component_start = 16 + 8 + 0x78
    struct.pack_into("<H", grocery, grocery_component_start + 0x96, 1)
    struct.pack_into("<H", grocery, grocery_component_start + 0x4E, 0x020B)
    struct.pack_into("<H", grocery, grocery_component_start + 0x9A, 3)
    source.write_bytes(grocery)

    component = parse_qdb_reports(source)[0].components[0]
    assert component.subtotal == "Don't subtotal"
    assert component.sort_by == "Date/Account"
    assert component.account_filter_mode == "Include all accounts"
    assert component.category_filter_mode == "Include only selected categories"
    assert component.transfer_mode == "Include all"
    assert component.subcategory_mode == "Show all"


def test_parse_qdb_reports_decodes_uncleared_and_household_controls(tmp_path):
    source = tmp_path / "qdb-reports.bin"

    source.write_bytes(
        _report_extract(
            rounding_checked=0,
            date_range=23,
            report_type=4,
            report_option=0x020D,
            status_words=(0x9FEE, 0x9FFF),
        )
    )
    component = parse_qdb_reports(source)[0].components[0]
    assert component.rounding_checked is True
    assert component.transfer_mode == "Exclude self-transfers"
    assert component.status_not_cleared is True
    assert component.status_newly_cleared is False
    assert component.status_reconciled is False

    source.write_bytes(
        _report_extract(
            date_range=0xFFFF,
            report_type=32,
            report_option=0x020B,
            status_words=(0xFF7F, 0xFFFF),
            group9_ref=11,
        )
    )
    component = parse_qdb_reports(source)[0].components[0]
    assert component.heading_column == "Year"
    assert component.tag_filter_mode == "Include only selected"
    assert component.transfer_mode == "Exclude self-transfers"
    assert component.subcategory_mode == "Hide all"
    assert component.status_not_cleared is True
    assert component.status_newly_cleared is True
    assert component.status_reconciled is True


def test_parse_qdb_reports_decodes_expense_summary_controls(tmp_path):
    source = tmp_path / "qdb-reports.bin"
    data = bytearray(
        _report_extract(
            rounding_checked=0,
            date_range=23,
            header_setting=0,
            report_type=14,
            category_ref=14,
            report_option=0x020D,
            status_words=(0xFFFE, 0xFFFF),
        )
    )
    component_start = 16 + 8 + 0x78
    struct.pack_into("<H", data, component_start + 0x9A, 1)
    source.write_bytes(data)

    component = parse_qdb_reports(source)[0].components[0]
    assert component.heading_row == "Category"
    assert component.heading_column == "Don't subtotal"
    assert component.subtotal == "Don't subtotal"
    assert component.category_filter_mode == "Include only selected categories"
    assert component.transfer_mode == "Exclude all"
    assert component.subcategory_mode == "Show all"
    assert component.status_not_cleared is None
    assert component.status_newly_cleared is True
    assert component.status_newly_reconciled is True
    assert component.status_reconciled is True


def test_parse_qdb_reports_decodes_budget_portfolio_and_cash_flow_variants(tmp_path):
    source = tmp_path / "qdb-reports.bin"

    source.write_bytes(
        _report_extract(
            report_type=20,
            header_setting=2,
            date_range=23,
            interval=18,
            report_option=0x020D,
        )
    )
    component = parse_qdb_reports(source)[0].components[0]
    assert component.header_setting is None
    assert component.organization is None
    assert component.budget is None

    source.write_bytes(
        _report_extract(
            report_type=20,
            header_setting=2,
            date_range=3,
            interval=0,
            report_option=0x0208,
        )
    )
    component = parse_qdb_reports(source)[0].components[0]
    assert component.date_range == "Last month"
    assert component.interval is None
    assert component.budget is None
    source.write_bytes(
        _report_extract(
            report_type=1,
            header_setting=2,
            interval=0xFFFF,
            report_option=0x020F,
        )
    )
    component = parse_qdb_reports(source)[0].components[0]
    assert component.header_setting is None
    assert component.interval == "Year"

    source.write_bytes(
        _report_extract(
            report_type=2,
            header_setting=0,
            interval=0xFFFF,
            report_option=0x020F,
            rounding_checked=0,
        )
    )
    component = parse_qdb_reports(source)[0].components[0]
    assert component.interval == "Year"
    assert component.rounding_checked is False
    assert component.transfer_mode is None

    cash_table = bytearray(
        _report_extract(
            report_type=29,
            category_ref=14,
            header_setting=2,
            date_range=23,
            interval=18,
            report_option=0x020D,
            status_words=(0xFFFE, 0xFFFF),
        )
    )
    cash_table_component_start = 16 + 8 + 0x78
    struct.pack_into("<H", cash_table, cash_table_component_start + 0x96, 1)
    struct.pack_into("<H", cash_table, cash_table_component_start + 0x9A, 5)
    source.write_bytes(cash_table)
    component = parse_qdb_reports(source)[0].components[0]
    assert component.transfer_mode == "Exclude internal"
    assert component.subcategory_mode == "Hide all"
    assert component.status_not_cleared is True
    assert component.status_newly_cleared is True
    assert component.status_newly_reconciled is True
    assert component.status_reconciled is True
    checking = bytearray(
        _report_extract(
            report_type=29,
            header_setting=2,
            date_range=2,
            interval=18,
            report_option=0x0206,
            status_words=(0xFFFE, 0xFFFF),
        )
    )
    checking_component_start = 16 + 8 + 0x78
    struct.pack_into("<H", checking, checking_component_start + 0x96, 1)
    struct.pack_into("<H", checking, checking_component_start + 0x9A, 5)
    source.write_bytes(checking)
    component = parse_qdb_reports(source)[0].components[0]
    assert component.transfer_mode == "Exclude self-transfers"
    assert component.status_not_cleared is True
    assert component.status_newly_cleared is True
    assert component.status_newly_reconciled is None
    assert component.status_reconciled is True

    cash_flow = bytearray(_report_extract(report_type=13, header_setting=2, report_option=0x020D))
    cash_flow_component_start = 16 + 8 + 0x78
    struct.pack_into("<H", cash_flow, cash_flow_component_start + 0x96, 1)
    source.write_bytes(cash_flow)
    component = parse_qdb_reports(source)[0].components[0]
    assert component.organization == "Cash flow basis"
    assert component.transfer_mode == "Exclude self-transfers"

    uncleared = bytearray(
        _report_extract(
            rounding_checked=0,
            date_range=23,
            report_type=4,
            report_option=0x020D,
        )
    )
    uncleared_component_start = 16 + 8 + 0x78
    struct.pack_into("<H", uncleared, uncleared_component_start + 0x9A, 3)
    source.write_bytes(uncleared)
    component = parse_qdb_reports(source)[0].components[0]
    assert component.rounding_checked is True
    assert component.subcategory_mode == "Show all"
