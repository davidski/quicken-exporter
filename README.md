# Quicken exporter

I've been a Quicken user for over 30 (!) years and have a truly stupid amount of
data in the program. Getting access to this information in a form that I can
analyze and blend with other data sources has been a long-desired goal for my
recovering data scientist persona.

This repository reads Quicken Classic for Windows data without modifying the
source files, then writes SQLite databases I can query and use elsewhere.

Native QDF extraction runs on Windows through Quicken's installed 32-bit DLLs.
QIF and QHI/Home Inventory export require Python 3.11+; `uv` is a recommended
runner, not a runtime dependency.

## Start here

- **Recommended: QDF on Windows.** A QDF is your Quicken data file; this is
  the richest export.
- **Fallback: QIF.** A QIF is exported from Quicken and produces a less
  complete but useful SQLite database.
- **Separate: QHI/Home Inventory.** Use this only with a `QHI.IDB` file.

If you use Quicken for Mac, see the note near the end of this README.

## Exporting

Source files stay unchanged; the exporter writes or replaces the requested
SQLite and report outputs. The commands below use `uv`.

### QDF on Windows

**Close Quicken before every QDF extraction.** The QDF must not be in use by
any process, or extraction fails as though it were password-protected.

On the Windows computer with Quicken, install Python 3.11+ and `uv` (for
example, `winget install astral-sh.uv`). You also need Windows PowerShell 5.1
and .NET Framework 4.x. Quicken supplies the matching DLLs; do not copy or use
them directly.

From this repository, run the wrapper in PowerShell:

```powershell
.\scripts\windows\Export-QdfFinancial.ps1 `
  -QdfPath 'C:\path\to\QData.QDF' `
  -OutputSqlite .\output\quicken.sqlite
```

This writes the SQLite database at `output\quicken.sqlite`.

The Windows stage uses C# helpers to access Quicken's native DLLs. Source under
`scripts/windows` is compiled when needed, so the executables are reproducible;
you normally use only the PowerShell wrapper.

For a protected QDF, use `-PromptForPassword` or pass `-DatafilePassword` as a
`SecureString`; it is sent through standard input, never the command line.
`-KeepExtraction` preserves intermediate files; `-StartDate` and `-EndDate`
limit the transaction window.

### QIF

QIF is a portable fallback, not the primary path: it lacks native account
metadata, balances, budgets, saved reports, and QDF investment/FI relationships.

Starting from a QIF file, use:

```sh
uv run --script scripts/export_financial_sqlite.py \
  exported.qif output/transactions.sqlite --format qif
```

Optional date limits can be supplied with `--start-date YYYY-MM-DD` and
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
materialized on another platform:

```sh
uv run --script scripts/export_financial_sqlite.py \
  qdb-financial-extract output/quicken.sqlite --format qdb
```

### QHI/Home Inventory

QHI uses a standalone `QHI.IDB` file and no Quicken DLLs:

```sh
uv run --script scripts/export_qhi_sqlite.py \
  "/path/to/your/QHI.IDB" "/path/to/home_inventory.sqlite"
```

Replace both paths with your files.

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
x86 helpers and does not need `csc.exe`.

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

Only the account that created this file can use it.

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

## A note for Mac users

Quicken for Mac users already have a SQLite database and do not need this
exporter. [quicken-mac-mcp](https://github.com/dweekly/quicken-mac-mcp) may help
connect that database to chat agents and other tools.

## AI disclosure

AI is used extensively in development of this project. I have worked closely
with the AI agents to refine goals and approaches. Areas with heavy AI include
some of the tedious script writing and many Windows particulars. I've
reviewed intermediate steps and validated the end products, which I use on a
regular basis.

## License and boundaries

This is for licensed Quicken users who want access to their own data. It does
not modify QDF files, redistribute Quicken software, or bypass licensing or
access controls. Quicken is a trademark of its respective owner; this project
is not affiliated with Quicken Inc. See [LICENSE](LICENSE).
