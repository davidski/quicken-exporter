[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [switch]$KeepExtraction
)

$ErrorActionPreference = 'Stop'
$configFile = (Resolve-Path -LiteralPath $ConfigPath).Path
$config = Import-PowerShellDataFile -LiteralPath $configFile

# PowerShell does not expand cmd.exe-style %NAME% tokens in ordinary strings.
# Expand configured filesystem paths explicitly so the example can use the
# standard machine-wide data location without hard-coding its drive/path.
$pathConfigurationNames = @(
    'QdfPath', 'QuickenDirectory', 'UvPath', 'DestinationDirectory',
    'StagingDirectory', 'LogDirectory', 'DatafilePasswordSecretPath'
)
foreach ($name in $pathConfigurationNames) {
    if ($config.ContainsKey($name) -and $null -ne $config[$name]) {
        $config[$name] = [Environment]::ExpandEnvironmentVariables([string]$config[$name])
    }
}

$required = @(
    'QdfPath', 'QuickenDirectory', 'UvPath', 'DestinationDirectory',
    'OutputFilePattern', 'StagingDirectory', 'LogDirectory'
)
foreach ($name in $required) {
    if (-not $config.ContainsKey($name) -or [string]::IsNullOrWhiteSpace($config[$name])) {
        throw "Missing required configuration value: $name"
    }
}

$datafilePassword = $null
if ($config.ContainsKey('DatafilePasswordSecretPath') -and
    -not [string]::IsNullOrWhiteSpace($config.DatafilePasswordSecretPath)) {
    $secretPath = (Resolve-Path -LiteralPath $config.DatafilePasswordSecretPath).Path
    if (-not (Test-Path -LiteralPath $secretPath -PathType Leaf)) {
        throw "Data-file password secret not found: $secretPath"
    }
    $encryptedPassword = (Get-Content -LiteralPath $secretPath -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($encryptedPassword)) {
        throw "Data-file password secret is empty: $secretPath"
    }
    try {
        $datafilePassword = ConvertTo-SecureString -String $encryptedPassword
    }
    catch {
        throw "Could not decrypt the data-file password secret for the current task account: $secretPath"
    }
}

$wrapper = Join-Path $PSScriptRoot 'Export-QdfFinancial.ps1'
if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) { throw "Exporter not found: $wrapper" }
if (-not (Test-Path -LiteralPath $config.QdfPath -PathType Leaf)) { throw "QDF not found: $($config.QdfPath)" }
if (-not (Test-Path -LiteralPath $config.QuickenDirectory -PathType Container)) {
    throw "Quicken directory not found: $($config.QuickenDirectory)"
}
if (-not (Test-Path -LiteralPath $config.UvPath -PathType Leaf)) { throw "uv not found: $($config.UvPath)" }

$runId = '{0:yyyyMMdd-HHmmss}-{1}' -f (Get-Date), $PID
$runDirectory = Join-Path $config.StagingDirectory $runId
$localSqlite = Join-Path $runDirectory 'quicken.sqlite'
$logFile = Join-Path $config.LogDirectory ("qdf-export-$runId.log")
$lockFile = Join-Path $config.StagingDirectory 'scheduled-export.lock'

New-Item -ItemType Directory -Force -Path $config.StagingDirectory | Out-Null
New-Item -ItemType Directory -Force -Path $config.LogDirectory | Out-Null
New-Item -ItemType Directory -Force -Path $runDirectory | Out-Null

$lock = $null
$transcriptStarted = $false
$temporaryDestination = $null
$runStarted = $null
$runSucceeded = $false

function Format-QdfTimestamp {
    param(
        [Parameter(Mandatory = $true)]
        [datetime]$Timestamp
    )

    return $Timestamp.ToString(
        'ddd, MMM d, yyyy h:mm:ss tt zzz',
        [Globalization.CultureInfo]::InvariantCulture
    )
}

function Format-QdfDuration {
    param(
        [Parameter(Mandatory = $true)]
        [timespan]$Duration
    )

    if ($Duration.TotalHours -ge 1) {
        return '{0}h {1}m {2:0.0}s' -f [math]::Floor($Duration.TotalHours), $Duration.Minutes, $Duration.Seconds + ($Duration.Milliseconds / 1000)
    }
    if ($Duration.TotalMinutes -ge 1) {
        return '{0}m {1:0.0}s' -f $Duration.Minutes, $Duration.Seconds + ($Duration.Milliseconds / 1000)
    }
    return '{0:0.0}s' -f $Duration.TotalSeconds
}

try {
    try {
        $lock = [IO.File]::Open($lockFile, 'OpenOrCreate', 'ReadWrite', 'None')
    }
    catch {
        throw 'Another QDF export appears to be running (could not acquire the export lock).'
    }

    Start-Transcript -LiteralPath $logFile -Force | Out-Null
    $transcriptStarted = $true
    $runStarted = Get-Date
    Write-Output ''
    Write-Output '=== QDF export run ==='
    Write-Output "Run ID: $runId"
    Write-Output "Start time (local): $(Format-QdfTimestamp $runStarted)"
    Write-Output "Source QDF: $($config.QdfPath)"
    Write-Output "Staging SQLite: $localSqlite"
    Write-Output ''

    # Use named-parameter splatting for the PowerShell wrapper. Expanding a
    # string array of parameter names and values passes them positionally in
    # Windows PowerShell 5.1, which can bind '-OutputSqlite' to DatafilePassword.
    $exportParameters = @{
        QdfPath           = $config.QdfPath
        OutputSqlite      = $localSqlite
        QuickenDirectory  = $config.QuickenDirectory
        UvPath             = $config.UvPath
    }
    if ($KeepExtraction) {
        $exportParameters.KeepExtraction = $true
    }
    if ($null -ne $datafilePassword) {
        $exportParameters.DatafilePassword = $datafilePassword
    }
    & $wrapper @exportParameters
    if ($LASTEXITCODE -ne 0) { throw "Exporter returned exit code $LASTEXITCODE" }
    if (-not (Test-Path -LiteralPath $localSqlite -PathType Leaf)) { throw 'Exporter produced no SQLite file' }
    if ((Get-Item -LiteralPath $localSqlite).Length -eq 0) { throw 'Exporter produced an empty SQLite file' }

    New-Item -ItemType Directory -Force -Path $config.DestinationDirectory | Out-Null
    $fileName = $config.OutputFilePattern -f (Get-Date)
    if ([IO.Path]::GetFileName($fileName) -ne $fileName) {
        throw 'OutputFilePattern must produce a file name, not a path'
    }
    $destination = Join-Path $config.DestinationDirectory $fileName
    $replace = $config.ContainsKey('ReplaceExisting') -and [bool]$config.ReplaceExisting
    if ((Test-Path -LiteralPath $destination) -and -not $replace) {
        throw "Destination already exists and ReplaceExisting is false: $destination"
    }

    # Copy to a same-directory temporary name, then rename. Consumers never see
    # a partially copied database on the network share.
    $temporaryDestination = "$destination.partial-$runId"
    Copy-Item -LiteralPath $localSqlite -Destination $temporaryDestination
    if ((Get-Item -LiteralPath $temporaryDestination).Length -ne (Get-Item -LiteralPath $localSqlite).Length) {
        throw 'Network copy size verification failed'
    }
    if (Test-Path -LiteralPath $destination) {
        # Both files are in the destination directory, so File.Replace performs
        # a same-volume atomic replacement instead of exposing a missing or
        # partially copied database to readers.
        # Use NullString so Windows PowerShell 5.1 passes a true null to the
        # .NET string parameter instead of coercing it to an empty path.
        [IO.File]::Replace($temporaryDestination, $destination, [NullString]::Value)
    }
    else {
        [IO.File]::Move($temporaryDestination, $destination)
    }
    $publishedSize = (Get-Item -LiteralPath $destination).Length
    Write-Output ("Published SQLite export: {0} ({1:N0} bytes)" -f $destination, $publishedSize)
    $runSucceeded = $true
}
catch {
    Write-Error $_
    exit 1
}
finally {
    if ($transcriptStarted) {
        $runEnded = Get-Date
        $runStatus = if ($runSucceeded) { 'Succeeded' } else { 'Failed' }
        Write-Output ''
        Write-Output "End time (local): $(Format-QdfTimestamp $runEnded)"
        Write-Output "Elapsed: $(Format-QdfDuration ($runEnded - $runStarted))"
        Write-Output "Status: $runStatus"
        Stop-Transcript | Out-Null
    }
    if ($null -ne $lock) { $lock.Dispose() }
    if ($null -ne $temporaryDestination -and (Test-Path -LiteralPath $temporaryDestination)) {
        Remove-Item -LiteralPath $temporaryDestination -Force
    }
    if (-not $KeepExtraction -and (Test-Path -LiteralPath $runDirectory)) {
        Remove-Item -LiteralPath $runDirectory -Recurse -Force
    }
}
