#requires -Version 5.1
<#
.SYNOPSIS
Installs OrdCast as a self-contained scheduled task for the current Windows user.

.DESCRIPTION
Creates a fresh, versioned collector and GPU runtime under LocalAppData, records
weather data to a user-selectable local drive, and registers a six-hourly task that
runs with the current user's normal privileges. Existing weather data and installed
runtime versions are never removed or overwritten.
#>

[CmdletBinding()]
param(
    [string]$DataRoot = "",
    [string]$TaskName = "",
    [switch]$SkipGpu,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Resolve-Python311 {
    $launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        & $launcher.Source -3.11 -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) and struct.calcsize('P') == 8 else 1)"
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ Executable = $launcher.Source; Prefix = @("-3.11") }
        }
    }

    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        & $python.Source -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) and struct.calcsize('P') == 8 else 1)"
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ Executable = $python.Source; Prefix = @() }
        }
    }
    throw "64-bit Python 3.11 was not found. Install Python 3.11 from python.org, then run this installer again."
}

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

$sourceRepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$runtimeRoot = Join-Path $env:LOCALAPPDATA "OrdCast"
$configurationPath = Join-Path $runtimeRoot "station.json"
$previousConfiguration = $null
if (Test-Path -PathType Leaf $configurationPath) {
    try {
        $candidateConfiguration = Get-Content -Raw -Path $configurationPath | ConvertFrom-Json
        if ($candidateConfiguration.schema_version -eq 1) {
            $previousConfiguration = $candidateConfiguration
        }
    }
    catch {
        Write-Warning "The existing OrdCast configuration is unreadable; installation defaults will be used."
    }
}

$defaultDataRoot = Join-Path $env:USERPROFILE "OrdCastData"
if (
    $null -ne $previousConfiguration -and
    $null -ne $previousConfiguration.data_root
) {
    $defaultDataRoot = [string]$previousConfiguration.data_root
}
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $chosenDataRoot = Read-Host "Weather data folder [$defaultDataRoot]"
    if ([string]::IsNullOrWhiteSpace($chosenDataRoot)) {
        $DataRoot = $defaultDataRoot
    }
    else {
        $DataRoot = $chosenDataRoot.Trim()
    }
}
if ([string]::IsNullOrWhiteSpace($TaskName)) {
    if (
        $null -ne $previousConfiguration -and
        $null -ne $previousConfiguration.task_name
    ) {
        $TaskName = [string]$previousConfiguration.task_name
    }
    else {
        $TaskName = "OrdCast Local Weather Station"
    }
}

$dataFullPath = [IO.Path]::GetFullPath($DataRoot)
if ($dataFullPath.StartsWith("\\")) {
    throw "DataRoot must be a local disk path, not a network share."
}
$driveRoot = [IO.Path]::GetPathRoot($dataFullPath)
if ([string]::IsNullOrWhiteSpace($driveRoot)) {
    throw "DataRoot must be an absolute local path."
}
if ($dataFullPath.TrimEnd("\") -eq $driveRoot.TrimEnd("\")) {
    throw "Choose a folder on the drive (for example D:\OrdCastData), not the drive root itself."
}

$versionsRoot = Join-Path $runtimeRoot "versions"
$versionId = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ") + "-" + [Guid]::NewGuid().ToString("N")
$versionRoot = Join-Path $versionsRoot $versionId
$applicationRoot = Join-Path $versionRoot "app"
$collectorEnvironment = Join-Path $versionRoot "collector"
$gpuEnvironment = Join-Path $versionRoot "gpu"
$archiveRoot = Join-Path $dataFullPath "archive"
$gpuStatusPath = Join-Path $dataFullPath "state\gpu-status.json"
$python311 = Resolve-Python311

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask) {
    if ($existingTask.State -eq "Running") {
        throw "OrdCast is collecting data now. Wait for that cycle to finish, then run the installer again."
    }
}

$registeredNewTask = $false
$taskSwapStarted = $false
$restoreExistingTask = $false
try {
    New-Item -ItemType Directory -Force -Path $runtimeRoot, $versionsRoot, $archiveRoot, (Split-Path $gpuStatusPath -Parent) | Out-Null
    New-Item -ItemType Directory -Path $versionRoot, $applicationRoot | Out-Null

    $runtimeItems = @(
        "ordcast",
        "pipelines",
        "requirements.txt",
        "requirements.lock.txt",
        "requirements-windows-gpu.txt"
    )
    foreach ($item in $runtimeItems) {
        $source = Join-Path $sourceRepositoryRoot $item
        if (-not (Test-Path $source)) {
            throw "Required OrdCast runtime item is missing: $source"
        }
        Copy-Item -Path $source -Destination $applicationRoot -Recurse
    }
    $applicationOps = Join-Path $applicationRoot "ops"
    New-Item -ItemType Directory -Path $applicationOps | Out-Null
    Copy-Item -Path (Join-Path $sourceRepositoryRoot "ops\windows\*") -Destination $applicationOps -Recurse

    $driveName = $driveRoot.TrimEnd("\").TrimEnd(":")
    $drive = Get-PSDrive -Name $driveName -ErrorAction Stop
    $recommendedFreeBytes = 250GB
    if ($drive.Free -lt $recommendedFreeBytes) {
        Write-Warning ("Only {0:N1} GB is free on {1}. A full year of raw local evidence may use roughly 150-250 GB; a 500 GB or larger data drive is recommended." -f ($drive.Free / 1GB), $driveRoot)
    }

    Write-Host "Creating a fresh isolated OrdCast collector..." -ForegroundColor Cyan
    & $python311.Executable @($python311.Prefix) -m venv $collectorEnvironment
    Assert-NativeSuccess "Collector environment creation"
    $collectorPython = Join-Path $collectorEnvironment "Scripts\python.exe"
    & $collectorPython -m pip install --upgrade pip
    Assert-NativeSuccess "Collector pip update"
    & $collectorPython -m pip install -c (Join-Path $applicationRoot "requirements.lock.txt") -r (Join-Path $applicationRoot "requirements.txt")
    Assert-NativeSuccess "Collector dependency installation"

    $gpuPython = $null
    if (-not $SkipGpu) {
        Write-Host "Creating a fresh isolated DirectML GPU engine..." -ForegroundColor Cyan
        & $python311.Executable @($python311.Prefix) -m venv $gpuEnvironment
        Assert-NativeSuccess "GPU environment creation"
        $gpuPython = Join-Path $gpuEnvironment "Scripts\python.exe"
        & $gpuPython -m pip install --upgrade pip
        Assert-NativeSuccess "GPU pip update"
        & $gpuPython -m pip install -r (Join-Path $applicationRoot "requirements-windows-gpu.txt")
        Assert-NativeSuccess "DirectML dependency installation"

        Push-Location $applicationRoot
        try {
            & $gpuPython -m pipelines.local_gpu --output $gpuStatusPath
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "The GPU arithmetic check did not pass. Collection can still run; install the current AMD driver and Microsoft Visual C++ runtime, reboot, then run ORDCAST-STATUS.cmd."
            }
        }
        finally {
            Pop-Location
        }
    }

    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $currentUserSid = $currentIdentity.User.Value
    $runScript = Join-Path $applicationRoot "ops\Run-OrdCastCycle.ps1"
    $quote = [char]34
    $actionArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File $quote$runScript$quote -ApplicationRoot $quote$applicationRoot$quote -ArchiveRoot $quote$archiveRoot$quote -PythonExe $quote$collectorPython$quote"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArguments -WorkingDirectory $applicationRoot
    $firstRun = (Get-Date).AddMinutes(5)
    $repeatingTrigger = New-ScheduledTaskTrigger -Once -At $firstRun -RepetitionInterval (New-TimeSpan -Hours 6)
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentIdentity.Name
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $currentUserSid -LogonType Interactive -RunLevel Limited
    $settingArguments = @{
        MultipleInstances = "IgnoreNew"
        ExecutionTimeLimit = (New-TimeSpan -Hours 2)
        StartWhenAvailable = $true
        AllowStartIfOnBatteries = $true
        DontStopIfGoingOnBatteries = $true
    }
    $settings = New-ScheduledTaskSettingsSet @settingArguments
    $settings.WakeToRun = $true
    $registration = @{
        TaskName = $TaskName
        Action = $action
        Trigger = @($logonTrigger, $repeatingTrigger)
        Principal = $taskPrincipal
        Settings = $settings
        Description = "Collects immutable Kununurra observations, GEFS ensembles, and Himawari imagery to local disk for OrdCast."
    }
    # Staging above is intentionally non-disruptive. Recheck immediately before the
    # short task/configuration swap so an update cannot modify a running cycle.
    $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $existingTask -and $existingTask.State -eq "Running") {
        throw "OrdCast started collecting while the update was prepared. Wait for it to finish, then run the installer again. The prepared version was preserved."
    }
    if ($null -ne $existingTask -and $existingTask.State -ne "Disabled") {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        $restoreExistingTask = $true
    }
    $taskSwapStarted = $true
    Register-ScheduledTask @registration -Force | Out-Null
    $registeredNewTask = $true

    $configuration = [ordered]@{
        schema_version = 1
        version_root = $versionRoot
        application_root = $applicationRoot
        data_root = $dataFullPath
        archive_root = $archiveRoot
        collector_python = $collectorPython
        gpu_python = $gpuPython
        gpu_status = $gpuStatusPath
        task_name = $TaskName
        installed_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    $configurationTemporary = Join-Path $runtimeRoot ("station-" + [Guid]::NewGuid().ToString("N") + ".tmp")
    $configuration | ConvertTo-Json | Set-Content -Path $configurationTemporary -Encoding UTF8
    Move-Item -Path $configurationTemporary -Destination $configurationPath -Force

    Copy-Item -Path (Join-Path $sourceRepositoryRoot "ops\windows\Show-OrdCastStatus.ps1") -Destination $runtimeRoot -Force
    Copy-Item -Path (Join-Path $sourceRepositoryRoot "ops\windows\ORDCAST-STATUS.cmd") -Destination $runtimeRoot -Force
    Copy-Item -Path (Join-Path $sourceRepositoryRoot "ops\windows\Uninstall-OrdCastLocal.ps1") -Destination $runtimeRoot -Force

    if (-not $NoStart) {
        Start-ScheduledTask -TaskName $TaskName
    }
}
catch {
    if (
        $taskSwapStarted -and
        -not $registeredNewTask -and
        $restoreExistingTask
    ) {
        Enable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
    }
    throw
}

Write-Host ""
Write-Host "OrdCast local station is installed for $($currentIdentity.Name)." -ForegroundColor Green
Write-Host "Data: $archiveRoot"
Write-Host "Runtime: $versionRoot"
Write-Host "Schedule: after sign-in and every six hours while this user is signed in"
Write-Host "Administrator or cloud account: not required"
Write-Host "Status shortcut: $runtimeRoot\ORDCAST-STATUS.cmd"
Write-Host "Keep this PC awake, signed in, and connected to the internet during collection."
