# Quicken query patterns and semantics

The parent `SKILL.md` names the supported table families. Use these focused query patterns after opening the user-selected database read-only, adapting names only if schema inspection shows a deliberate variant.

## Inventory

```sql
PRAGMA quick_check;
SELECT type, name, tbl_name, sql
FROM sqlite_master
WHERE type IN ('table', 'view')
ORDER BY type, name;
```

For each relevant table:

```sql
PRAGMA table_info("table_name");
PRAGMA foreign_key_list("table_name");
PRAGMA index_list("table_name");
SELECT COUNT(*) AS rows FROM "table_name";
```

Use quoted identifiers and parameterized values for user-supplied text. Avoid selecting an entire financial table when a few columns and a `LIMIT` answer the inspection question.

## Transaction analysis

Common extract designs include:

- `transactions` with one parent row per transaction and `transaction_splits` containing category/payee/amount detail;
- the revised native export's register-first `transactions` rows (`qdb_record_type = 'QDB:0xf7'`) with parallel FI/download rows in `fi_transactions` (`qdb_record_type = 'QDB:0x13c'`);
- canonical posting tables such as `transaction_lines`;
- specialized tables for investments, balances, or reports.

Determine which design is present before aggregating. If splits exist, do not sum a `--Split--` parent together with its split rows. Confirm whether amounts are stored as decimal values, integer minor units such as cents, or signed values whose direction must be inferred from account type and transaction type.

For a category or spending total, inspect these relationships when available:

- transaction to split/posting;
- category to category hierarchy;
- `transfer_account` or equivalent to accounts;
- account type and account name;
- cleared status, transaction date, and posting date;
- investment transaction type, security, quantity, and cash movement.

Bracketed categories such as `[Account Name]` may be Quicken transfer notation rather than missing categories. Treat a category as unresolved only after checking both the category table and the linked transfer account.

For ordinary operating-spending analysis, make exclusions explicit rather than silently applying them. Typical exclusions include internal transfers, credit-card payments, reimbursements, rewards, investment contributions/withdrawals, and future-dated activity. These are not universal rules for every cash-flow question; retain them when the user asks for household spending and show them separately when material.

Unless the user explicitly requests each account class, exclude accounts with
`is_closed = 1` or `is_separate = 1` from account-based totals, aggregates, and
report output. Join through `accounts` and apply
`a.is_closed = 0 AND a.is_separate = 0`; remove only the requested predicate
when opting into one class. Do not assume that a saved report's account
selection overrides this default.

## Budgets and reports

Inspect report definitions and budget periods before trusting a saved report total. Check whether a report stores filters, an expression, cached results, or only metadata. Reproduce the report from ledger rows when possible and compare the reproduced scope with the saved definition. State when a report cannot be reconstructed because the extract lacks category, account, split, or period detail.

For budgets, identify the budget, period grain, category/account dimension, and whether amounts are planned, actual, or variance. Do not compare budget and actual totals until their date range, sign convention, and category hierarchy are aligned.

## Evidence boundaries

`PRAGMA quick_check = ok`, foreign-key checks, and amount reconciliation support structural consistency. They do not prove that the export contains every Quicken table, that semantic labels match the Quicken UI, or that the extract faithfully represents a full QDF. Say “the extract contains” rather than “Quicken contains” unless independently verified.

## Export-specific provenance

The supported portable inputs are a QIF export or a complete extracted QDF
directory materialized to SQLite. A standalone transaction-only binary is not
enough to establish the account, balance, budget, or report context needed by
this skill. The Quicken Home Inventory (QHI) data family is separate from the
financial SQLite schema and should not be treated as financial transactions.

Before relying on a result, inspect the export metadata:

```sql
SELECT key, value
FROM metadata
WHERE key IN (
  'schema_version', 'source_format', 'source_name', 'source',
  'extract_date',
  'balance_source', 'saved_report_count', 'budget_count',
  'account_count', 'transaction_count', 'fi_transaction_count',
  'fi_linked_count'
)
ORDER BY key;
```

When present, `extract_date` is the ISO (`YYYY-MM-DD`) date on which the
SQLite database was materialized. Validate it with `date(value) IS NOT NULL`
and report it as unavailable when the key is absent or invalid. Do not use the
latest transaction date or the database file timestamp as a substitute.

The project can materialize portable QIF data or a native Windows QDF
extraction. `source_format` and `schema_version` identify which path produced
the database. A native QDF export may contain saved reports and native account
handles; a QIF materialization may not. Do not infer that a missing optional
table means an empty Quicken feature—report it as unavailable in this export.

For a quick availability check:

```sql
SELECT name, type
FROM sqlite_master
WHERE type IN ('table', 'view')
  AND name IN (
    'investment_transactions', 'reports', 'report_readable',
    'report_filter_values', 'budgets', 'budget_analytics'
  )
ORDER BY type, name;
```

`accounts.id` is only the SQLite export-local key. Native QDB account handles
are stored in `accounts.qdb_handle`; report and budget entity references use
that native handle. A handle should not be treated as stable across different
exports of the same file.

## Banking balance provenance

Use the interval predicate, including the nullable opening interval, for every
as-of query. Bind `:account_name` and ISO `:as_of_date`, validate the date, and
apply both boundaries. For positions, select the latest effective row per
security. If no effective row exists, report that the extract has no balance
for that account/date.

```sql
SELECT a.name,
       b.balance_date,
       b.next_balance_date,
       b.balance_cents,
       b.balance_cents / 100.0 AS balance_dollars
FROM accounts AS a
JOIN banking_account_balance_periods AS b ON b.account_id = a.id
WHERE a.name = :account_name
  AND a.is_closed = 0
  AND a.is_separate = 0
  AND date(:as_of_date) IS NOT NULL
  AND (b.balance_date IS NULL OR b.balance_date <= date(:as_of_date))
  AND (b.next_balance_date IS NULL OR date(:as_of_date) < b.next_balance_date);
```

`balance_cents` is authoritative for arithmetic. The displayed dollar value
is derived only for presentation. Inspect `metadata.balance_source`: native
QDF exports generally use the account-register `0xf7` amount stream when it is
valid and use a transaction-derived fallback otherwise. The canonical
financial `0x13c` rows are in `fi_transactions`; they must not be added to a
register-backed balance when they represent the same ledger entries.

## Transaction, investment, and balance provenance

There are three deliberately different transaction representations:

- `transactions` is the register-first primary ledger surface. It contains
  the displayed payee, memo, category, transfer destination, cleared state,
  common amount/date, and split details used for ordinary analysis.
- `fi_transactions` contains canonical FI/download rows. It preserves the
  downloaded payee, posted date and amount, FITID, FI record type, register
  reference, and the nullable `register_transaction_id` plus `link_method`.
  It is used for download-specific reporting and explicit reconciliation, not
  as an additional spending ledger.

- `investment_transactions` preserves native register rows, including rows
  with no common `transactions` match. It is the source for shares, prices,
  investment/cash amounts, transaction types, transfer IDs, and opening or
  backfill metadata.

The safe optional join is:

```sql
SELECT i.transaction_date,
       i.transaction_type,
       i.security_id,
       i.shares,
       i.price,
       i.transaction_amount,
       t.payee,
       t.memo,
       t.category,
       t.cleared
FROM investment_transactions AS i
LEFT JOIN transactions AS t ON t.id = i.transaction_id
WHERE i.account_id = :account_id
ORDER BY i.transaction_date, i.id;
```

For FI/register reconciliation, join only through the recorded register link:

```sql
SELECT f.transaction_date AS fi_date,
       f.amount AS fi_amount,
       f.payee AS downloaded_payee,
       f.fit_id,
       f.link_method,
       t.id AS register_transaction_id,
       t.transaction_date AS register_date,
       t.amount AS register_amount,
       t.payee,
       t.category
FROM fi_transactions AS f
LEFT JOIN transactions AS t
  ON t.id = f.register_transaction_id
WHERE f.qdb_record_type = 'QDB:0x13c'
ORDER BY f.transaction_date, f.id;
```

Do not infer a missing register transaction from a NULL
`register_transaction_id`; unmatched FI-only rows are valid export cases.

Never deduplicate investment rows by date, amount, or payee. Native rows may
be register-only, and a date/amount match is not an identity match.

For investment account totals, use
`investment_account_balance_periods`; for per-security shares and market
values, use `investment_position_balance_periods`. Both use the same effective
interval boundary as banking balances. If `cash_balance_status` is
`unanchored_opening_balance`, label the cash and total as transaction-derived
from an unanchored opening point. Include `cash_balance_source` when reporting
that caveat. `StkSplit` rows represent split ratios in the export's native
investment materialization, not ordinary signed share deltas.

## Budgets

First discover budget candidates covering the requested year range:

```sql
SELECT budget_qid,
       budget_name,
       MIN(year) AS first_year,
       MAX(year) AS last_year,
       COUNT(DISTINCT year) AS covered_years
FROM budgets
WHERE year BETWEEN :year_from AND :year_to
GROUP BY budget_qid, budget_name
ORDER BY budget_name, budget_qid;
```

If this returns more than one `budget_qid`, ask the user which budget to use
before reading amounts. A budget's `flags` is a bitmask; in the project,
`flags & 0x0800` indicates rollover enabled. `secondary_amount` can hold a
manual rollover amount. Parent and child category rows can both be present,
so retain `item_index`, `category_qid`, and flags and do not blindly sum every
row as an independent category.

## Saved reports

Report extraction is optional. When present, start with the readable view:

```sql
SELECT qdb_qid,
       report_name,
       report_type_name,
       component_name,
       selected_accounts,
       selected_categories
FROM report_readable
ORDER BY report_name, qdb_qid, component_index;
```

Then inspect `report_components`, `report_filter_groups`, and
`report_filter_values` for the requested report. Account/category filter
values use native `entity_ref` handles; resolve accounts through
`accounts.qdb_handle` and categories through `categories.qdb_handle`.
When reproducing report totals, exclude resolved accounts where
`accounts.is_closed = 1` or `accounts.is_separate = 1` unless the user
explicitly requests those accounts. This is a result filter only; never modify
the saved report definition.
`report_readable` is a decoded convenience view, not a complete renderer:
date ranges, columns, grouping, subtotals, and some filter semantics may not
be fully decoded. Reproduce a report from ledger data only after confirming
its scope, and state when exact Quicken UI semantics are unavailable.

## Source-family reconciliation

The revised native export retains parallel representations:

- `transactions.qdb_record_type = 'QDB:0xf7'`: account-register objects with
  displayed payee, memo, category, transfer destination, cleared state, and
  split accessors. This is the primary analysis family.
- `fi_transactions.qdb_record_type = 'QDB:0x13c'`: canonical FI/download
  objects with downloaded payee, posted date/amount, FITID, and optional
  register links. This is the download/reconciliation family.

Legacy or alternate source families may also share FITIDs or reverse amounts.
Use the declared source family and recorded link fields; do not union all
families into a larger ledger. When a total is sensitive to the choice, report
which family was used and validate it independently.
