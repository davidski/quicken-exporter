import datetime as dt
import sqlite3
from decimal import Decimal

from qdf_tools.qif import (
    QifSplit,
    QifTransaction,
    parse_amount,
    parse_date,
    parse_qif,
    parse_qif_data,
)
from qdf_tools.sqlite_export import export_qif_to_sqlite, write_transactions


def test_qif_scalar_parsers():
    assert parse_amount("(1,234.50)") == -1234.50
    assert parse_date("12/31/2024").isoformat() == "2024-12-31"
    assert parse_date("1/ 1'26").isoformat() == "2026-01-01"


def test_qif_metadata_sections_are_catalogs_not_transactions(tmp_path):
    source = tmp_path / "metadata.qif"
    source.write_text(
        """!Type:Tag
NTravel
^
!Type:Cat
NFood:Groceries
DFood
^
!Account
NChecking
TBank
^
!Type:Bank
D1/ 1'26
T-5.00
LFood:Groceries
^
""",
        encoding="utf-8",
    )

    data = parse_qif_data(source)

    assert [account.name for account in data.accounts] == ["Checking"]
    assert [category.name for category in data.categories] == ["Food:Groceries"]
    assert len(data.transactions) == 1
    assert data.transactions[0].account_type == "Banking"


def test_qif_to_sqlite(tmp_path):
    source = tmp_path / "sample.qif"
    source.write_text(
        """!Account
NChecking
TBank
^
!Type:Bank
D12/31/2024
T-42.50
PMarket
A123 Main Street
MWeekly shop
LFood:Groceries
SFood:Groceries
EProduce
$-30.00
SFood:Household
EHousehold
$-12.50
^
""",
        encoding="utf-8",
    )
    destination = tmp_path / "export.sqlite"
    transactions = parse_qif(source)
    assert len(transactions) == 1
    assert transactions[0].account == "Checking"
    assert export_qif_to_sqlite(source, destination) == 1
    with sqlite3.connect(destination) as db:
        assert db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM transaction_splits").fetchone()[0] == 2
        row = db.execute(
            "SELECT transaction_date, amount, amount_numeric, payee, downloaded_payee, "
            "payee_source, memo "
            "FROM transactions"
        ).fetchone()
        assert row == (
            "2024-12-31",
            "-42.50",
            -42.5,
            "Market",
            None,
            None,
            "Weekly shop",
        )
        assert db.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone() == (
            "32",
        )
        extract_date = db.execute(
            "SELECT value FROM metadata WHERE key = 'extract_date'"
        ).fetchone()[0]
        assert extract_date == dt.date.today().isoformat()
        assert dt.date.fromisoformat(extract_date).isoformat() == extract_date
        columns = {row[1] for row in db.execute("PRAGMA table_info(transactions)")}
        assert "address" not in columns
        assert db.execute(
            "SELECT account_name, balance_cents "
            "FROM banking_account_balance_intervals "
            "WHERE balance_date = '2024-12-31'"
        ).fetchone() == ("Checking", -4250)


def test_transactions_are_physically_ordered_by_date_desc(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(
        destination,
        [
            QifTransaction(account="Checking", date=dt.date(2024, 1, 1), payee="Old"),
            QifTransaction(account="Checking", date=dt.date(2024, 1, 3), payee="New"),
            QifTransaction(account="Checking", date=dt.date(2024, 1, 2), payee="Middle"),
        ],
        source_format="test-qif",
    )

    with sqlite3.connect(destination) as db:
        rows = db.execute("SELECT transaction_date, payee FROM transactions").fetchall()

    assert rows == [
        ("2024-01-03", "New"),
        ("2024-01-02", "Middle"),
        ("2024-01-01", "Old"),
    ]


def test_categories_are_physically_ordered_by_name(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(
        destination,
        [
            QifTransaction(
                account="Checking",
                date=dt.date(2024, 1, 1),
                category="middle",
                splits=[QifSplit(category="alpha")],
            )
        ],
        source_format="test-qif",
        categories=["zebra", "Apple"],
    )

    with sqlite3.connect(destination) as db:
        rows = db.execute("SELECT id, name FROM categories NOT INDEXED").fetchall()

    assert rows == [(1, "Apple"), (2, "alpha"), (3, "middle"), (4, "zebra")]


def test_banking_transaction_balances_view_is_removed(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(destination, [], source_format="test-qif")

    with sqlite3.connect(destination) as db:
        view = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'view' "
            "AND name = 'banking_transaction_balances'"
        ).fetchone()

    assert view is None


def test_write_transactions_drops_legacy_address_column(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(destination, [], source_format="test-qif")
    with sqlite3.connect(destination) as db:
        db.execute("ALTER TABLE transactions ADD COLUMN address TEXT")
        db.execute("UPDATE transactions SET address = 'discard me'")

    write_transactions(destination, [], source_format="test-qif")

    with sqlite3.connect(destination) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(transactions)")}
    assert "address" not in columns


def test_write_transactions_removes_legacy_raw_price_date_column(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(destination, [], source_format="test-qif")
    with sqlite3.connect(destination) as db:
        db.execute("ALTER TABLE security_prices ADD COLUMN qdb_date_word INTEGER")

    write_transactions(destination, [], source_format="test-qif")

    with sqlite3.connect(destination) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(security_prices)")}
        version = db.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
    assert "qdb_date_word" not in columns
    assert version == ("32",)


def test_write_transactions_removes_legacy_account_raw_json_column(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(destination, [], source_format="test-qif")
    with sqlite3.connect(destination) as db:
        db.execute("ALTER TABLE accounts ADD COLUMN raw_json TEXT")
        db.execute(
            "INSERT INTO accounts(name, account_type, raw_json) VALUES (?, ?, ?)",
            ("Legacy", "Banking", "{}"),
        )

    write_transactions(destination, [], source_format="test-qif")

    with sqlite3.connect(destination) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
        version = db.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
    assert "raw_json" not in columns
    assert version == ("32",)


def test_account_transaction_balances_view_is_removed(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(destination, [], source_format="test-qif")

    with sqlite3.connect(destination) as db:
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'view' "
                "AND name = 'account_transaction_balances'"
            ).fetchone()
            is None
        )


def test_banking_account_balance_intervals_accept_as_of_date_and_account_id(tmp_path):
    destination = tmp_path / "export.sqlite"
    write_transactions(
        destination,
        [
            QifTransaction(
                account="Checking",
                account_type="Banking",
                date=dt.date(2024, 1, 1),
                amount=Decimal("100.00"),
            ),
            QifTransaction(
                account="Checking",
                account_type="Banking",
                date=dt.date(2024, 1, 1),
                amount=Decimal("-20.10"),
            ),
            QifTransaction(
                account="Checking",
                account_type="Banking",
                date=dt.date(2024, 1, 3),
                amount=Decimal("5.25"),
            ),
            QifTransaction(
                account="Wallet",
                account_type="Cash",
                date=dt.date(2024, 1, 1),
                amount=Decimal("999.00"),
            ),
        ],
        source_format="test-qif",
    )

    query = """
        SELECT balance_date, next_balance_date, balance_cents, balance
        FROM banking_account_balance_intervals
        WHERE account_id = :account_id
          AND date(:as_of_date) IS NOT NULL
          AND (balance_date IS NULL OR balance_date <= date(:as_of_date))
          AND (next_balance_date IS NULL OR date(:as_of_date) < next_balance_date)
    """
    with sqlite3.connect(destination) as db:
        checking_id = db.execute("SELECT id FROM accounts WHERE name = 'Checking'").fetchone()[0]
        assert db.execute(
            query, {"account_id": checking_id, "as_of_date": "2023-12-31"}
        ).fetchone() == (None, "2024-01-01", 0, 0.0)
        assert db.execute(
            query, {"account_id": checking_id, "as_of_date": "2024-01-01"}
        ).fetchone() == ("2024-01-01", "2024-01-03", 7990, 79.9)
        assert db.execute(
            query, {"account_id": checking_id, "as_of_date": "2024-01-02"}
        ).fetchone() == ("2024-01-01", "2024-01-03", 7990, 79.9)
        assert db.execute(
            query, {"account_id": checking_id, "as_of_date": "2024-01-03"}
        ).fetchone() == ("2024-01-03", None, 8515, 85.15)
        assert (
            db.execute(query, {"account_id": checking_id, "as_of_date": "not-a-date"}).fetchone()
            is None
        )
        assert db.execute(query, {"account_id": -1, "as_of_date": "2024-01-03"}).fetchone() is None


def test_qif_transaction_category_and_tag_are_split(tmp_path):
    source = tmp_path / "tag.qif"
    source.write_text(
        "!Type:Bank\nD3/14/2022\nT-30\nPAmerican Airlines\nLTravel:Air Travel/Vacation\n^\n",
        encoding="utf-8",
    )

    transaction = parse_qif(source)[0]
    assert transaction.category == "Travel:Air Travel"
    assert transaction.tag == "Vacation"
