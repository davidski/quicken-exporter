---
name: quicken-sqlite-query
description: Query user-selected SQLite databases containing Quicken extracts for transactions, accounts, budgets, reports, and related ledger data. Use for read-only inspection or financial analysis of an existing extract; do not use for editing Quicken files or reverse-engineering protected QDF files.
---

# Quicken SQLite Query

## Rules

1. **Read-only.** Never modify the database or source evidence. Do not run
   `INSERT`, `UPDATE`, `DELETE`, `REPLACE`, `MERGE`, DDL, `VACUUM`, `ANALYZE`,
   `REINDEX`, or a write-capable pragma. Open SQLite with
   `file:/absolute/path/export.sqlite?mode=ro&immutable=1` or `sqlite3 -readonly`.
   Refuse database changes and offer a read-only alternative.
2. **Resolve the database first.** Prefer a user-provided path, then
   `QUICKEN_DB_PATH`. If neither exists, ask for a path or search only an
   explicitly agreed scope; never search broad personal storage. Confirm the
   selected file is the intended SQLite extract before querying it.
3. **Validate the extract.** On one read-only connection, inspect
   `PRAGMA database_list`, `PRAGMA quick_check` (or `integrity_check`),
   `sqlite_master`, relevant schemas, and `metadata`. Record the SQLite/schema
   version, source format/name, valid `extract_date`, row counts, optional
   tables, budget/report counts, FI/register links, and balance provenance.
   `extract_date` is materialization date, not data-coverage end date. Distinguish
   QIF materialization from native QDF extraction and report unavailable fields.
4. **Use the right ledger.** Use `transactions` as the register-first surface
   for ordinary analysis and `transactions.payee` for displayed payees. Use
   `fi_transactions` only for download-specific data or explicit reconciliation
   through `register_transaction_id`; use `investment_transactions` for native
   shares, prices, investment/cash amounts, types, and transfer metadata. Never
   union or deduplicate these families by date and amount.
5. **Keep identifiers distinct.** Join ordinary foreign keys through
   `accounts.id`. Use native `accounts.qdb_handle` for QDB references, report
   filters, and budget/category references; it is not stable across exports.
6. **Respect account and transaction semantics.** Exclude closed and separate
   accounts with `a.is_closed = 0 AND a.is_separate = 0` by default. Remove only
   the requested predicate when the user opts into one class. Interpret `is_*`
   and `is_backfill_cash` as booleans, preserve `NULL` as unknown, and decode
   budget `flags` as a bitmask. For splits, use split rows and exclude a
   `--Split--` parent from aggregates; otherwise use the transaction row.
   Resolve bracketed transfer categories through `transfer_account` before
   calling a category missing.
7. **Use effective balances.** For an as-of date, bind `:account_name` and
   ISO `:as_of_date`, validate the date, and apply both interval boundaries.
   Allow a null `balance_date` only for a valid initial banking interval; never
   substitute a future balance. Use integer banking cents for arithmetic and
   divide by `100.0` only for display. Use investment account totals and
   position periods for their respective results, and report unanchored cash
   provenance when present.
8. **Disambiguate budgets and limit report claims.** Enumerate budget coverage
   before reading amounts; if multiple budgets overlap the requested range, ask
   the user to choose. Preserve category hierarchy and do not blindly sum
   parent and child rows. Saved reports may lack date, grouping, subtotal, or
   filter semantics; reproduce them only when the requested semantics are
   confirmed.
9. **Preserve precision and unknowns.** Prefer integer cents or exact decimal
   text for totals; numeric generated columns are query conveniences. Do not
   place undated rows into as-of calculations or turn missing dates, prices,
   categories, or cleared values into false/zero.
10. **Reuse the connection.** Keep one persistent read-only SQLite connection
    for the interactive task. Reopen only after a connection error, database
    replacement, or explicit refresh request, then revalidate the schema.

## Supported schema

The extract normally contains these table families; inspect the actual schema
before using them and report deliberate differences:

- Core: `accounts`, `transactions`, `transaction_splits`, `categories`,
  `metadata`
- FI and investment: `fi_transactions`, `investment_transactions`,
  `securities`, `security_prices`
- Balances: `banking_account_balance_periods`,
  `investment_account_balance_periods`,
  `investment_position_balance_periods`
- Plans and reports: `budgets`, `reports`, `report_components`,
  `report_filter_groups`, `report_filter_values`, `report_readable`
- Optional convenience views: `banking_account_balance_intervals`,
  `investment_account_balance_intervals`,
  `investment_position_balance_intervals`, `budget_analytics`

Read [references/schema-and-semantics.md](references/schema-and-semantics.md)
for the column map and focused query patterns.

## Effective-dated balances

Use bound `:account_name` and ISO `:as_of_date` parameters, validate the date,
and apply both effective-date boundaries. Never substitute a future balance;
if no effective row exists, report that the extract has no balance for that
account/date. See the reference for the SQL pattern and position lookup.

## Query workflow

1. Resolve, validate, and if needed snapshot the exact database path.
2. Establish one read-only connection; inspect schema, provenance, and relevant
   representative rows without exposing unrelated financial data.
3. Choose the narrowest ledger/table query, applying account, split, transfer,
   date, and source-family rules.
4. Validate the result with an independent grouping or reconciliation; state
   date coverage, exclusions, assumptions, and optional-data limits.
5. Return only the needed rows or aggregates, enough evidence to reproduce any
   non-obvious classification, and clear distinctions between facts and
   Quicken-semantic assumptions.

## Network snapshot

When the selected SQLite file is network-mounted, resolve the bundled helper
and run it once with the exact path to the found database:

```sh
python3 /path/to/quicken-sqlite-query/scripts/cache_sqlite.py \
  "/absolute/path/to/found/quicken.sqlite"
```

If the database is local, skip the helper and use the found path. Otherwise,
use the path printed on standard output as the only SQLite path for the task.
The helper reuses a cache only when the source and SQLite sidecar signatures
match, uses SQLite's read-only backup API when `-wal`, `-shm`, or `-journal`
exists, and rejects a source that changes during its bounded snapshot retries.
Open the selected file read-only and keep the same connection for the task.
