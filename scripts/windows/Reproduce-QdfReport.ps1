[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [ValidateNotNullOrEmpty()] [string]$Database,
    [Parameter(Mandatory = $true)] [ValidateNotNullOrEmpty()] [string]$Output,
    [string]$Report = 'Grocery expenses',
    [string]$UvPath = 'uv'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) {
    throw "SQLite export not found: $Database"
}
if (-not (Get-Command $UvPath -ErrorAction SilentlyContinue)) {
    throw "uv executable not found: $UvPath"
}

$scriptPath = Join-Path $PSScriptRoot '..\reproduce_qdb_report.py'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "report reproduction script not found: $scriptPath"
}

& $UvPath run --quiet --script $scriptPath $Database $Output --report $Report
if ($LASTEXITCODE -ne 0) {
    throw "Report reproduction failed"
}
