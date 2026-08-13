#requires -Version 5.1

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$configurationPath = Join-Path $env:LOCALAPPDATA "OrdCast\station.json"
if (-not (Test-Path -PathType Leaf $configurationPath)) {
    throw "OrdCast is not installed on this PC. Run INSTALL-ORDCAST-LOCAL.cmd first."
}
$configuration = Get-Content -Raw -Path $configurationPath | ConvertFrom-Json
if ($configuration.schema_version -ne 1) {
    throw "Unsupported OrdCast local configuration version."
}

Push-Location $configuration.application_root
try {
    & $configuration.collector_python -m pipelines.local_station status --archive-root $configuration.archive_root
    $statusExitCode = $LASTEXITCODE
    Write-Host ""
    if ($null -ne $configuration.gpu_python -and (Test-Path -PathType Leaf $configuration.gpu_python)) {
        & $configuration.gpu_python -m pipelines.local_gpu
        $gpuExitCode = $LASTEXITCODE
    }
    else {
        Write-Host "OrdCast GPU: NOT INSTALLED"
        $gpuExitCode = 1
    }

    $task = Get-ScheduledTask -TaskName $configuration.task_name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "Scheduled collection: MISSING" -ForegroundColor Red
        $taskExitCode = 1
    }
    else {
        $taskInfo = Get-ScheduledTaskInfo -TaskName $configuration.task_name
        Write-Host "Scheduled collection: $($task.State)"
        Write-Host "Next run: $($taskInfo.NextRunTime)"
        Write-Host "Last task result: $($taskInfo.LastTaskResult)"
        # 0x41301 = running and 0x41303 = not yet run; neither is a task failure.
        $informationalResults = @(0, 0x41301, 0x41303)
        if (
            $task.State -eq "Disabled" -or
            (
                $taskInfo.LastTaskResult -notin $informationalResults -and
                $task.State -ne "Running"
            )
        ) {
            Write-Host "Scheduled collection needs attention." -ForegroundColor Red
            $taskExitCode = 1
        }
        else {
            $taskExitCode = 0
        }
    }
}
finally {
    Pop-Location
}

if ($statusExitCode -ne 0 -or $gpuExitCode -ne 0 -or $taskExitCode -ne 0) {
    exit 1
}
exit 0
