#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$TaskName = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$configurationPath = Join-Path $env:LOCALAPPDATA "OrdCast\station.json"
if ([string]::IsNullOrWhiteSpace($TaskName) -and (Test-Path -PathType Leaf $configurationPath)) {
    $configuration = Get-Content -Raw -Path $configurationPath | ConvertFrom-Json
    if ($null -ne $configuration.task_name) {
        $TaskName = [string]$configuration.task_name
    }
}
if ([string]::IsNullOrWhiteSpace($TaskName)) {
    $TaskName = "OrdCast Local Weather Station"
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
    if ($task.State -eq "Running") {
        throw "OrdCast is collecting data now. Wait for that cycle to finish, then uninstall it."
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed the OrdCast scheduled task." -ForegroundColor Green
}
else {
    Write-Host "No OrdCast scheduled task was found."
}

Write-Host "Weather archives, logs, configuration, and versioned Python environments were preserved."
Write-Host "Nothing was deleted."
