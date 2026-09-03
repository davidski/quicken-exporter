[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$QdfPath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputSqlite,

    [Security.SecureString]$DatafilePassword,

    [switch]$PromptForPassword,

    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$StartDate,

    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$EndDate,

    [switch]$KeepExtraction,

    [string]$QuickenDirectory = 'C:\Program Files (x86)\Quicken',
    [string]$UvPath = 'uv'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-QdfProgress {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    # Keep human-readable progress visible in a console and transcript without
    # treating it as the command's pipeline output.
    Write-Information -MessageData $Message -InformationAction Continue
}

function Resolve-QdfExportRoot {
    $candidate = (Resolve-Path -LiteralPath $PSScriptRoot).Path
    while ($null -ne $candidate) {
        $portableScript = Join-Path $candidate 'scripts\export_financial_sqlite.py'
        $sourceDirectory = Join-Path $candidate 'src\qdf_tools'
        if ((Test-Path -LiteralPath $portableScript -PathType Leaf) -and
            (Test-Path -LiteralPath $sourceDirectory -PathType Container)) {
            return $candidate
        }
        $parent = [IO.Directory]::GetParent($candidate)
        if ($null -eq $parent) { break }
        $candidate = $parent.FullName
    }
    throw "Could not locate the QDF export root from: $PSScriptRoot"
}

$exportRoot = Resolve-QdfExportRoot
if (-not (Test-Path -LiteralPath $QdfPath -PathType Leaf)) {
    throw "QDF file not found: $QdfPath"
}
$qdf = (Resolve-Path -LiteralPath $QdfPath).Path
$sqlite = [IO.Path]::GetFullPath($OutputSqlite)
$sqliteDirectory = [IO.Path]::GetDirectoryName($sqlite)
if ($KeepExtraction) {
    $extractDirectory = [IO.Path]::GetFullPath((Join-Path $sqliteDirectory 'qdb-financial-extract'))
    $extractionLockPath = Join-Path $sqliteDirectory 'qdb-financial-extract.lock'
}
else {
    # Use a per-run directory in TEMP so non-kept runs do not leave
    # intermediary files beside the requested SQLite output.
    $extractDirectory = Join-Path ([IO.Path]::GetTempPath()) (
        'qdb-financial-extract-' + [guid]::NewGuid().ToString('N')
    )
    $extractionLockPath = $null
}
$extractionMarker = Join-Path $extractDirectory '.qdf-export-extraction'
$extractionLock = $null
$ownsExtractionDirectory = $false
$helper = Join-Path $PSScriptRoot 'Extract-QdbFinancial.cs'
$mapHelper = Join-Path $PSScriptRoot 'Extract-QdbAccountMap.cs'
$reportHelper = Join-Path $PSScriptRoot 'Extract-QdbReports.cs'
$variableHelper = Join-Path $PSScriptRoot 'Extract-QdbVariableType.cs'
$passwordHelper = Join-Path $PSScriptRoot 'QdbPassword.cs'
$assemblyInfo = Join-Path $PSScriptRoot 'QdfExportAssemblyInfo.cs'
$precompiledDirectory = Join-Path $exportRoot 'bin'
$precompiledHelperExe = Join-Path $precompiledDirectory 'Extract-QdbFinancial.exe'
$precompiledMapHelperExe = Join-Path $precompiledDirectory 'Extract-QdbAccountMap.exe'
$precompiledReportHelperExe = Join-Path $precompiledDirectory 'Extract-QdbReports.exe'
$precompiledVariableHelperExe = Join-Path $precompiledDirectory 'Extract-QdbVariableType.exe'
# A packaged module carries all four helpers. If this is a repository checkout
# or an incomplete bundle, retain the development-time compile-on-run path.
$missingPrecompiledHelpers = @(
    $precompiledHelperExe,
    $precompiledMapHelperExe,
    $precompiledReportHelperExe,
    $precompiledVariableHelperExe
) | ForEach-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Where-Object { -not $_ } | Measure-Object | Select-Object -ExpandProperty Count
$portableScript = Join-Path $exportRoot 'scripts\export_financial_sqlite.py'
$budgetScript = Join-Path $exportRoot 'scripts\export_qdb_budgets.py'

function Resolve-QdfCSharpCompiler {
    $frameworkRoot = Join-Path $env:WINDIR 'Microsoft.NET\Framework'
    $compiler = Get-ChildItem -LiteralPath $frameworkRoot -Directory -Filter 'v*' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^v\d' } |
        Sort-Object { [version]$_.Name.Substring(1) } -Descending |
        ForEach-Object {
            $candidate = Join-Path $_.FullName 'csc.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { $candidate }
        } |
        Select-Object -First 1
    if ($null -eq $compiler) {
        throw "32-bit .NET Framework C# compiler not found under: $frameworkRoot"
    }
    return (Resolve-Path -LiteralPath $compiler).Path
}

function ConvertTo-QdfPasswordPayload {
    param(
        [Parameter(Mandatory = $true)]
        [Security.SecureString]$Password
    )

    $bstr = [IntPtr]::Zero
    $characters = $null
    $utf8 = $null
    try {
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password)
        $characters = New-Object char[] $Password.Length
        if ($characters.Length -gt 0) {
            [Runtime.InteropServices.Marshal]::Copy($bstr, $characters, 0, $characters.Length)
        }
        $utf8 = [Text.Encoding]::UTF8.GetBytes($characters)
        return [Convert]::ToBase64String($utf8)
    }
    finally {
        if ($null -ne $utf8) { [Array]::Clear($utf8, 0, $utf8.Length) }
        if ($null -ne $characters) { [Array]::Clear($characters, 0, $characters.Length) }
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }
}

function Invoke-QdfNativeStage {
    param(
        [Parameter(Mandatory = $true)] [string]$Name,
        [Parameter(Mandatory = $true)] [string]$Executable,
        [Parameter(Mandatory = $true)] [object[]]$Arguments,
        [Parameter(Mandatory = $true)] [string]$FailureMessage,
        [Security.SecureString]$DatafilePassword,
        [switch]$AllowPasswordRequired
    )

    Write-QdfProgress "[QDF export] $Name..."
    $started = Get-Date
    $nativeArguments = @($Arguments)
    $passwordPayload = $null
    if ($null -ne $DatafilePassword) {
        $nativeArguments += '--password-stdin'
        $passwordPayload = ConvertTo-QdfPasswordPayload $DatafilePassword
    }
    if ($VerbosePreference -eq 'Continue') {
        if ($null -ne $passwordPayload) {
            $passwordPayload | & $Executable @nativeArguments | ForEach-Object {
                Write-Verbose -Message ([string]$_)
            }
        }
        else {
            & $Executable @nativeArguments | ForEach-Object {
                Write-Verbose -Message ([string]$_)
            }
        }
    }
    else {
        # The native helpers have useful diagnostics, but their default output
        # is a per-record dump. Keep it available through -Verbose without
        # making the normal export log thousands of lines long.
        if ($null -ne $passwordPayload) {
            $passwordPayload | & $Executable @nativeArguments | Out-Null
        }
        else {
            & $Executable @nativeArguments | Out-Null
        }
    }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 4 -and $AllowPasswordRequired) { return 4 }
    if ($exitCode -ne 0) { throw $FailureMessage }
    $elapsed = ((Get-Date) - $started).TotalSeconds
    Write-QdfProgress ("[QDF export] {0} complete ({1:0.0}s)" -f $Name, $elapsed)
    return 0
}

if (-not (Test-Path -LiteralPath $QuickenDirectory -PathType Container)) { throw "Quicken directory not found: $QuickenDirectory" }
if (-not (Get-Command $UvPath -ErrorAction SilentlyContinue)) { throw "uv executable not found: $UvPath" }
if (-not (Test-Path -LiteralPath $portableScript -PathType Leaf)) { throw "portable export script not found: $portableScript" }
if (-not (Test-Path -LiteralPath $budgetScript -PathType Leaf)) { throw "budget export script not found: $budgetScript" }

if ($missingPrecompiledHelpers -eq 0) {
    $helperExe = $precompiledHelperExe
    $mapHelperExe = $precompiledMapHelperExe
    $reportHelperExe = $precompiledReportHelperExe
    $variableHelperExe = $precompiledVariableHelperExe
}
else {
    $compiler = Resolve-QdfCSharpCompiler
    if (-not (Test-Path -LiteralPath $compiler)) { throw "C# compiler not found: $compiler" }
    if (-not (Test-Path -LiteralPath $assemblyInfo -PathType Leaf)) {
        throw "assembly metadata source not found: $assemblyInfo"
    }
    $helperExe = Join-Path $extractDirectory 'Extract-QdbFinancial.exe'
    $mapHelperExe = Join-Path $extractDirectory 'Extract-QdbAccountMap.exe'
    $reportHelperExe = Join-Path $extractDirectory 'Extract-QdbReports.exe'
    $variableHelperExe = Join-Path $extractDirectory 'Extract-QdbVariableType.exe'
}

if ($missingPrecompiledHelpers -ne 0) {
    if (-not (Test-Path -LiteralPath $passwordHelper -PathType Leaf)) {
        throw "qdb password bridge source not found: $passwordHelper"
    }
}

New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($sqlite)) | Out-Null

try {
    if ($null -ne $extractionLockPath) {
        try {
            $extractionLock = [IO.File]::Open(
                $extractionLockPath,
                [IO.FileMode]::OpenOrCreate,
                [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::None
            )
        }
        catch {
            throw "Another QDF export appears to be using the extraction directory: $extractDirectory"
        }
    }

    # Always start from a fresh bundle. The marker prevents an unrelated
    # directory with the conventional name from being removed accidentally.
    if (Test-Path -LiteralPath $extractDirectory) {
        if (-not (Test-Path -LiteralPath $extractionMarker -PathType Leaf)) {
            throw "Refusing to remove an unrecognized extraction directory: $extractDirectory"
        }
        Remove-Item -LiteralPath $extractDirectory -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $extractDirectory | Out-Null
    Set-Content -LiteralPath $extractionMarker -Value 'QDF export extraction directory' -Encoding ASCII
    $ownsExtractionDirectory = $true

    $overallStarted = Get-Date
    Write-QdfProgress ("[QDF export] Starting: {0} -> {1}" -f $qdf, $sqlite)

    if ($missingPrecompiledHelpers -ne 0) {
        Write-QdfProgress '[QDF export] Preparing native extractors...'
        & $compiler /nologo /platform:x86 /target:exe /out:$helperExe $helper $passwordHelper $assemblyInfo
        if ($LASTEXITCODE -ne 0) { throw "Failed to compile the qdb financial extractor" }
        & $compiler /nologo /platform:x86 /target:exe /out:$mapHelperExe $mapHelper $passwordHelper $assemblyInfo
        if ($LASTEXITCODE -ne 0) { throw "Failed to compile the qdb account-index extractor" }
        & $compiler /nologo /platform:x86 /target:exe /out:$reportHelperExe $reportHelper $passwordHelper $assemblyInfo
        if ($LASTEXITCODE -ne 0) { throw "Failed to compile the qdb saved-report extractor" }
        & $compiler /nologo /platform:x86 /target:exe /out:$variableHelperExe $variableHelper $passwordHelper $assemblyInfo
        if ($LASTEXITCODE -ne 0) { throw "Failed to compile the qdb variable-type extractor" }
        Write-QdfProgress '[QDF export] Native extractors ready'
    }

    Push-Location -LiteralPath $QuickenDirectory
    try {
        $financialArguments = @($qdf, $extractDirectory)
        $mapArguments = @($qdf, (Join-Path $extractDirectory 'qdb-account-map.bin'))
        $reportArguments = @($qdf, $extractDirectory)
        $financialStatus = Invoke-QdfNativeStage 'Extracting financial records' $helperExe $financialArguments 'qdb.dll could not open the QDF. Check the data-file password and matching Quicken DLL build.' -DatafilePassword $DatafilePassword -AllowPasswordRequired
        if ($financialStatus -eq 4) {
            if (-not $PromptForPassword) {
                throw "QDF requires a data-file password. Supply -DatafilePassword (Read-Host 'QDF password' -AsSecureString) or use -PromptForPassword."
            }
            $DatafilePassword = Read-Host -Prompt 'QDF data-file password' -AsSecureString
            Invoke-QdfNativeStage 'Extracting financial records' $helperExe $financialArguments 'qdb.dll rejected the data-file password or could not open the QDF.' -DatafilePassword $DatafilePassword | Out-Null
        }
        Invoke-QdfNativeStage 'Building account and register indexes' $mapHelperExe $mapArguments 'qdb.dll account-index extraction failed. Check the data-file password and matching Quicken DLL build.' -DatafilePassword $DatafilePassword | Out-Null
        Invoke-QdfNativeStage 'Extracting saved reports' $reportHelperExe $reportArguments 'qdb.dll saved-report extraction failed. Check the data-file password and matching Quicken DLL build.' -DatafilePassword $DatafilePassword | Out-Null
        Invoke-QdfNativeStage 'Extracting budget metadata' $variableHelperExe @(
            $qdf, 144, (Join-Path $extractDirectory 'qdb-type-144.bin')
        ) 'qdb.dll budget-header extraction failed. Check the data-file password and matching Quicken DLL build.' -DatafilePassword $DatafilePassword | Out-Null
        Invoke-QdfNativeStage 'Extracting budget years' $variableHelperExe @(
            $qdf, '14b', (Join-Path $extractDirectory 'qdb-type-14b-full.bin')
        ) 'qdb.dll budget-year extraction failed. Check the data-file password and matching Quicken DLL build.' -DatafilePassword $DatafilePassword | Out-Null
    }
    finally { Pop-Location }

    # Keep the Python stage uv-only.  --script uses the PEP 723 script runner
    # and does not create or depend on a project .venv.  qdb.dll extraction
    # remains the preceding Windows-only C# stage.
    $dateArguments = @()
    if ($StartDate) { $dateArguments += @('--start-date', $StartDate) }
    if ($EndDate) { $dateArguments += @('--end-date', $EndDate) }
    Write-QdfProgress '[QDF export] Building SQLite database...'
    $sqliteStarted = Get-Date
    & $UvPath run --quiet --script $portableScript $extractDirectory $sqlite --format qdb --qdf-path $qdf @dateArguments
    if ($LASTEXITCODE -ne 0) { throw "SQLite materialization failed" }
    Write-QdfProgress ("[QDF export] SQLite database complete ({0:0.0}s)" -f ((Get-Date) - $sqliteStarted).TotalSeconds)

    Write-QdfProgress '[QDF export] Adding budget data...'
    $budgetStarted = Get-Date
    $budgetArguments = @(
        'run'
        '--quiet'
        '--script'
        $budgetScript
        (Join-Path $extractDirectory 'qdb-type-144.bin')
        (Join-Path $extractDirectory 'qdb-type-14b-full.bin')
        (Join-Path $extractDirectory 'qdb-type-080.bin')
        $extractDirectory
    )
    & $UvPath @budgetArguments
    if ($LASTEXITCODE -ne 0) { throw "Budget export failed" }
    Write-QdfProgress ("[QDF export] Budget data complete ({0:0.0}s)" -f ((Get-Date) - $budgetStarted).TotalSeconds)

    Write-QdfProgress ("[QDF export] Complete ({0:0.0}s): {1}" -f (((Get-Date) - $overallStarted).TotalSeconds), $sqlite)
}
finally {
    try {
        if (-not $KeepExtraction -and $ownsExtractionDirectory -and (Test-Path -LiteralPath $extractDirectory)) {
            Remove-Item -LiteralPath $extractDirectory -Recurse -Force
        }
    }
    finally {
        if ($null -ne $extractionLock) {
            $extractionLock.Dispose()
        }
    }
}
