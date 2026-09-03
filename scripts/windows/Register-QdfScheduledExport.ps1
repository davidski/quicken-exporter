[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [string]$TaskName = 'Daily QDF SQLite Export',
    [datetime]$At = '02:00',
    [switch]$RunAsSystem
)

$ErrorActionPreference = 'Stop'
$runner = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'Invoke-QdfScheduledExport.ps1')).Path
$config = (Resolve-Path -LiteralPath $ConfigPath).Path
$powerShell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -ConfigPath "{1}"' -f $runner, $config
$action = New-ScheduledTaskAction -Execute $powerShell -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$description = 'Runs the daily read-only Quicken QDF export and publishes the completed SQLite database to the configured destination.'
$settings = New-ScheduledTaskSettingsSet -Compatibility Win10 -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 15)

if ($RunAsSystem) {
    Write-Warning 'LocalSystem normally cannot access network shares. Use only when the destination explicitly permits the computer account.'
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description $description -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
}
else {
    $credential = Get-Credential -Message 'Account that can read the QDF and write to the UNC destination'
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description $description -User $credential.UserName `
        -Password $credential.GetNetworkCredential().Password -RunLevel Highest -Force | Out-Null
}

Write-Output "Registered scheduled task: $TaskName"
Write-Output "Test it with: Start-ScheduledTask -TaskName '$TaskName'"
