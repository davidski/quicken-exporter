# Quicken exporter

> Create a [SQLite](https://www.sqlite.org/) database from your Quicken Classic
> for Windows data, giving you free (as in speech) access to your data.

I've been a Quicken user for over 30 (!) years and have a truly stupid amount of
data in the program. Getting access to this information in a form that I can
analyze and blend with other data sources scratches my recovering data scientist itch.

## Start here

- **Recommended: QDF on Windows.** Reads in your QDF and outputs a SQLite DB.
- **Fallback: QIF.** A QIF is exported from Quicken and produces a less
  complete but useful SQLite database.
- **Optional: QHI/Home Inventory.** If you use the deprecated Quicken Home
- Inventory program, that can also be exported into a standalone DB.

- **Quicken for Mac: unsupported**  QMac already uses a SQLite database, though
- it's encrypted. [quicken-mac-mcp](https://github.com/dweekly/quicken-mac-mcp)
  may help connect that database to AI agents and other tools.

## AI disclosure

AI is used extensively in the developing and maintaining this project. I work
closely with the AI agents to refine goals and approaches, trying to keep
slop to a minimum. Areas with heavy AI include some of the tedious script
writing and many Windows particulars. I've reviewed intermediate steps and
validated the end products, which I use on a regular basis. YMMV.

## Exporting

All exports are read-only and make no changes to your Quicken data files.
Each export creates a fresh database, overwriting any previously existing export.

### QDF on Windows

**Close Quicken before every QDF extraction.** The QDF must not be in use by
any process, or extraction fails as though it were password-protected.

On the Windows computer with Quicken, install Python 3.11+ and `uv` (for
example, `winget install astral-sh.uv`). You also need Windows PowerShell 5.1
and .NET Framework 4.x. Quicken supplies the matching DLLs; do not copy or use
them directly.

From this repository, run the PowerShell wrapper:

```powershell
.\scripts\windows\Export-QdfFinancial.ps1 `
  -QdfPath 'C:\path\to\QData.QDF' `
  -OutputSqlite .\output\quicken.sqlite
```

This reads the QDF at C:\path\to and writes the SQLite database at `output\quicken.sqlite`.

The Windows stage uses C# helpers to access Quicken's native DLLs. Source under
`scripts/windows` is compiled when needed, so the executables are reproducible;
you normally use only the PowerShell wrapper.

For a protected QDF, use `-PromptForPassword` or pass `-DatafilePassword` as a
`SecureString`; it is sent through standard input, never the command line.
`-KeepExtraction` preserves intermediate files; `-StartDate` and `-EndDate`
limit the transaction window.

### QIF

A QIF export from Quicken can be used instead of starting from a QDF. This is
an alternate route and lacks several things your get from the QDF reader,
including native account metadata, balances, budgets, saved reports, and
investment/FI relationships.

Starting from a QIF file, use:

```sh
uv run --script scripts/export_financial_sqlite.py \
  exported.qif output/transactions.sqlite --format qif
```

Like with QDF reading, date limits can be supplied with `--start-date YYYY-MM-DD` and
`--end-date YYYY-MM-DD`.

## What is and is not supported

The export covers spending and investment transactions, categories, tags, and
separate QHI Home Inventory data. Native QDF exports also include security price
history, budgets, and saved-report definitions. It does not preserve attachments
or every Quicken-specific semantic: detailed loan-payment and net-paycheck
budget lines are not fully supported. Report reproduction supports only the
available report types and data; it is not a Quicken display clone.

## Advanced and separate workflows

### Materialize an existing Windows extraction

After Windows extracts the QDF, the complete extraction directory can be
taken to another platform (e.g. Linux, MacOS, or any other python supported 
environment) and converted into a DB:

```sh
uv run --script scripts/export_financial_sqlite.py \
  qdb-financial-extract output/quicken.sqlite --format qdb
```

[docs/extraction-files.md](docs/extraction-files.md) lists every file in the
directory and which ones are required.

### QHI/Home Inventory

Exporting QHI data is straightforward. Just replace the path to the QHI file
and your desired output location in the following:

```sh
uv run --script scripts/export_qhi_sqlite.py \
  "/path/to/your/QHI.IDB" "/path/to/home_inventory.sqlite"
```

### Windows package and scheduled export

On a Windows build machine with Windows PowerShell 5.1 and .NET Framework 4.x,
build the self-contained module bundle:

```powershell
.\scripts\windows\Package-QdfExport.ps1 `
  -OutputPath .\output\qdf-export-bundle.zip
```

The archive contains a `QdfExport` directory. Extract it under a stable
location such as `C:\Tools`, install `uv`, then import
`C:\Tools\QdfExport\QdfExport.psd1`:

```powershell
Import-Module 'C:\Tools\QdfExport\QdfExport.psd1'
```

The machine needs matching Quicken DLLs; the bundle has the four precompiled
x86 helpers.

For a daily export, copy
`QdfExport\scripts\windows\QdfScheduledExport.config.psd1.example` and set its
absolute paths.

For a protected QDF, sign in as the task's Windows account and create its
encrypted password file before registration:

```powershell
New-Item -ItemType Directory -Force 'C:\ProgramData\QdfExport' | Out-Null
$password = Read-Host 'QDF password' -AsSecureString
$password | ConvertFrom-SecureString |
  Set-Content 'C:\ProgramData\QdfExport\qdf-password.dpapi'
```

Add this path to the copied configuration; never put the plaintext password
there.

```powershell
DatafilePasswordSecretPath = 'C:\ProgramData\QdfExport\qdf-password.dpapi'
```

Then register and test the task:

```powershell
$configPath = 'C:\path\QdfScheduledExport.config.psd1'
Import-Module 'C:\Tools\QdfExport\QdfExport.psd1'
Register-QdfScheduledExport -ConfigPath $configPath -At '02:00'
Start-ScheduledTask -TaskName 'Daily QDF SQLite Export'
Get-ScheduledTaskInfo -TaskName 'Daily QDF SQLite Export'
```

Use a UNC destination for network shares. The task builds locally and replaces
the destination only after success. Schedule it while Quicken and the QDF are
closed, or it can report a misleading password-protected-QDF error.

### Reproduce a saved report

Saved-report reproduction is a separate, read-only operation on an existing
financial SQLite export:

```powershell
Reproduce-QdfReport `
  -Database C:\Exports\quicken.sqlite `
  -Output C:\Exports\report.tsv `
  -Report 'Grocery expenses'
```

## Using the database with chat agents

The included [`quicken-sqlite-query` skill](skills/quicken-sqlite-query/SKILL.md)
gives chat agents a safe, read-only workflow for schema/provenance checks,
transaction and investment queries, balances, budgets, and register/download
distinctions.

Install it for your agents with the [`skills`](https://www.npmjs.com/package/skills)
CLI:

```sh
pnpx skills add davidski/quicken-exporter --skill quicken-sqlite-query -g
```

`-g` installs for your user rather than the current project. Update or remove
it later with `pnpx skills update` and `pnpx skills remove quicken-sqlite-query -g`.

## Development

Install the locked development tools, then lint, format-check, and test:

```sh
uv sync --group dev
uv run --group dev ruff check .
uv run --group dev ruff format --check .
uv run --group dev python -m pytest
```

The tests use small synthetic fixtures; no Quicken data is needed.

## License and boundaries

This is for licensed Quicken users who want access to their own data. It does
not modify QDF files, redistribute Quicken software, or bypass licensing or
access controls. Quicken is a trademark of its respective owner; this project
is not affiliated with Quicken Inc. See [LICENSE](LICENSE).
