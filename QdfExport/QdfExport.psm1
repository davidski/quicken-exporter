Set-StrictMode -Version Latest

function Export-QdfFinancial {
    <#
    .SYNOPSIS
    Exports a Quicken QDF file to a read-only SQLite database containing
    financial data, budgets, security price history, and saved reports.

    .DESCRIPTION
    Runs the read-only native QDF extraction stage through Quicken's 32-bit
    native DLLs, then runs the bundled Python materializer through uv. A
    packaged QdfExport module uses the precompiled helpers in its bin
    directory; a repository checkout compiles the helpers when needed.

    The QDF source is not modified. Unless KeepExtraction is specified, the
    intermediate extraction directory is created under the system TEMP
    directory and removed after the export. Use KeepExtraction to retain it
    beside the requested SQLite output for troubleshooting or to rerun the
    portable materializer.

    .PARAMETER QdfPath
    Path to the source Quicken QDF file. The file must be readable by the
    current account and is opened read-only.

    .PARAMETER OutputSqlite
    Path for the resulting SQLite database. Its parent directory is created if
    necessary.

    .PARAMETER DatafilePassword
    Optional Quicken data-file password as a SecureString. It is sent to the
    native helper through standard input and is never placed on its command
    line. Use Read-Host -AsSecureString to enter it interactively.

    .PARAMETER PromptForPassword
    If the QDF is protected and no DatafilePassword was supplied, prompt for the
    password after the initial unprotected open attempt.

    .PARAMETER KeepExtraction
    Retains the intermediate qdb-financial-extract directory after completion
    or failure. Without this switch, the directory is removed.

    .PARAMETER QuickenDirectory
    Directory containing the matching 32-bit Quicken DLL set. The default is
    C:\Program Files (x86)\Quicken.

    .PARAMETER UvPath
    Command name or path of the uv executable used to run the Python
    materializer. The default is uv.

    .EXAMPLE
    Import-Module C:\Tools\QdfExport\QdfExport.psd1
    Export-QdfFinancial -QdfPath C:\Data\QData.QDF `
        -OutputSqlite C:\Exports\quicken.sqlite

    Exports a QDF using the default Quicken directory and uv command.

    .EXAMPLE
    Export-QdfFinancial -QdfPath C:\Data\QData.QDF `
        -OutputSqlite C:\Exports\quicken.sqlite `
        -DatafilePassword (Read-Host 'QDF password' -AsSecureString) `
        -QuickenDirectory C:\Tools\quicken-runtime `
        -UvPath C:\Tools\uv\uv.exe `
        -KeepExtraction

    Uses an isolated Quicken DLL directory, an explicit uv path, and retains
    the intermediate records for inspection.

    .NOTES
    The exporter directly requires Windows PowerShell 5.1, .NET Framework 4.x
    for the managed helpers, and uv for the Python/SQLite stage. The matching
    Quicken DLL installation is an external native runtime boundary.

    .LINK
    README.md
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$QdfPath,

        [Parameter(Mandatory = $true)]
        [string]$OutputSqlite,

        [Security.SecureString]$DatafilePassword,

        [switch]$PromptForPassword,

        [string]$StartDate,

        [string]$EndDate,

        [switch]$KeepExtraction,

        [string]$QuickenDirectory = 'C:\Program Files (x86)\Quicken',
        [string]$UvPath = 'uv'
    )

    $scriptPath = Join-Path $PSScriptRoot 'scripts\windows\Export-QdfFinancial.ps1'
    & $scriptPath @PSBoundParameters
    if ($LASTEXITCODE -ne 0) {
        throw "QDF exporter returned exit code $LASTEXITCODE"
    }
}

function Register-QdfScheduledExport {
    <#
    .SYNOPSIS
    Registers a daily Windows Task Scheduler job for QDF SQLite export.

    .DESCRIPTION
    Registers the Daily QDF SQLite Export task using the supplied configuration
    file. The task runs the module's scheduled-export worker, which performs a
    local extraction and SQLite build, writes a transcript log, and atomically
    publishes the completed database to the configured destination.

    The configuration file must define QdfPath, QuickenDirectory, UvPath,
    DestinationDirectory, OutputFilePattern, StagingDirectory, and LogDirectory.
    Use an absolute path for ConfigPath because Task Scheduler stores the
    resulting script and configuration paths.

    By default, PowerShell prompts for the Windows account that will run the
    task. RunAsSystem avoids that prompt, but LocalSystem generally cannot
    access a network share unless the share grants access to the computer
    account.

    .PARAMETER ConfigPath
    Path to the PowerShell data configuration file for the scheduled export.

    .PARAMETER TaskName
    Name assigned to the registered scheduled task. The default is Daily QDF
    SQLite Export.

    .PARAMETER At
    Local time at which the task runs each day. The default is 02:00.

    .PARAMETER RunAsSystem
    Registers the task as LocalSystem instead of prompting for credentials.
    This is appropriate only when the task account can access the QDF,
    Quicken directory, staging paths, and destination.

    .EXAMPLE
    $configPath = 'C:\path\you\chose\QdfScheduledExport.config.psd1'
    Import-Module C:\Tools\QdfExport\QdfExport.psd1
    Register-QdfScheduledExport `
        -ConfigPath $configPath `
        -At '02:00'

    Registers a daily export and prompts for the task account credentials.

    .EXAMPLE
    $configPath = 'C:\path\you\chose\QdfScheduledExport.config.psd1'
    Register-QdfScheduledExport `
        -ConfigPath $configPath `
        -TaskName 'QDF export' `
        -At '03:30' `
        -RunAsSystem

    Registers the task under LocalSystem with a custom name and schedule.

    .NOTES
    The task is configured to start when available, ignore overlapping runs,
    allow up to four hours, and retry twice after failure. Test the task with
    Start-ScheduledTask and inspect the configured log directory.

    .LINK
    README.md
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ConfigPath,

        [string]$TaskName = 'Daily QDF SQLite Export',
        [datetime]$At = '02:00',
        [switch]$RunAsSystem
    )

    $scriptPath = Join-Path $PSScriptRoot 'scripts\windows\Register-QdfScheduledExport.ps1'
    & $scriptPath @PSBoundParameters
}

function Reproduce-QdfReport {
    <#
    .SYNOPSIS
    Reproduces a saved report from an existing SQLite export.

    .DESCRIPTION
    Reads a self-contained financial SQLite export and writes the selected
    saved report as a portable TSV. This is a post-extraction operation; it
    does not open or modify a QDF file.

    .PARAMETER Database
    Path to the financial SQLite export.

    .PARAMETER Output
    Destination TSV path.

    .PARAMETER Report
    Saved report name stored in the SQLite export. Defaults to Grocery expenses.

    .PARAMETER UvPath
    Command name or path of the uv executable used to run the bundled script.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)] [string]$Database,
        [Parameter(Mandatory = $true)] [string]$Output,
        [string]$Report = 'Grocery expenses',
        [string]$UvPath = 'uv'
    )

    $scriptPath = Join-Path $PSScriptRoot 'scripts\windows\Reproduce-QdfReport.ps1'
    & $scriptPath @PSBoundParameters
    if ($LASTEXITCODE -ne 0) {
        throw "QDF report reproduction returned exit code $LASTEXITCODE"
    }
}

Export-ModuleMember -Function Export-QdfFinancial, Reproduce-QdfReport, Register-QdfScheduledExport
