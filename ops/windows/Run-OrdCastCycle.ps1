#requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ApplicationRoot,
    [Parameter(Mandatory = $true)][string]$ArchiveRoot,
    [Parameter(Mandatory = $true)][string]$PythonExe
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not (Test-Path -PathType Container $ApplicationRoot)) {
    throw "OrdCast application runtime is missing: $ApplicationRoot"
}
if (-not (Test-Path -PathType Leaf $PythonExe)) {
    throw "OrdCast collector Python is missing: $PythonExe"
}

$dataRoot = Split-Path $ArchiveRoot -Parent
$logDirectory = Join-Path $dataRoot "logs"
New-Item -ItemType Directory -Force -Path $logDirectory, $ArchiveRoot | Out-Null
$stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$logPath = Join-Path $logDirectory "cycle-$stamp.log"
$cycleArguments = @(
    "-m", "pipelines.local_station", "cycle",
    "--archive-root", $ArchiveRoot,
    "--workers", "4",
    "--deadline-seconds", "2700",
    "--min-success-ratio", "1.0"
)

Push-Location $ApplicationRoot
try {
    "[$([DateTime]::UtcNow.ToString('o'))] Starting OrdCast local cycle" | Tee-Object -FilePath $logPath
    & $PythonExe @cycleArguments 2>&1 | Tee-Object -FilePath $logPath -Append
    $cycleExitCode = $LASTEXITCODE
    "[$([DateTime]::UtcNow.ToString('o'))] Cycle exit code: $cycleExitCode" | Tee-Object -FilePath $logPath -Append
}
finally {
    Pop-Location
}

exit $cycleExitCode
