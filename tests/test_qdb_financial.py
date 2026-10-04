import datetime as dt
import sqlite3
import struct
from decimal import Decimal

from qdf_tools.qdb_financial import (
    _investment_cash_delta,
    _read_qdb_investment_native_balances,
    export_qdb_financial_to_sqlite,
    parse_qdb_financial_extract,
    parse_qdb_register_transactions,
    parse_qdb_type13c,
    write_qdb_account_status_to_sqlite,
    write_qdb_investment_balance_periods_to_sqlite,
    write_qdb_investment_transactions_to_sqlite,
    write_qdb_register_balance_periods_to_sqlite,
    write_qdb_security_prices_to_sqlite,
)
from qdf_tools.qif import QifTransaction
from qdf_tools.qph import parse_qph_bytes
from qdf_tools.sqlite_export import write_transactions


def test_investment_cash_delta_ignores_security_only_legs():
    assert _investment_cash_delta("Added", Decimal("-100")) == Decimal(0)
    assert _investment_cash_delta("BoughtX", Decimal("-100")) == Decimal(0)
    assert _investment_cash_delta("Bought", Decimal("-100")) == Decimal("-100")
    assert _investment_cash_delta("XIn", Decimal("100")) == Decimal("100")
    assert _investment_cash_delta("ReinvInt", Decimal("100")) == Decimal(0)
    assert _investment_cash_delta("ReinvInt", Decimal("100"), True) == Decimal("100")


def test_parse_qdb_type13c(tmp_path):
    record = bytearray(961)
    struct.pack_into("<I", record, 0, 1)
    struct.pack_into("<I", record, 0x74, (104 << 24) | (10 << 16) | (21 << 8))

    struct.pack_into("<q", record, 0x61, -3500)
    record[0x15 : 0x15 + 6] = b"Coffee"
    record[0x81 : 0x81 + 7] = b"DDA1234"
    source = tmp_path / "qdb-type-13c.bin"
    source.write_bytes(struct.pack("<II", 961, 1) + record)

    transactions = parse_qdb_type13c(source)

    assert len(transactions) == 1
    assert transactions[0].date == dt.date(2004, 10, 21)
    assert transactions[0].amount == Decimal("-35")
    assert transactions[0].payee == "Coffee"
    assert transactions[0].number == "DDA1234"
    assert transactions[0].raw["qdb_type"] == "0x13c"


def test_qdb_register_clear_status_is_decoded(tmp_path):
    catalog = bytearray(850)
    struct.pack_into("<I", catalog, 0, 42)
    catalog[4] = 3
    catalog[5 : 5 + len(b"Checking\x00")] = b"Checking\x00"
    (tmp_path / "qdb-type-080.bin").write_bytes(struct.pack("<II", 850, 1) + catalog)

    records = []
    for register_ref, day in ((1001, 15), (1002, 27), (1003, 30)):
        record = bytearray(211)
        struct.pack_into("<I", record, 0, register_ref)
        struct.pack_into("<H", record, 4, 42)
        record[6:9] = bytes((day, 8, 126))
        records.append(record)
    (tmp_path / "qdb-type-0f7.bin").write_bytes(
        struct.pack("<II", 211, len(records)) + b"".join(records)
    )
    (tmp_path / "qdb-register-splits.tsv").write_text(
        "register_ref\tsplit_index\tcategory_handle\ttransfer_handle\tmemo_ref\tamount_cents\traw_hex\taccount\tkey\n"
        "142662\t0\t4198\t42\t0\t12345\t00\t14\t2626\n"
        "142662\t1\t5077\t42\t0\t-678\t00\t14\t2626\n"
        "142662\t2\t0\t42\t0\t0\t00\t14\t2626\n",
        encoding="utf-8",
    )
    (tmp_path / "qdb-register-clear-status.tsv").write_text(
        "register_ref\taccount\tkey\tclear_status\n"
        "1001\t42\t1\t2\n"
        "1002\t42\t2\t0\n"
        "1003\t42\t3\t1\n",
        encoding="utf-8",
    )

    transactions = parse_qdb_register_transactions(tmp_path)

    assert [transaction.cleared for transaction in transactions] == ["R", None, "c"]
    assert [transaction.raw["qdb_clear_status"] for transaction in transactions] == [2, 0, 1]

    destination = tmp_path / "financial.sqlite"
    write_transactions(destination, transactions, source_format="test-qdb")
    with sqlite3.connect(destination) as connection:
        assert connection.execute(
            "SELECT cleared FROM transactions ORDER BY transaction_date"
        ).fetchall() == [("R",), (None,), ("c",)]


def test_read_qdb_native_investment_balances(tmp_path):
    (tmp_path / "qdb-investment-transactions.tsv").write_text(
        "register_ref\taccount\tkey\tsecurity_ref\tshares\tprice\t"
        "investment_amount\ttransaction_amount\tbackfill_pair\t"
        "is_backfill_cash\ttransfer_qid\txfer_account\tstart_location\t"
        "destination_location\tnative_cash_balance\tinv_txn_type\t"
        "inv_txn_type_name\tis_cash\ttransaction_date\n"
        "1\t77\t3\t0\t0\t0\t0\t0\t0\t0\t0\t0\t\t\t17423736\t"
        "0\tCash\t1\t2024-01-01\n",
        encoding="utf-8",
    )

    assert _read_qdb_investment_native_balances(tmp_path) == {(77, 3): Decimal("174237.36")}


def test_write_qdb_account_status_to_sqlite(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(destination, [], source_format="test-qdb")
    with sqlite3.connect(destination) as db:
        db.execute(
            "INSERT INTO accounts(name, account_type, qdb_handle) VALUES (?, ?, ?)",
            ("Checking", "Banking", 7),
        )
    (tmp_path / "qdb-account-status.tsv").write_text(
        "qdb_handle\taccount_type\tis_house\tis_closed\tis_separate\tis_hidden_in_bar\tis_hidden_in_list\n"
        "7\t2\t1\t1\t1\t0\t1\n",
        encoding="utf-8",
    )

    assert write_qdb_account_status_to_sqlite(tmp_path, destination) == 1
    with sqlite3.connect(destination) as db:
        assert db.execute(
            "SELECT account_type, is_closed, is_separate, is_hidden, is_hidden_in_bar, "
            "is_hidden_in_list FROM accounts WHERE qdb_handle = 7"
        ).fetchone() == ("House", 1, 1, 1, 0, 1)


def test_write_qdb_account_status_materializes_native_catalog_accounts(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(destination, [], source_format="test-qdb")
    record = bytearray(850)
    struct.pack_into("<I", record, 0, 9)
    record[4] = 6
    record[5 : 5 + len(b"Main Home") + 1] = b"Main Home\x00"
    asset = bytearray(850)
    struct.pack_into("<I", asset, 0, 10)
    asset[4] = 6
    asset[5 : 5 + len(b"Car One") + 1] = b"Car One\x00"
    (tmp_path / "qdb-type-080.bin").write_bytes(struct.pack("<II", 850, 2) + record + asset)
    (tmp_path / "qdb-account-status.tsv").write_text(
        "qdb_handle\taccount_type\tis_house\tis_closed\tis_separate\tis_hidden_in_bar\tis_hidden_in_list\n"
        "9\t2\t1\t0\t0\t0\t0\n"
        "10\t2\t0\t0\t0\t0\t0\n",
        encoding="utf-8",
    )

    assert write_qdb_account_status_to_sqlite(tmp_path, destination) == 2
    with sqlite3.connect(destination) as db:
        assert db.execute(
            "SELECT name, account_type, qdb_handle FROM accounts ORDER BY qdb_handle"
        ).fetchall() == [("Main Home", "House", 9), ("Car One", "Asset", 10)]


def test_write_qdb_account_status_classifies_asset_subtypes(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(destination, [], source_format="test-qdb")
    catalog = bytearray(850 * 2)
    for offset, handle, name in (
        (0, 9, "Main Home"),
        (850, 10, "Car One"),
    ):
        struct.pack_into("<I", catalog, offset, handle)
        catalog[offset + 4] = 6
        encoded = name.encode() + b"\x00"
        catalog[offset + 5 : offset + 5 + len(encoded)] = encoded
    (tmp_path / "qdb-type-080.bin").write_bytes(struct.pack("<II", 850, 2) + catalog)
    (tmp_path / "qdb-account-status.tsv").write_text(
        "qdb_handle\taccount_type\taccount_subtype\tis_closed\tis_separate\t"
        "is_hidden_in_bar\tis_hidden_in_list\n"
        "9\t2\t3\t0\t0\t0\t0\n"
        "10\t2\t5\t0\t0\t0\t0\n",
        encoding="utf-8",
    )

    assert write_qdb_account_status_to_sqlite(tmp_path, destination) == 2
    with sqlite3.connect(destination) as db:
        assert db.execute(
            "SELECT name, account_type FROM accounts ORDER BY qdb_handle"
        ).fetchall() == [("Main Home", "House"), ("Car One", "Vehicle")]
        assert db.execute(
            'SELECT value FROM metadata WHERE key = "account_subtype_source"'
        ).fetchone() == ("qdb-access-native-account-subtype",)


def test_export_qdb_financial_always_excludes_internal_accounts(tmp_path):
    source = tmp_path / "extract"
    source.mkdir()
    (source / "qdb-type-13c.bin").write_bytes(struct.pack("<II", 961, 0))
    catalog = bytearray(850 * 2)
    for offset, handle, name in (
        (0, 1, "Tax Impact of 401(k) Accounts"),
        (850, 2, "Unspecified Bill Presentment Account"),
    ):
        struct.pack_into("<I", catalog, offset, handle)
        catalog[offset + 4] = 3
        encoded = name.encode() + b"\x00"
        catalog[offset + 5 : offset + 5 + len(encoded)] = encoded
    (source / "qdb-type-080.bin").write_bytes(struct.pack("<II", 850, 2) + catalog)
    (source / "qdb-type-134.bin").write_bytes(struct.pack("<II", 211, 0))
    (source / "qdb-account-map-134.bin").write_bytes(struct.pack("<4sIII", b"QATM", 1, 211, 0))
    (source / "qdb-account-status.tsv").write_text(
        "qdb_handle\taccount_type\tis_house\tis_closed\tis_separate\t"
        "is_hidden_in_bar\tis_hidden_in_list\n"
        "1\t0\t0\t0\t0\t0\t0\n"
        "2\t0\t0\t0\t0\t0\t0\n",
        encoding="utf-8",
    )

    destination = tmp_path / "financial.sqlite"
    assert export_qdb_financial_to_sqlite(source, destination) == 0
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT name FROM accounts").fetchall() == []


def test_write_qdb_security_prices_to_sqlite(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(destination, [], source_format="test-qdb")
    (tmp_path / "qdb-securities.tsv").write_text(
        "qdb_security_ref\tname\tsymbol\n7\tVanguard Example\tVEX\n9\tPrivate Holding\t\n",
        encoding="utf-8",
    )
    (tmp_path / "qdb-price-history.tsv").write_text(
        "qdb_security_ref\tprice_date\tprice\thigh\tlow\tvolume\n"
        "7\t2024-01-02\t101.25\t102.5\t99.75\t12345\n"
        "9\t2024-01-03\t1\t\t\t\n",
        encoding="utf-8",
    )

    assert write_qdb_security_prices_to_sqlite(tmp_path, destination) == (2, 2)
    with sqlite3.connect(destination) as db:
        rows = db.execute(
            """SELECT securities.qdb_security_ref, securities.name, securities.symbol,
                      security_prices.price_date, security_prices.price,
                      security_prices.high, security_prices.low, security_prices.volume
               FROM security_prices
               JOIN securities ON securities.id = security_prices.security_id
               ORDER BY securities.qdb_security_ref"""
        ).fetchall()
        metadata = dict(
            db.execute(
                "SELECT key, value FROM metadata WHERE key IN ('security_count', 'security_price_count')"
            )
        )

    assert rows == [
        (7, "Vanguard Example", "VEX", "2024-01-02", "101.25", "102.5", "99.75", 12345),
        (9, "Private Holding", None, "2024-01-03", "1", None, None, None),
    ]
    assert metadata == {"security_count": "2", "security_price_count": "2"}


def test_parse_qph_bytes_associates_blocks_with_following_symbol_header():
    def price_value(text):
        value = Decimal(text)
        whole = int(value)
        fraction = int((value - whole) * Decimal(100000000))
        return ((0x80000000 + fraction) << 32) | whole

    def quote(offset, date_word, price, high, low, volume):
        record = bytearray(54)
        struct.pack_into("<I", record, 0, date_word)
        struct.pack_into("<I", record, 4, 60)
        struct.pack_into("<Q", record, 14, price_value(price))
        struct.pack_into("<Q", record, 22, price_value(high))
        struct.pack_into("<Q", record, 30, price_value(low))
        struct.pack_into("<i", record, 38, volume)
        struct.pack_into("<II", record, 42, 1, 1)
        struct.pack_into("<I", record, 50, offset + 50)
        return bytes(record)

    def header(symbol):
        return b"\x01\x00\x00\x00" + symbol.encode() + b"\x00" * (32 - len(symbol)) + b"\x00" * 15

    first_offset = 36
    first = quote(first_offset, 0x007E0814, "72.34", "72.50", "72.10", 4299)
    second_offset = first_offset + len(first) + 51
    second = quote(second_offset, 0x007E0813, "87.03", "0", "0", 10205)
    data = b"\x00" * 36 + first + header("VTI") + second + header("VXUS")

    quotes = parse_qph_bytes(data, {"VTI", "VXUS"})

    assert [(quote.symbol, quote.price_date, quote.price) for quote in quotes] == [
        ("VTI", dt.date(2026, 8, 20), "72.34"),
        ("VXUS", dt.date(2026, 8, 19), "87.03"),
    ]
    assert quotes[0].high == "72.5"
    assert quotes[0].low == "72.1"
    assert quotes[0].volume == 4299
    assert quotes[1].high is None
    assert quotes[1].low is None


def test_write_qdb_investment_transactions_to_sqlite(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(
        destination,
        [
            QifTransaction(
                account="Brokerage",
                account_type="Investing",
                date=dt.date(2024, 1, 2),
                amount=Decimal("-250.00"),
                number="native-fit",
                raw={"qdb_account_handle": 77, "qdb_internal_id": 123},
            )
        ],
        source_format="test-qdb",
    )
    (tmp_path / "qdb-securities.tsv").write_text(
        "qdb_security_ref\tname\tsymbol\n7\tExample Fund\tEXF\n",
        encoding="utf-8",
    )
    (tmp_path / "qdb-price-history.tsv").write_text(
        "qdb_security_ref\tprice_date\tprice\thigh\tlow\tvolume\n"
        "7\t2024-01-02\t101.25\t102\t100\t1000\n",
        encoding="utf-8",
    )
    record = bytearray(211)
    struct.pack_into("<I", record, 0, 123)
    (tmp_path / "qdb-account-map-134.bin").write_bytes(
        struct.pack("<4sIII", b"QATM", 1, 211, 1) + struct.pack("<II", 77, 3) + record
    )
    legacy = bytearray(961)
    struct.pack_into("<I", legacy, 0, 9001)
    legacy[0x81 : 0x81 + len("native-fit")] = b"native-fit"
    (tmp_path / "qdb-type-086.bin").write_bytes(struct.pack("<II", 961, 1) + legacy)
    (tmp_path / "qdb-investment-transactions.tsv").write_text(
        "register_ref\taccount\tkey\tsecurity_ref\tshares\tprice\tinvestment_amount\t"
        "transaction_amount\tbackfill_pair\tis_backfill_cash\ttransfer_qid\t"
        "xfer_account\tstart_location\tdestination_location\tnative_cash_balance\t"
        "inv_txn_type\tinv_txn_type_name\tis_cash\ttransaction_date\n"
        "9001\t77\t3\t7\t-2.5\t101.25\t-25000\t-25000\t9002\t1\t9002\t77\t\t\t-25000\t17\tSold\t0\t2024-01-02\n",
        encoding="utf-8",
    )

    assert write_qdb_security_prices_to_sqlite(tmp_path, destination) == (1, 1)
    assert write_qdb_investment_transactions_to_sqlite(tmp_path, destination) == (1, 1)
    assert write_qdb_investment_balance_periods_to_sqlite(None, destination) == 1
    with sqlite3.connect(destination) as db:
        assert (
            next(
                row[3]
                for row in db.execute("PRAGMA table_info(investment_transactions)")
                if row[1] == "native_cash_balance"
            )
            == 1
        )
        assert "register_ref" not in {
            row[1] for row in db.execute("PRAGMA table_info(investment_transactions)")
        }
        row = db.execute(
            """SELECT accounts.name, investment_transactions.transaction_id,
                      securities.name, investment_transactions.shares,
                      investment_transactions.price, investment_transactions.investment_amount,
                      investment_transactions.transaction_type,
                      investment_transactions.backfill_pair_ref,
                      investment_transactions.is_backfill_cash,
                      investment_transactions.transfer_qdb_register_ref,
                      investment_transactions.transfer_account,
                      investment_transactions.native_cash_balance
               FROM investment_transactions
               JOIN accounts ON accounts.id = investment_transactions.account_id
               LEFT JOIN securities ON securities.id = investment_transactions.security_id"""
        ).fetchone()
        transaction = db.execute("SELECT security, price, quantity FROM transactions").fetchone()
        balance = db.execute(
            "SELECT cash_balance, investment_value, total_value "
            "FROM investment_account_balance_periods"
        ).fetchone()
        position = db.execute(
            "SELECT shares, price, market_value FROM investment_position_balance_periods"
        ).fetchone()

    assert row == (
        "Brokerage",
        1,
        "Example Fund",
        "-2.5",
        "101.25",
        "-250",
        "Sold",
        9002,
        1,
        9002,
        77,
        "-250",
    )
    assert transaction == ("Example Fund", "101.25", "-2.5")
    assert balance == ("-250", "-253.125", "-503.125")
    assert position == ("-2.5", "101.25", "-253.125")


def test_investment_rows_do_not_join_by_account_key_ordinal(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(
        destination,
        [
            QifTransaction(
                account="Brokerage",
                account_type="Investing",
                date=dt.date(2024, 1, 2),
                amount=Decimal("10.00"),
                raw={"qdb_account_handle": 77, "qdb_internal_id": 123},
            )
        ],
        source_format="test-qdb",
    )
    record = bytearray(211)
    struct.pack_into("<I", record, 0, 123)
    (tmp_path / "qdb-account-map-134.bin").write_bytes(
        struct.pack("<4sIII", b"QATM", 1, 211, 1) + struct.pack("<II", 77, 3) + record
    )
    (tmp_path / "qdb-investment-transactions.tsv").write_text(
        "register_ref\taccount\tkey\tsecurity_ref\tshares\tprice\tinvestment_amount\t"
        "transaction_amount\tbackfill_pair\tis_backfill_cash\ttransfer_qid\t"
        "xfer_account\tstart_location\tdestination_location\tnative_cash_balance\t"
        "inv_txn_type\tinv_txn_type_name\tis_cash\ttransaction_date\n"
        "9001\t77\t3\t0\t0\t0\t1000\t1000\t0\t0\t0\t0\t\t\t1000\t22\tXIn\t1\t2024-01-02\n",
        encoding="utf-8",
    )

    assert write_qdb_investment_transactions_to_sqlite(tmp_path, destination) == (1, 0)
    with sqlite3.connect(destination) as db:
        assert db.execute("SELECT transaction_id FROM investment_transactions").fetchone() == (
            None,
        )


def test_write_qdb_register_balance_periods_uses_register_amounts(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(
        destination,
        [
            QifTransaction(
                account="Checking",
                account_type="Banking",
                date=dt.date(2024, 1, 1),
                amount=Decimal("999.00"),
                raw={"qdb_account_handle": 42},
            )
        ],
        source_format="test-qdb",
    )

    records = []
    for day, cents in ((1, 10000), (1, -2500), (3, 1000)):
        record = bytearray(211)
        struct.pack_into("<I", record, 0, len(records) + 1)
        struct.pack_into("<H", record, 4, 42)
        record[6:9] = bytes((day, 1, 124))
        struct.pack_into("<q", record, 0x24, cents)
        records.append(record)
    (tmp_path / "qdb-type-0f7.bin").write_bytes(
        struct.pack("<II", 211, len(records)) + b"".join(records)
    )

    assert write_qdb_register_balance_periods_to_sqlite(tmp_path, destination) == 3
    with sqlite3.connect(destination) as db:
        periods = db.execute(
            """SELECT balance_date, next_balance_date, balance_cents
               FROM banking_account_balance_intervals"""
        ).fetchall()
        metadata = dict(db.execute("SELECT key, value FROM metadata WHERE key LIKE 'balance_%'"))

    assert periods == [
        (None, "2024-01-01", 0),
        ("2024-01-01", "2024-01-03", 7500),
        ("2024-01-03", None, 8500),
    ]
    assert metadata == {
        "balance_period_count": "3",
        "balance_source": "qdb-register-0xf7-with-transaction-fallback",
    }


def test_parse_qdb_financial_extract_joins_account_name(tmp_path):
    record = bytearray(961)
    struct.pack_into("<I", record, 0, 1)
    struct.pack_into("<I", record, 0x74, (104 << 24) | (10 << 16) | (21 << 8))
    struct.pack_into("<q", record, 0x61, 1250)
    (tmp_path / "qdb-type-13c.bin").write_bytes(struct.pack("<II", 961, 1) + record)

    catalog = bytearray(850)
    struct.pack_into("<I", catalog, 0, 42)
    catalog[4] = 3
    catalog[5:16] = b"Checking X\x00"
    (tmp_path / "qdb-type-080.bin").write_bytes(struct.pack("<II", 850, 1) + catalog)

    index = bytearray(211)
    struct.pack_into("<II", index, 0, 1, 42)
    (tmp_path / "qdb-type-134.bin").write_bytes(struct.pack("<II", 211, 1) + index)
    (tmp_path / "qdb-account-map-134.bin").write_bytes(
        struct.pack("<4sIII", b"QATM", 1, 211, 1) + struct.pack("<II", 42, 1) + index
    )

    transactions = parse_qdb_financial_extract(tmp_path)

    assert transactions[0].account == "Checking X"
    assert transactions[0].account_type == "Banking"
    assert transactions[0].raw["qdb_account_handle"] == 42


def test_parse_qdb_financial_extract_resolves_category_string_slot(tmp_path):
    record = bytearray(961)
    struct.pack_into("<I", record, 0, 1)
    struct.pack_into("<I", record, 0x74, (104 << 24) | (10 << 16) | (21 << 8))
    struct.pack_into("<I", record, 0xA0, 9001)
    (tmp_path / "qdb-type-13c.bin").write_bytes(struct.pack("<II", 961, 1) + record)

    catalog = bytearray(850)
    struct.pack_into("<I", catalog, 0, 42)
    catalog[4] = 7
    catalog[5:16] = b"Checking X\x00"
    (tmp_path / "qdb-type-080.bin").write_bytes(struct.pack("<II", 850, 1) + catalog)

    index = bytearray(211)
    struct.pack_into("<II", index, 0, 1, 42)
    (tmp_path / "qdb-type-134.bin").write_bytes(struct.pack("<II", 211, 1) + index)
    (tmp_path / "qdb-account-map-134.bin").write_bytes(
        struct.pack("<4sIII", b"QATM", 1, 211, 1) + struct.pack("<II", 42, 1) + index
    )
    (tmp_path / "qdb-string-map.tsv").write_text("9001\t=Groceries\n", encoding="utf-8")

    transactions = parse_qdb_financial_extract(tmp_path)

    assert transactions[0].category == "Groceries"
    assert transactions[0].raw["qdb_category_string_id"] == 9001


def test_parse_qdb_financial_extract_resolves_category_handle(tmp_path):
    record = bytearray(961)
    struct.pack_into("<I", record, 0, 1)
    (tmp_path / "qdb-type-13c.bin").write_bytes(struct.pack("<II", 961, 1) + record)

    account = bytearray(850)
    struct.pack_into("<I", account, 0, 42)
    account[4] = 3
    account[5:14] = b"Checking\x00"
    category = bytearray(850)
    struct.pack_into("<I", category, 0, 4241)
    category[4] = 1
    category[5:15] = b"Utilities\x00"
    (tmp_path / "qdb-type-080.bin").write_bytes(struct.pack("<II", 850, 2) + account + category)

    index = bytearray(211)
    struct.pack_into("<I", index, 0, 1)
    struct.pack_into("<I", index, 0x4E, 4241)
    (tmp_path / "qdb-type-134.bin").write_bytes(struct.pack("<II", 211, 1) + index)
    (tmp_path / "qdb-account-map-134.bin").write_bytes(
        struct.pack("<4sIII", b"QATM", 1, 211, 1) + struct.pack("<II", 42, 1) + index
    )

    transaction = parse_qdb_financial_extract(tmp_path)[0]

    assert transaction.category == "Utilities"
    assert transaction.raw["qdb_category_string_id"] == 4241


def test_parse_qdb_financial_extract_prefers_normalized_pending_payee(tmp_path):
    record = bytearray(961)
    struct.pack_into("<I", record, 0, 1)
    struct.pack_into("<I", record, 0x74, (126 << 24) | (8 << 16) | (17 << 8))
    struct.pack_into("<q", record, 0x61, -300)
    record[0x15 : 0x15 + 17] = b"ACME NORTH GARAGE"
    record[0x81 : 0x81 + 11] = b"33164828940"
    header = struct.pack("<II", 961, 1)
    (tmp_path / "qdb-type-13c.bin").write_bytes(header + record)
    (tmp_path / "qdb-type-08e.bin").write_bytes(header + record)

    payee_record = bytearray(211)
    struct.pack_into("<I", payee_record, 0x63, 48039)
    struct.pack_into("<I", payee_record, 0x67, 52847)
    (tmp_path / "qdb-type-0b7.bin").write_bytes(struct.pack("<II", 211, 1) + payee_record)

    catalog = bytearray(850)
    struct.pack_into("<I", catalog, 0, 42)
    catalog[4] = 3
    catalog[5:14] = b"Checking\x00"
    (tmp_path / "qdb-type-080.bin").write_bytes(struct.pack("<II", 850, 1) + catalog)
    index = bytearray(211)
    struct.pack_into("<II", index, 0, 1, 42)
    (tmp_path / "qdb-type-134.bin").write_bytes(struct.pack("<II", 211, 1) + index)
    (tmp_path / "qdb-account-map-134.bin").write_bytes(
        struct.pack("<4sIII", b"QATM", 1, 211, 1) + struct.pack("<II", 42, 1) + index
    )
    (tmp_path / "qdb-string-map.tsv").write_text(
        "48039\tAcme Parking\n52847\tACME NORTH GARAGE\n", encoding="utf-8"
    )

    transaction = parse_qdb_financial_extract(tmp_path)[0]

    assert transaction.payee == "Acme Parking"
    assert transaction.downloaded_payee == "ACME NORTH GARAGE"
    assert transaction.payee_source == "QDB register displayed payee"
    assert "qdb_downloaded_payee" not in transaction.raw
    assert "qdb_payee_source" not in transaction.raw

    destination = tmp_path / "financial.sqlite"
    write_transactions(destination, [transaction], source_format="test-qdb")
    with sqlite3.connect(destination) as connection:
        payee_row = connection.execute(
            "SELECT payee, downloaded_payee, payee_source FROM transactions"
        ).fetchone()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(transactions)")}
    assert payee_row[:3] == (
        "Acme Parking",
        "ACME NORTH GARAGE",
        "QDB register displayed payee",
    )
    assert "raw_json" not in columns


def test_parse_qdb_financial_extract_uses_native_register_memo(tmp_path):
    record = bytearray(961)
    struct.pack_into("<I", record, 0, 1)
    struct.pack_into("<I", record, 0x74, (126 << 24) | (8 << 16) | (17 << 8))
    struct.pack_into("<q", record, 0x61, -10066)
    record[0x15:0x1B] = b"LTCINS"
    record[0x81 : 0x81 + 15] = b"202608050000001"
    (tmp_path / "qdb-type-13c.bin").write_bytes(struct.pack("<II", 961, 1) + record)

    legacy = bytearray(record)
    struct.pack_into("<I", legacy, 0, 142661)
    (tmp_path / "qdb-type-086.bin").write_bytes(struct.pack("<II", 961, 1) + legacy)
    (tmp_path / "qdb-register-memo.tsv").write_text(
        "register_ref\tmemo_ref\tmemo\tpayee\tcheck_number\taccount\tkey\n"
        "142661\t39783\tLong term care insurance\tLTCINS\t1234\t8\t12196\n",
        encoding="utf-8",
    )
    (tmp_path / "qdb-canonical-check-numbers.tsv").write_text(
        "account\tkey\tqdb_internal_id\tcheck_number\n8\t1\t1\t1234\n",
        encoding="utf-8",
    )

    catalog = bytearray(850)
    struct.pack_into("<I", catalog, 0, 8)
    catalog[4] = 3
    catalog[5:14] = b"Checking\x00"
    (tmp_path / "qdb-type-080.bin").write_bytes(struct.pack("<II", 850, 1) + catalog)
    index = bytearray(211)
    struct.pack_into("<II", index, 0, 1, 8)
    (tmp_path / "qdb-type-134.bin").write_bytes(struct.pack("<II", 211, 1) + index)
    (tmp_path / "qdb-account-map-134.bin").write_bytes(
        struct.pack("<4sIII", b"QATM", 1, 211, 1) + struct.pack("<II", 8, 1) + index
    )

    transaction = parse_qdb_financial_extract(tmp_path)[0]

    assert transaction.memo == "Long term care insurance"
    assert transaction.number == "1234"
    assert transaction.fit_id == "202608050000001"


def test_parse_qdb_financial_extract_uses_native_register_splits(tmp_path):
    record = bytearray(961)
    struct.pack_into("<I", record, 0, 1)
    struct.pack_into("<I", record, 0x74, (126 << 24) | (7 << 16) | (27 << 8))
    struct.pack_into("<q", record, 0x61, 41774)
    record[0x15 : 0x15 + 14] = b"Example Mutual"
    record[0x81 : 0x81 + 15] = b"202607270000001"
    header = struct.pack("<II", 961, 1)
    (tmp_path / "qdb-type-13c.bin").write_bytes(header + record)
    legacy = bytearray(record)
    struct.pack_into("<I", legacy, 0, 142662)
    (tmp_path / "qdb-type-086.bin").write_bytes(header + legacy)
    (tmp_path / "qdb-register-splits.tsv").write_text(
        "register_ref\tsplit_index\tcategory_handle\ttransfer_handle\tmemo_ref\tamount_cents\traw_hex\taccount\tkey\n"
        "142662\t0\t4198\t42\t0\t12345\t00\t14\t2626\n"
        "142662\t1\t5077\t42\t0\t-678\t00\t14\t2626\n"
        "142662\t2\t0\t42\t0\t0\t00\t14\t2626\n",
        encoding="utf-8",
    )

    catalog = bytearray(850 * 4)
    struct.pack_into("<I", catalog, 0, 14)
    catalog[4] = 3
    catalog[5 : 5 + len(b"Pat\x00")] = b"Pat\x00"
    struct.pack_into("<I", catalog, 850, 4198)
    catalog[850 + 5 : 850 + 5 + len(b"Pension\x00")] = b"Pension\x00"
    struct.pack_into("<I", catalog, 1700, 5077)
    catalog[1700 + 5 : 1700 + 5 + len(b"Tax Spouse:State\x00")] = b"Tax Spouse:State\x00"
    struct.pack_into("<I", catalog, 2550, 42)
    catalog[2550 + 4] = 3
    catalog[2550 + 5 : 2550 + 5 + len(b"Checking (Bank)\x00")] = b"Checking (Bank)\x00"
    (tmp_path / "qdb-type-080.bin").write_bytes(struct.pack("<II", 850, 4) + catalog)
    index = bytearray(211)
    struct.pack_into("<II", index, 0, 1, 14)
    (tmp_path / "qdb-type-134.bin").write_bytes(struct.pack("<II", 211, 1) + index)
    (tmp_path / "qdb-account-map-134.bin").write_bytes(
        struct.pack("<4sIII", b"QATM", 1, 211, 1) + struct.pack("<II", 14, 1) + index
    )

    transaction = parse_qdb_financial_extract(tmp_path)[0]

    assert transaction.category == "--Split--"
    assert [(split.category, split.amount) for split in transaction.splits] == [
        ("Pension", Decimal("123.45")),
        ("Tax Spouse:State", Decimal("-6.78")),
        ("[Checking (Bank)]", Decimal("0")),
    ]


def test_parse_qdb_financial_extract_uses_fitid_register_category_and_transfer(tmp_path):
    records = []
    for internal_id, amount, fit_id, payee in (
        (1, -1250, "FITCAT", "Grocery Store"),
        (2, -5000, "FITXFER", "Transfer"),
    ):
        record = bytearray(961)
        struct.pack_into("<I", record, 0, internal_id)
        struct.pack_into("<I", record, 0x74, (126 << 24) | (8 << 16) | (17 << 8))
        struct.pack_into("<q", record, 0x61, amount)
        record[0x15 : 0x15 + len(payee)] = payee.encode()
        record[0x81 : 0x81 + len(fit_id)] = fit_id.encode()
        records.append(record)
    (tmp_path / "qdb-type-13c.bin").write_bytes(
        struct.pack("<II", 961, len(records)) + b"".join(records)
    )

    legacy_records = []
    for record, register_ref in zip(records, (9001, 9002), strict=True):
        legacy = bytearray(record)
        struct.pack_into("<I", legacy, 0, register_ref)
        legacy_records.append(legacy)
    (tmp_path / "qdb-type-086.bin").write_bytes(
        struct.pack("<II", 961, len(legacy_records)) + b"".join(legacy_records)
    )

    register_records = []
    for register_ref, amount, payee_id, category_handle in (
        (9001, -1250, 1001, 4241),
        (9002, -5000, 1002, 43),
    ):
        register = bytearray(211)
        struct.pack_into("<I", register, 0, register_ref)
        struct.pack_into("<H", register, 4, 42)
        register[6:9] = bytes((17, 8, 126))
        struct.pack_into("<q", register, 0x24, amount)
        struct.pack_into("<I", register, 0x4E, category_handle)
        struct.pack_into("<I", register, 0x63, payee_id)
        register_records.append(register)
    (tmp_path / "qdb-type-0f7.bin").write_bytes(
        struct.pack("<II", 211, len(register_records)) + b"".join(register_records)
    )

    catalog = bytearray(850 * 3)
    for offset, handle, account_type, name in (
        (0, 42, 3, "Checking"),
        (850, 43, 3, "Savings"),
        (1700, 4241, 1, "Groceries"),
    ):
        struct.pack_into("<I", catalog, offset, handle)
        catalog[offset + 4] = account_type
        encoded = name.encode() + bytes([0])
        catalog[offset + 5 : offset + 5 + len(encoded)] = encoded
    (tmp_path / "qdb-type-080.bin").write_bytes(struct.pack("<II", 850, 3) + catalog)

    indexes = []
    for internal_id in (1, 2):
        index = bytearray(211)
        struct.pack_into("<I", index, 0, internal_id)
        indexes.append(index)
    (tmp_path / "qdb-type-134.bin").write_bytes(
        struct.pack("<II", 211, len(indexes)) + b"".join(indexes)
    )
    (tmp_path / "qdb-account-map-134.bin").write_bytes(
        struct.pack("<4sIII", b"QATM", 1, 211, len(indexes))
        + b"".join(
            struct.pack("<II", 42, internal_id) + index
            for internal_id, index in zip((1, 2), indexes, strict=True)
        )
    )
    (tmp_path / "qdb-string-map.tsv").write_text(
        "1001\tGrocery Store\n1002\tTransfer\n", encoding="utf-8"
    )

    transactions = parse_qdb_financial_extract(tmp_path)

    assert [
        (transaction.category, transaction.raw["qdb_account_handle"])
        for transaction in transactions
    ] == [
        ("Groceries", 42),
        ("[Savings]", 42),
    ]

    destination = tmp_path / "financial.sqlite"
    write_transactions(destination, transactions, source_format="test-qdb")
    with sqlite3.connect(destination) as connection:
        assert connection.execute(
            "SELECT category, transfer_account FROM transactions ORDER BY id"
        ).fetchall() == [("Groceries", None), ("[Savings]", "Savings")]


def test_write_transactions_materializes_tags_table(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(
        destination,
        [QifTransaction(account="Checking", payee="Airline", tag="Vacation")],
    )

    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT tag FROM transactions").fetchone() == ("Vacation",)
        assert connection.execute("SELECT name FROM tags").fetchall() == [("Vacation",)]
