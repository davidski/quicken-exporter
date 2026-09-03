[CmdletBinding()]
param(
    [string]$OutputPath = '.\output\qdf-export-bundle.zip',
    [string]$CompilerPath
)

$ErrorActionPreference = 'Stop'

function Resolve-QdfOutputPath {
    param(
        [Parameter(Mandatory = $true)] [string]$Path
    )

    # Resolve relative paths against PowerShell's current location. .NET's
    # Path.GetFullPath uses the process working directory, which can differ
    # from the location shown by the PowerShell prompt.
    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
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
$destination = Resolve-QdfOutputPath $OutputPath
$destinationDirectory = [IO.Path]::GetDirectoryName($destination)
$stagingRoot = Join-Path ([IO.Path]::GetTempPath()) ('qdf-export-bundle-' + [guid]::NewGuid().ToString('N'))
$moduleRoot = Join-Path $stagingRoot 'QdfExport'
$compiledDirectory = Join-Path $moduleRoot 'bin'

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

# The archive is a self-contained PowerShell module directory. Keep the
# internal scripts/src layout because the wrapper resolves both paths relative
# to the module root.
$bundleFiles = @(
    @{ Source = 'LICENSE'; Target = 'LICENSE' },
    @{ Source = 'QdfExport\QdfExport.psd1'; Target = 'QdfExport.psd1' },
    @{ Source = 'QdfExport\QdfExport.psm1'; Target = 'QdfExport.psm1' },
    @{ Source = 'scripts\export_financial_sqlite.py'; Target = 'scripts\export_financial_sqlite.py' },
    @{ Source = 'scripts\export_qhi_sqlite.py'; Target = 'scripts\export_qhi_sqlite.py' },
    @{ Source = 'scripts\export_qdb_budgets.py'; Target = 'scripts\export_qdb_budgets.py' },
    @{ Source = 'scripts\reproduce_qdb_report.py'; Target = 'scripts\reproduce_qdb_report.py' },
    @{ Source = 'scripts\windows\Export-QdfFinancial.ps1'; Target = 'scripts\windows\Export-QdfFinancial.ps1' },
    @{ Source = 'scripts\windows\Reproduce-QdfReport.ps1'; Target = 'scripts\windows\Reproduce-QdfReport.ps1' },
    @{ Source = 'scripts\windows\Invoke-QdfScheduledExport.ps1'; Target = 'scripts\windows\Invoke-QdfScheduledExport.ps1' },
    @{ Source = 'scripts\windows\Register-QdfScheduledExport.ps1'; Target = 'scripts\windows\Register-QdfScheduledExport.ps1' },
    @{ Source = 'scripts\windows\QdfScheduledExport.config.psd1.example'; Target = 'scripts\windows\QdfScheduledExport.config.psd1.example' },
    @{ Source = 'src\qdf_tools\__init__.py'; Target = 'src\qdf_tools\__init__.py' },
    @{ Source = 'src\qdf_tools\qif.py'; Target = 'src\qdf_tools\qif.py' },
    @{ Source = 'src\qdf_tools\sqlite_export.py'; Target = 'src\qdf_tools\sqlite_export.py' },
    @{ Source = 'src\qdf_tools\qdb_financial.py'; Target = 'src\qdf_tools\qdb_financial.py' },
    @{ Source = 'src\qdf_tools\qph.py'; Target = 'src\qdf_tools\qph.py' },
    @{ Source = 'src\qdf_tools\qdb_budgets.py'; Target = 'src\qdf_tools\qdb_budgets.py' },
    @{ Source = 'src\qdf_tools\qdb_reports.py'; Target = 'src\qdf_tools\qdb_reports.py' },
    @{ Source = 'src\qdf_tools\report_reproduction.py'; Target = 'src\qdf_tools\report_reproduction.py' },
    @{ Source = 'src\qdf_tools\qhi_idb.py'; Target = 'src\qdf_tools\qhi_idb.py' }
)

function Compile-Extractor {
    param(
        [Parameter(Mandatory = $true)] [string]$Source,
        [Parameter(Mandatory = $true)] [string]$Output,
        [Parameter(Mandatory = $true)] [string]$Description
    )

    $passwordSource = Join-Path $exportRoot 'scripts\windows\QdbPassword.cs'
    $assemblyInfo = Join-Path $exportRoot 'scripts\windows\QdfExportAssemblyInfo.cs'
    & $CompilerPath /nologo /platform:x86 /target:exe /out:$Output $Source $passwordSource $assemblyInfo
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to compile the $Description"
    }
}

try {
    if ([string]::IsNullOrWhiteSpace($CompilerPath)) {
        $CompilerPath = Resolve-QdfCSharpCompiler
    }
    if (-not (Test-Path -LiteralPath $CompilerPath -PathType Leaf)) {
        throw "32-bit .NET Framework C# compiler not found: $CompilerPath"
    }
    $assemblyInfo = Join-Path $exportRoot 'scripts\windows\QdfExportAssemblyInfo.cs'
    if (-not (Test-Path -LiteralPath $assemblyInfo -PathType Leaf)) {
        throw "assembly metadata source not found: $assemblyInfo"
    }

    New-Item -ItemType Directory -Force -Path $destinationDirectory | Out-Null
    New-Item -ItemType Directory -Force -Path $moduleRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $compiledDirectory | Out-Null

    $sourceDirectory = Join-Path $exportRoot 'scripts\windows'
    Compile-Extractor (Join-Path $sourceDirectory 'Extract-QdbFinancial.cs') `
        (Join-Path $compiledDirectory 'Extract-QdbFinancial.exe') 'qdb financial extractor'
    Compile-Extractor (Join-Path $sourceDirectory 'Extract-QdbAccountMap.cs') `
        (Join-Path $compiledDirectory 'Extract-QdbAccountMap.exe') 'qdb account-index extractor'
    Compile-Extractor (Join-Path $sourceDirectory 'Extract-QdbReports.cs') `
        (Join-Path $compiledDirectory 'Extract-QdbReports.exe') 'qdb saved-report extractor'
    Compile-Extractor (Join-Path $sourceDirectory 'Extract-QdbVariableType.cs') `
        (Join-Path $compiledDirectory 'Extract-QdbVariableType.exe') 'qdb variable-type extractor'

    foreach ($file in $bundleFiles) {
        $source = Join-Path $exportRoot $file.Source
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Bundle source file not found: $source"
        }
        $target = Join-Path $moduleRoot $file.Target
        New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($target)) | Out-Null
        Copy-Item -LiteralPath $source -Destination $target -Force
    }

    @'
QDF export PowerShell module

The QdfExport directory is a self-contained PowerShell 5.1 module directory.
Import it from the extracted bundle with:

    Import-Module .\QdfExport\QdfExport.psd1

Then run:

    Export-QdfFinancial -QdfPath C:\path\input.QDF `
        -OutputSqlite C:\path\quicken.sqlite `
        -QuickenDirectory 'C:\Program Files (x86)\Quicken' `
        -UvPath C:\Tools\uv\uv.exe

The four x86 C# qdb.dll helpers are precompiled in QdfExport\bin. The target
machine does not need csc.exe. The exporter requires Windows PowerShell 5.1,
.NET Framework 4.x, and uv; the native extraction boundary additionally
assumes the matching x86 Quicken DLL set. The Python materializer remains
source-based and is run through uv run --script; it is included under
QdfExport\scripts and QdfExport\src.

The standalone Home Inventory exporter is also included as
QdfExport\scripts\export_qhi_sqlite.py and reads QHI.IDB without Quicken DLLs.

For scheduled exports, copy
QdfExport\scripts\windows\QdfScheduledExport.config.psd1.example, edit it,
and run Register-QdfScheduledExport from the imported module.

To reproduce a saved report from an existing SQLite export, run
Reproduce-QdfReport -Database C:\path\quicken.sqlite -Output C:\path\report.tsv
[-Report 'Grocery expenses']. This is a separate read-only post-processing
operation and does not open or modify the QDF.
'@ | Set-Content -LiteralPath (Join-Path $stagingRoot 'README.txt') -Encoding UTF8

    if (Test-Path -LiteralPath $destination) {
        Remove-Item -LiteralPath $destination -Force
    }
    Compress-Archive -Path (Join-Path $stagingRoot '*') -DestinationPath $destination -CompressionLevel Optimal -Force
    Write-Output "wrote precompiled QDF export module bundle: $destination"
}
finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
}
