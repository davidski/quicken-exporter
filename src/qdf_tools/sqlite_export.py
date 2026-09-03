"""Transaction-focused SQLite export with stable, queryable tables."""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

from .qif import QifCategory, QifTransaction, parse_qif_data

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    account_type TEXT,
    qdb_handle INTEGER,
    is_closed INTEGER NOT NULL DEFAULT 0,
    is_separate INTEGER NOT NULL DEFAULT 0,
    is_hidden INTEGER NOT NULL DEFAULT 0,
    is_hidden_in_bar INTEGER NOT NULL DEFAULT 0,
    is_hidden_in_list INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    qdb_handle INTEGER
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    qdb_handle INTEGER
);
CREATE TABLE IF NOT EXISTS budgets (
    budget_qid INTEGER NOT NULL,
    budget_name TEXT NOT NULL,
    budget_date TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
    item_count INTEGER NOT NULL,
    item_index INTEGER NOT NULL,
    category_qid INTEGER NOT NULL,
    flags INTEGER NOT NULL,
    budget_amount TEXT NOT NULL,
    secondary_amount TEXT NOT NULL,
    budget_amount_numeric REAL GENERATED ALWAYS AS (CAST(budget_amount AS REAL)) VIRTUAL,
    secondary_amount_numeric REAL GENERATED ALWAYS AS (CAST(secondary_amount AS REAL)) VIRTUAL,
    PRIMARY KEY (budget_qid, year, item_index, month)
);
CREATE TABLE IF NOT EXISTS securities (
    id INTEGER PRIMARY KEY,
    qdb_security_ref INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL,
    symbol TEXT
);
CREATE TABLE IF NOT EXISTS security_prices (
    id INTEGER PRIMARY KEY,
    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
    price_date TEXT NOT NULL,
    price TEXT NOT NULL,
    high TEXT,
    low TEXT,
    volume INTEGER,
    price_numeric REAL GENERATED ALWAYS AS (CAST(price AS REAL)) VIRTUAL,
    high_numeric REAL GENERATED ALWAYS AS (CAST(high AS REAL)) VIRTUAL,
    low_numeric REAL GENERATED ALWAYS AS (CAST(low AS REAL)) VIRTUAL,
    UNIQUE(security_id, price_date)
);
CREATE TABLE IF NOT EXISTS investment_transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    register_key INTEGER NOT NULL,
    transaction_date TEXT NOT NULL,
    transaction_type TEXT NOT NULL,
    security_id INTEGER REFERENCES securities(id) ON DELETE SET NULL,
    shares TEXT NOT NULL,
    price TEXT NOT NULL,
    price_numeric REAL GENERATED ALWAYS AS (CAST(price AS REAL)) VIRTUAL,
    investment_amount TEXT NOT NULL,
    investment_amount_numeric REAL GENERATED ALWAYS AS (CAST(investment_amount AS REAL)) VIRTUAL,
    transaction_amount TEXT NOT NULL,
    transaction_amount_numeric REAL GENERATED ALWAYS AS (CAST(transaction_amount AS REAL)) VIRTUAL,
    backfill_pair_ref INTEGER,
    is_backfill_cash INTEGER NOT NULL DEFAULT 0,
    transfer_qdb_register_ref INTEGER,
    transfer_account INTEGER,
    native_cash_balance TEXT NOT NULL,
    native_cash_balance_numeric REAL GENERATED ALWAYS AS (CAST(native_cash_balance AS REAL)) VIRTUAL,
    UNIQUE(account_id, register_key)
);
CREATE TABLE IF NOT EXISTS investment_account_balance_periods (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    balance_date TEXT NOT NULL,
    next_balance_date TEXT,
    cash_balance TEXT NOT NULL,
    cash_balance_status TEXT NOT NULL DEFAULT 'unanchored_opening_balance',
    cash_balance_source TEXT NOT NULL DEFAULT 'native_transaction_types',
    investment_value TEXT NOT NULL,
    total_value TEXT NOT NULL,
    cash_balance_numeric REAL GENERATED ALWAYS AS (CAST(cash_balance AS REAL)) VIRTUAL,
    investment_value_numeric REAL GENERATED ALWAYS AS (CAST(investment_value AS REAL)) VIRTUAL,
    total_value_numeric REAL GENERATED ALWAYS AS (CAST(total_value AS REAL)) VIRTUAL,
    UNIQUE(account_id, balance_date)
);
CREATE TABLE IF NOT EXISTS investment_position_balance_periods (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    security_id INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
    balance_date TEXT NOT NULL,
    next_balance_date TEXT,
    shares TEXT NOT NULL,
    price TEXT,
    market_value TEXT NOT NULL,
    price_numeric REAL GENERATED ALWAYS AS (CAST(price AS REAL)) VIRTUAL,
    market_value_numeric REAL GENERATED ALWAYS AS (CAST(market_value AS REAL)) VIRTUAL,
    UNIQUE(account_id, security_id, balance_date)
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
    transaction_date TEXT,
    amount TEXT,
    payee TEXT,
    downloaded_payee TEXT,
    payee_source TEXT,
    memo TEXT,
    number TEXT,
    fit_id TEXT,
    security TEXT,
    price TEXT,
    quantity TEXT,
    commission TEXT,
    amount_numeric REAL GENERATED ALWAYS AS (CAST(amount AS REAL)) VIRTUAL,
    price_numeric REAL GENERATED ALWAYS AS (CAST(price AS REAL)) VIRTUAL,
    commission_numeric REAL GENERATED ALWAYS AS (CAST(commission AS REAL)) VIRTUAL,
    category TEXT,
    tag TEXT,
    transfer_account TEXT,
    cleared TEXT,
    qdb_record_type TEXT NOT NULL,
    qdb_internal_id INTEGER,
    qdb_register_ref INTEGER
);
CREATE TABLE IF NOT EXISTS fi_transactions (
    id INTEGER PRIMARY KEY,
    register_transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    transaction_date TEXT,
    amount TEXT,
    payee TEXT,
    fit_id TEXT,
    cleared TEXT,
    qdb_record_type TEXT NOT NULL,
    qdb_internal_id INTEGER NOT NULL UNIQUE,
    qdb_register_ref INTEGER,
    link_method TEXT
);
CREATE TABLE IF NOT EXISTS transaction_splits (
    id INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    line_number INTEGER NOT NULL,
    category TEXT,
    transfer_account TEXT,
    memo TEXT,
    amount TEXT,
    amount_numeric REAL GENERATED ALWAYS AS (CAST(amount AS REAL)) VIRTUAL,
    UNIQUE(transaction_id, line_number)
);
CREATE TABLE IF NOT EXISTS banking_account_balance_periods (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    balance_date TEXT,
    next_balance_date TEXT,
    balance_cents INTEGER NOT NULL,
    UNIQUE(account_id, balance_date)
);
CREATE INDEX IF NOT EXISTS ix_transactions_date ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS ix_transactions_account_date ON transactions(account_id, transaction_date);
CREATE INDEX IF NOT EXISTS ix_transactions_payee ON transactions(payee);
CREATE INDEX IF NOT EXISTS ix_fi_transactions_fit_id ON fi_transactions(fit_id);
CREATE INDEX IF NOT EXISTS ix_fi_transactions_account_date ON fi_transactions(account_id, transaction_date);
CREATE INDEX IF NOT EXISTS ix_fi_transactions_register ON fi_transactions(register_transaction_id);
CREATE INDEX IF NOT EXISTS ix_splits_transaction ON transaction_splits(transaction_id);
CREATE INDEX IF NOT EXISTS ix_categories_qdb_handle ON categories(qdb_handle);
CREATE INDEX IF NOT EXISTS ix_budgets_budget_year
    ON budgets(budget_qid, year);
CREATE INDEX IF NOT EXISTS ix_budgets_category ON budgets(category_qid);
CREATE INDEX IF NOT EXISTS ix_budgets_year_month
    ON budgets(budget_qid, year, month);
CREATE INDEX IF NOT EXISTS ix_security_prices_date ON security_prices(price_date);
CREATE INDEX IF NOT EXISTS ix_security_prices_security_date
    ON security_prices(security_id, price_date);
CREATE INDEX IF NOT EXISTS ix_investment_transactions_account_date
    ON investment_transactions(account_id, transaction_date);
CREATE INDEX IF NOT EXISTS ix_investment_transactions_security_date
    ON investment_transactions(security_id, transaction_date);
CREATE INDEX IF NOT EXISTS ix_investment_transactions_backfill_pair
    ON investment_transactions(backfill_pair_ref);
CREATE INDEX IF NOT EXISTS ix_investment_account_balance_dates
    ON investment_account_balance_periods(account_id, balance_date, next_balance_date);
CREATE INDEX IF NOT EXISTS ix_investment_position_balance_dates
    ON investment_position_balance_periods(account_id, security_id, balance_date, next_balance_date);
CREATE INDEX IF NOT EXISTS ix_balance_periods_account_dates
    ON banking_account_balance_periods(account_id, balance_date, next_balance_date);
DROP VIEW IF EXISTS banking_account_balance_intervals;
DROP VIEW IF EXISTS investment_account_balance_intervals;
DROP VIEW IF EXISTS investment_position_balance_intervals;
DROP VIEW IF EXISTS budget_amounts_analytics;
DROP VIEW IF EXISTS budget_analytics;
DROP VIEW IF EXISTS banking_transaction_balances;
DROP VIEW IF EXISTS account_transaction_balances;
CREATE VIEW banking_account_balance_intervals AS
SELECT
    balance_periods.account_id,
    accounts.name AS account_name,
    accounts.account_type,
    balance_periods.balance_date,
    balance_periods.next_balance_date,
    balance_periods.balance_cents,
    balance_periods.balance_cents / 100.0 AS balance
FROM banking_account_balance_periods AS balance_periods
JOIN accounts ON accounts.id = balance_periods.account_id;
CREATE VIEW investment_account_balance_intervals AS
SELECT
    balance_periods.account_id,
    accounts.name AS account_name,
    accounts.account_type,
    balance_periods.balance_date,
    balance_periods.next_balance_date,
    balance_periods.cash_balance,
    CAST(balance_periods.cash_balance AS REAL) AS cash_balance_value,
    balance_periods.cash_balance_status,
    balance_periods.cash_balance_source,
    balance_periods.investment_value,
    CAST(balance_periods.investment_value AS REAL) AS investment_value_amount,
    balance_periods.total_value,
    CAST(balance_periods.total_value AS REAL) AS total_value_amount
FROM investment_account_balance_periods AS balance_periods
JOIN accounts ON accounts.id = balance_periods.account_id;
CREATE VIEW investment_position_balance_intervals AS
SELECT
    position_periods.account_id,
    accounts.name AS account_name,
    accounts.account_type,
    position_periods.security_id,
    securities.name AS security_name,
    securities.symbol,
    position_periods.balance_date,
    position_periods.next_balance_date,
    position_periods.shares,
    CAST(position_periods.shares AS REAL) AS share_balance,
    position_periods.price,
    CAST(position_periods.price AS REAL) AS price_value,
    position_periods.market_value,
    CAST(position_periods.market_value AS REAL) AS market_value_amount
FROM investment_position_balance_periods AS position_periods
JOIN accounts ON accounts.id = position_periods.account_id
JOIN securities ON securities.id = position_periods.security_id;
CREATE VIEW budget_analytics AS
SELECT
    budgets.budget_qid,
    budgets.budget_name,
    budgets.year,
    budgets.month,
    budgets.item_index,
    budgets.category_qid,
    categories.name AS category_name,
    budgets.budget_amount,
    budgets.secondary_amount,
    CAST(budgets.budget_amount AS REAL) AS budget_amount_numeric,
    CAST(budgets.secondary_amount AS REAL) AS secondary_amount_numeric,
    CASE WHEN (budgets.flags & 0x0800) <> 0 THEN 1 ELSE 0 END
        AS rollover_enabled,
    CASE WHEN CAST(budgets.secondary_amount AS REAL) <> 0 THEN 1 ELSE 0 END
        AS has_manual_rollover_amount
FROM budgets
LEFT JOIN categories
    ON categories.qdb_handle = budgets.category_qid;
"""

NUMERIC_GENERATED_COLUMNS = {
    "budgets": {
        "budget_amount_numeric": "budget_amount",
        "secondary_amount_numeric": "secondary_amount",
    },
    "security_prices": {
        "price_numeric": "price",
        "high_numeric": "high",
        "low_numeric": "low",
    },
    "investment_transactions": {
        "price_numeric": "price",
        "investment_amount_numeric": "investment_amount",
        "transaction_amount_numeric": "transaction_amount",
        "native_cash_balance_numeric": "native_cash_balance",
    },
    "investment_account_balance_periods": {
        "cash_balance_numeric": "cash_balance",
        "investment_value_numeric": "investment_value",
        "total_value_numeric": "total_value",
    },
    "investment_position_balance_periods": {
        "price_numeric": "price",
        "market_value_numeric": "market_value",
    },
    "transactions": {
        "amount_numeric": "amount",
        "price_numeric": "price",
        "commission_numeric": "commission",
    },
    "transaction_splits": {"amount_numeric": "amount"},
}


def _ensure_numeric_generated_columns(connection: sqlite3.Connection) -> None:
    for table, generated_columns in NUMERIC_GENERATED_COLUMNS.items():
        columns = {row[1] for row in connection.execute(f"PRAGMA table_xinfo({table})")}
        for name, source_column in generated_columns.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} REAL GENERATED ALWAYS AS "
                    f"(CAST({source_column} AS REAL)) VIRTUAL"
                )


BALANCE_PERIODS_SQL = """
WITH banking_accounts AS (
    SELECT id AS account_id
    FROM accounts
    WHERE account_type IN ('Bank', 'Banking', 'Cash')
),
daily_changes AS (
    SELECT
        transactions.account_id,
        transactions.transaction_date AS balance_date,
        SUM(
            COALESCE(
                CAST(ROUND(CAST(transactions.amount AS REAL) * 100.0) AS INTEGER),
                0
            )
        ) AS balance_change_cents
    FROM transactions
    JOIN banking_accounts
        ON banking_accounts.account_id = transactions.account_id
    WHERE transactions.transaction_date IS NOT NULL
    GROUP BY transactions.account_id, transactions.transaction_date
),
daily_balances AS (
    SELECT
        account_id,
        balance_date,
        SUM(balance_change_cents) OVER (
            PARTITION BY account_id
            ORDER BY balance_date
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS balance_cents
    FROM daily_changes
),
dated_intervals AS (
    SELECT
        account_id,
        balance_date,
        LEAD(balance_date) OVER (
            PARTITION BY account_id
            ORDER BY balance_date
        ) AS next_balance_date,
        balance_cents
    FROM daily_balances
)
INSERT INTO banking_account_balance_periods(
    account_id, balance_date, next_balance_date, balance_cents
)
SELECT
    banking_accounts.account_id,
    NULL AS balance_date,
    MIN(daily_balances.balance_date) AS next_balance_date,
    0 AS balance_cents
FROM banking_accounts
LEFT JOIN daily_balances
    ON daily_balances.account_id = banking_accounts.account_id
GROUP BY banking_accounts.account_id
UNION ALL
SELECT account_id, balance_date, next_balance_date, balance_cents
FROM dated_intervals;
"""


def _date(value: dt.date | None) -> str | None:
    return value.isoformat() if value else None


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _account_id(connection: sqlite3.Connection, name: str | None, section: str) -> int | None:
    return _account_id_with_metadata(connection, name, section)


def _account_id_with_metadata(
    connection: sqlite3.Connection,
    name: str | None,
    section: str,
    qdb_handle: int | None = None,
) -> int | None:
    if not name:
        return None
    connection.execute(
        "INSERT INTO accounts(name, account_type, qdb_handle) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO NOTHING",
        (name, section, qdb_handle),
    )
    if qdb_handle is not None:
        connection.execute(
            "UPDATE accounts SET qdb_handle = COALESCE(qdb_handle, ?) WHERE name = ?",
            (qdb_handle, name),
        )
    return connection.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()[0]


def _category(connection: sqlite3.Connection, name: str | None) -> None:
    if not name or name.startswith("["):
        return
    connection.execute(
        "INSERT INTO categories(name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,)
    )


def _tag(connection: sqlite3.Connection, name: str | None) -> None:
    if name:
        connection.execute(
            "INSERT INTO tags(name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,)
        )


def _materialize_categories(connection: sqlite3.Connection) -> None:
    """Rewrite the category rows so the table's physical order is name-ascending."""
    connection.execute(
        "CREATE TEMP TABLE ordered_category_names(name TEXT PRIMARY KEY, qdb_handle INTEGER)"
    )
    connection.execute(
        "INSERT INTO ordered_category_names(name, qdb_handle) "
        "SELECT name, qdb_handle FROM categories ORDER BY name COLLATE BINARY"
    )
    connection.execute("DELETE FROM categories")
    connection.execute(
        "INSERT INTO categories(name, qdb_handle) "
        "SELECT name, qdb_handle FROM ordered_category_names ORDER BY name COLLATE BINARY"
    )
    connection.execute("DROP TABLE ordered_category_names")


def _materialize_tags(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TEMP TABLE ordered_tag_names(name TEXT PRIMARY KEY, qdb_handle INTEGER)"
    )
    connection.execute(
        "INSERT INTO ordered_tag_names SELECT name, qdb_handle FROM tags ORDER BY name COLLATE BINARY"
    )
    connection.execute("DELETE FROM tags")
    connection.execute(
        "INSERT INTO tags(name, qdb_handle) SELECT name, qdb_handle FROM ordered_tag_names ORDER BY name COLLATE BINARY"
    )
    connection.execute("DROP TABLE ordered_tag_names")


def _transfer_account(name: str | None) -> str | None:
    if name and len(name) > 2 and name.startswith("[") and name.endswith("]"):
        return name[1:-1]
    return None


def write_transactions(
    destination: str | Path,
    transactions: Iterable[QifTransaction],
    *,
    source_format: str = "qif",
    source_name: str | None = None,
    accounts: Iterable[tuple[str, str | None]] = (),
    categories: Iterable[QifCategory | str] = (),
    tags: Iterable[tuple[str, int | None] | str] = (),
) -> int:
    """Create/replace a SQLite export and return the transaction count."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(destination) as connection:
        existing_price_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(security_prices)")
        }
        if "qdb_date_word" in existing_price_columns:
            connection.execute("DROP TABLE security_prices")
        existing_investment_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(investment_transactions)")
        }
        if "register_ref" in existing_investment_columns:
            connection.execute("ALTER TABLE investment_transactions DROP COLUMN register_ref")
            existing_investment_columns.remove("register_ref")
        if "investment_type" in existing_investment_columns:
            connection.execute("ALTER TABLE investment_transactions DROP COLUMN investment_type")
        if "investment_type_name" in existing_investment_columns:
            connection.execute(
                "ALTER TABLE investment_transactions "
                "RENAME COLUMN investment_type_name TO transaction_type"
            )
        if "transfer_qid" in existing_investment_columns:
            connection.execute(
                "ALTER TABLE investment_transactions "
                "RENAME COLUMN transfer_qid TO transfer_qdb_register_ref"
            )
        if existing_investment_columns:
            if "native_cash_balance" not in existing_investment_columns:
                raise ValueError(
                    "existing investment_transactions table lacks required "
                    "native_cash_balance; create a fresh native export"
                )
            native_cash_nulls = connection.execute(
                "SELECT COUNT(*) FROM investment_transactions WHERE native_cash_balance IS NULL"
            ).fetchone()[0]
            if native_cash_nulls:
                raise ValueError(
                    "existing investment_transactions contains rows without "
                    "native_cash_balance; create a fresh native export"
                )
            native_cash_notnull = next(
                row[3]
                for row in connection.execute("PRAGMA table_info(investment_transactions)")
                if row[1] == "native_cash_balance"
            )
            if not native_cash_notnull:
                connection.execute(
                    "ALTER TABLE investment_transactions RENAME TO investment_transactions_previous"
                )
                connection.executescript(SCHEMA)
                connection.execute(
                    """INSERT INTO investment_transactions(
                        id, account_id, transaction_id, register_key,
                        transaction_date, transaction_type, security_id, shares,
                        price, investment_amount, transaction_amount,
                        backfill_pair_ref, is_backfill_cash, transfer_qdb_register_ref,
                        transfer_account, native_cash_balance
                    )
                    SELECT id, account_id, transaction_id, register_key,
                           transaction_date, transaction_type, security_id, shares,
                           price, investment_amount, transaction_amount,
                           backfill_pair_ref, is_backfill_cash, transfer_qdb_register_ref,
                           transfer_account, native_cash_balance
                    FROM investment_transactions_previous"""
                )
                connection.execute("DROP TABLE investment_transactions_previous")
        existing_transaction_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(transactions)")
        }
        if "address" in existing_transaction_columns:
            for view_name in (
                "banking_transaction_balances",
                "banking_account_balance_intervals",
                "investment_account_balance_intervals",
                "investment_position_balance_intervals",
                "account_transaction_balances",
            ):
                connection.execute(f"DROP VIEW IF EXISTS {view_name}")
            connection.execute("ALTER TABLE transactions DROP COLUMN address")
        category_columns = {row[1] for row in connection.execute("PRAGMA table_info(categories)")}
        if category_columns and "qdb_handle" not in category_columns:
            connection.execute("ALTER TABLE categories ADD COLUMN qdb_handle INTEGER")
        budget_amounts_exists = bool(
            connection.execute("PRAGMA table_info(budget_amounts)").fetchall()
        )
        budget_years_exists = bool(connection.execute("PRAGMA table_info(budget_years)").fetchall())
        budgets_exists = bool(connection.execute("PRAGMA table_info(budgets)").fetchall())
        if budget_amounts_exists or budget_years_exists or budgets_exists:
            connection.execute("DROP VIEW IF EXISTS budget_amounts_analytics")
            connection.execute("DROP VIEW IF EXISTS budget_analytics")
            if budget_amounts_exists:
                connection.execute("DROP TABLE budget_amounts")
            if budget_years_exists:
                connection.execute("DROP TABLE budget_years")
            if budgets_exists:
                connection.execute("DROP TABLE budgets")
        connection.executescript(SCHEMA)
        _ensure_numeric_generated_columns(connection)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(accounts)")}
        if "qdb_handle" not in columns:
            connection.execute("ALTER TABLE accounts ADD COLUMN qdb_handle INTEGER")
        for column in (
            "is_closed",
            "is_separate",
            "is_hidden",
            "is_hidden_in_bar",
            "is_hidden_in_list",
        ):
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE accounts ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )
        if "raw_json" in columns:
            connection.execute("ALTER TABLE accounts DROP COLUMN raw_json")
        transaction_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(transactions)")
        }
        if "section" in transaction_columns and "qdb_record_type" not in transaction_columns:
            connection.execute("ALTER TABLE transactions RENAME COLUMN section TO qdb_record_type")
            transaction_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(transactions)")
            }
        if "qdb_record_type" not in transaction_columns:
            connection.execute(
                "ALTER TABLE transactions ADD COLUMN qdb_record_type TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "qdb_register_ref" not in transaction_columns:
            connection.execute("ALTER TABLE transactions ADD COLUMN qdb_register_ref INTEGER")
        if "qdb_internal_id" not in transaction_columns:
            connection.execute("ALTER TABLE transactions ADD COLUMN qdb_internal_id INTEGER")
        if "fit_id" not in transaction_columns:
            connection.execute("ALTER TABLE transactions ADD COLUMN fit_id TEXT")
        if "raw_json" in transaction_columns:
            connection.execute("ALTER TABLE transactions DROP COLUMN raw_json")
        split_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(transaction_splits)")
        }
        if "raw_json" in split_columns:
            connection.execute("ALTER TABLE transaction_splits DROP COLUMN raw_json")
        if "downloaded_payee" not in transaction_columns:
            connection.execute("ALTER TABLE transactions ADD COLUMN downloaded_payee TEXT")
        if "payee_source" not in transaction_columns:
            connection.execute("ALTER TABLE transactions ADD COLUMN payee_source TEXT")
        if "tag" not in transaction_columns:
            connection.execute("ALTER TABLE transactions ADD COLUMN tag TEXT")
        investment_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(investment_transactions)")
        }
        if "transaction_amount" not in investment_columns:
            connection.execute(
                "ALTER TABLE investment_transactions ADD COLUMN transaction_amount TEXT"
            )
        if "backfill_pair_ref" not in investment_columns:
            connection.execute(
                "ALTER TABLE investment_transactions ADD COLUMN backfill_pair_ref INTEGER"
            )
        if "is_backfill_cash" not in investment_columns:
            connection.execute(
                "ALTER TABLE investment_transactions ADD COLUMN "
                "is_backfill_cash INTEGER NOT NULL DEFAULT 0"
            )
        if "transfer_qdb_register_ref" not in investment_columns:
            connection.execute(
                "ALTER TABLE investment_transactions ADD COLUMN transfer_qdb_register_ref INTEGER"
            )
        if "transfer_account" not in investment_columns:
            connection.execute(
                "ALTER TABLE investment_transactions ADD COLUMN transfer_account INTEGER"
            )
        if "native_cash_balance" not in investment_columns:
            connection.execute(
                "ALTER TABLE investment_transactions ADD COLUMN native_cash_balance TEXT"
            )
        if "is_cash" in investment_columns:
            connection.execute("ALTER TABLE investment_transactions DROP COLUMN is_cash")
        investment_balance_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(investment_account_balance_periods)")
        }
        if "cash_balance_status" not in investment_balance_columns:
            connection.execute(
                "ALTER TABLE investment_account_balance_periods ADD COLUMN "
                "cash_balance_status TEXT NOT NULL DEFAULT 'unanchored_opening_balance'"
            )
        if "cash_balance_source" not in investment_balance_columns:
            connection.execute(
                "ALTER TABLE investment_account_balance_periods ADD COLUMN "
                "cash_balance_source TEXT NOT NULL DEFAULT 'native_transaction_types'"
            )
        connection.execute("DELETE FROM banking_account_balance_periods")
        connection.execute("DELETE FROM investment_position_balance_periods")
        connection.execute("DELETE FROM investment_account_balance_periods")
        connection.execute("DELETE FROM fi_transactions")
        connection.execute("DELETE FROM investment_transactions")
        connection.execute("DELETE FROM budgets")
        connection.execute("DELETE FROM security_prices")
        connection.execute("DELETE FROM securities")
        connection.execute("DELETE FROM transaction_splits")
        connection.execute("DELETE FROM transactions")
        connection.execute("DELETE FROM categories")
        connection.execute("DELETE FROM tags")
        connection.execute("DELETE FROM accounts")
        connection.execute("DELETE FROM metadata")
        extract_date = dt.date.today().isoformat()
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", "32"),
                ("source_format", source_format),
                ("extract_date", extract_date),
            ]
            + ([("source_name", source_name)] if source_name else []),
        )
        for account_name, account_type in accounts:
            _account_id_with_metadata(connection, account_name, account_type or "Account")
        for category in categories:
            category_name = category.name if isinstance(category, QifCategory) else category
            _category(connection, category_name)
        for tag in tags:
            if isinstance(tag, tuple):
                tag_name, tag_handle = tag
                _tag(connection, tag_name)
                connection.execute(
                    "UPDATE tags SET qdb_handle = COALESCE(qdb_handle, ?) WHERE name = ?",
                    (tag_handle, tag_name),
                )
            else:
                _tag(connection, tag)
        # SQLite tables are scanned in rowid order when a query has no ORDER BY.
        # Insert rows newest-first so the physical table has the requested
        # order without turning ``transactions`` into a view.
        ordered_transactions = sorted(
            transactions,
            key=lambda transaction: (
                transaction.date is not None,
                transaction.date or dt.date.min,
            ),
            reverse=True,
        )
        count = 0
        for transaction in ordered_transactions:
            qdb_handle = transaction.raw.get("qdb_account_handle")
            if not isinstance(qdb_handle, int):
                qdb_handle = None
            account_id = _account_id_with_metadata(
                connection,
                transaction.account,
                transaction.account_type or transaction.section,
                qdb_handle,
            )
            _category(connection, transaction.category)
            _tag(connection, transaction.tag)
            if _transfer_account(transaction.category):
                _account_id_with_metadata(
                    connection, _transfer_account(transaction.category), "Transfer"
                )
            for split in transaction.splits:
                _category(connection, split.category)
                if _transfer_account(split.category):
                    _account_id_with_metadata(
                        connection, _transfer_account(split.category), "Transfer"
                    )
            cursor = connection.execute(
                """INSERT INTO transactions(
                    account_id, transaction_date, amount, payee, downloaded_payee,
                    payee_source, memo, number, fit_id,
                    category, tag, transfer_account, cleared, qdb_record_type,
                    security, price, quantity, commission, qdb_internal_id, qdb_register_ref
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    account_id,
                    _date(transaction.date),
                    _decimal(transaction.amount),
                    transaction.payee,
                    transaction.downloaded_payee,
                    transaction.payee_source,
                    transaction.memo,
                    transaction.number,
                    transaction.fit_id,
                    transaction.category,
                    transaction.tag,
                    _transfer_account(transaction.category),
                    transaction.cleared,
                    transaction.section,
                    transaction.security,
                    _decimal(transaction.price),
                    _decimal(transaction.quantity),
                    _decimal(transaction.commission),
                    (
                        transaction.raw.get("qdb_internal_id")
                        if isinstance(transaction.raw.get("qdb_internal_id"), int)
                        else None
                    ),
                    (
                        transaction.raw.get("qdb_register_ref")
                        if isinstance(transaction.raw.get("qdb_register_ref"), int)
                        else None
                    ),
                ),
            )
            transaction_id = cursor.lastrowid
            for line_number, split in enumerate(transaction.splits, 1):
                connection.execute(
                    """INSERT INTO transaction_splits(
                        transaction_id, line_number, category, transfer_account, memo, amount
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        transaction_id,
                        line_number,
                        split.category,
                        _transfer_account(split.category),
                        split.memo,
                        _decimal(split.amount),
                    ),
                )
            count += 1
        _materialize_categories(connection)
        _materialize_tags(connection)
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?)", ("transaction_count", str(count))
        )
        connection.execute(BALANCE_PERIODS_SQL)
    return count


def export_qif_to_sqlite(
    source: str | Path,
    destination: str | Path,
    *,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
) -> int:
    """Filter QIF transactions and write them with catalogs to SQLite."""
    data = parse_qif_data(source)
    transactions = [
        transaction
        for transaction in data.transactions
        if transaction.date is not None
        and (start_date is None or transaction.date >= start_date)
        and (end_date is None or transaction.date <= end_date)
    ]
    return write_transactions(
        destination,
        transactions,
        source_name=Path(source).name,
        accounts=((account.name, account.account_type) for account in data.accounts),
        categories=data.categories,
    )
