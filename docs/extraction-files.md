# Windows extraction files

`Export-QdfFinancial.ps1` runs the helpers in `scripts/windows/`, which
write an extraction directory. `export_financial_sqlite.py --format qdb` turns
that directory into SQLite. This page lists what each file holds.

Type numbers are Quicken's internal QDB item types as seen through `qdb.dll`.
These may differ between Quicken builds.

## Fixed-record files (`qdb-type-XXX.bin`)

Each one holds a single item type. It starts with a little-endian `uint32`
record size and `uint32` record count, followed by `count` records numbered
from key 1. A type with no records still gets a header with count 0. If a
required type comes back with no item spec or an invalid record size, the
helper exits with code 5.

### Required

The export fails if any of these is missing.

| File | Type | Contents |
|---|---|---|
| `qdb-type-13c.bin` | 0x13c | Canonical transactions, 961-byte records: date, amount, payee, FITID, and string-pool references. Primary source for `transactions`. |
| `qdb-type-134.bin` | 0x134 | Account index, 211-byte records. One row per canonical transaction, tying it to its account. |
| `qdb-type-080.bin` | 0x80 | Account and category catalog: account names and high-level kind, plus the category list (`:ZZZZZ` group sentinels are skipped). |

### Optional, decoded

The export uses these when present.

| File | Type | Contents |
|---|---|---|
| `qdb-type-0f7.bin` | 0xf7 | Native register rows. Source of displayed payees and register balances (`balance_source`). |
| `qdb-type-096.bin` | 0x96 | Identity links pairing a canonical 0x134 row with its 0xf7 register row. |
| `qdb-type-086.bin` | 0x86 | Legacy transaction records. They carry the register reference and FITID, which bridges 0xf7 and 0x13c. |
| `qdb-type-08e.bin` | 0x8e | Pending transactions, used for payee and category overrides. |
| `qdb-type-0b7.bin` | 0xb7 | Pending payees: the user-facing payee at 0x63 and the downloaded payee at 0x67. |
| `qdb-type-07f.bin` | 0x7f | Tag catalog, 135-byte records. |
| `qdb-type-144.bin` | 0x144 | Budget headers. Written by `Extract-QdbVariableType.exe`. |
| `qdb-type-14b-full.bin` | 0x14b | Budget years, in a variable-length layout (key and size per record). Written by `Extract-QdbVariableType.exe`. |
| `qdb-type-120.bin` | 0x120 | Saved report records. Written by `Extract-QdbReports.exe`. |

### Optional, not decoded

`Extract-QdbFinancial.exe` also dumps about 70 transaction-family companion
types, listed in the `foreach` in `Extract-QdbFinancial.cs`. They are kept for
research and are not parsed. 0xb1–0xb6, 0xf8, 0xf9, 0x113,
0x123, 0x127, 0x128, and 0x135 are scanned for string IDs when building
`qdb-string-map.tsv`.

## Other binary files

| File | Required | Contents |
|---|---|---|
| `qdb-account-map-134.bin` | yes | `QATM` v1 header, then for each 0x134 row an (account handle, account key) pair plus the 211-byte record. Written by `Extract-QdbAccountMap.exe`. |
| `qdb-reports.bin` | no | Lossless saved-report definitions, parsed by `qdb_reports.py`. |

## TSV sidecars

Written by the helpers from qaccess APIs.

| File | Required | Contents |
|---|---|---|
| `qdb-string-map.tsv` | yes | QDB string-pool ID → text for payees and categories. Category names start with `=`. |
| `qdb-account-status.tsv` | no | Per account handle: type, subtype, closed, separate, hidden in bar, and hidden in list. |
| `qdb-register-memo.tsv` | no | Memo and check number for each register reference. |
| `qdb-register-splits.tsv` | no | Split lines from qaccess's native Transaction object. |
| `qdb-register-clear-status.tsv` | no | Cleared status for each register reference. |
| `qdb-canonical-check-numbers.tsv` | no | Check numbers read directly from canonical transaction objects. |
| `qdb-investment-transactions.tsv` | no | Native investment transactions: shares, prices, and cash and transfer legs. |
| `qdb-securities.tsv` | no | Security reference, name, and symbol. |
| `qdb-price-history.tsv` | no | Price quotes for each security. |
| `qdb-report-records.tsv` | no | Saved-report record index. |

## Helper exit codes

`Extract-QdbFinancial.exe` exits with:

| Code | Meaning |
|---|---|
| 0 | Success |
| 3 | `qdb.dll` couldn't open the QDF |
| 4 | The QDF needs a data-file password (`Export-QdfFinancial.ps1` prompts for one with `-PromptForPassword`) |
| 5 | A required record type has no item spec or an invalid record size, usually an unsupported Quicken build. The type, count and size are written to stderr. |

Any other non-zero code is an unhandled crash.
