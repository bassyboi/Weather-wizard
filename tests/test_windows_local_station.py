from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "ops" / "windows"


def test_installer_is_local_only_and_runs_as_limited_current_user():
    installer = (WINDOWS / "Install-OrdCastLocal.ps1").read_text(encoding="utf-8")

    assert "LOCALAPPDATA" in installer
    assert "versions" in installer
    assert "[Guid]::NewGuid()" in installer
    assert "LogonType Interactive" in installer
    assert "RunLevel Limited" in installer
    assert "WindowsIdentity]::GetCurrent" in installer
    assert "New-TimeSpan -Hours 6" in installer
    assert "requirements-windows-gpu.txt" in installer
    assert "pipelines.local_gpu" in installer
    assert "application_root" in installer
    assert "Disable-ScheduledTask" in installer
    assert 'State -eq "Running"' in installer
    assert "previousConfiguration.data_root" in installer
    assert "previousConfiguration.task_name" in installer
    assert installer.index("-m venv $gpuEnvironment") < installer.index("Disable-ScheduledTask")
    assert "-m venv $collectorEnvironment" in installer
    assert "-m venv $gpuEnvironment" in installer
    assert 'UserId "SYSTEM"' not in installer
    assert "ServiceAccount" not in installer
    assert "RunAs" not in installer
    assert "ProgramData" not in installer
    assert "RestartCount" not in installer
    assert "RestartInterval" not in installer
    assert "AWS_" not in installer
    assert "Cloudflare" not in installer


def test_uninstaller_preserves_weather_data():
    uninstaller = (WINDOWS / "Uninstall-OrdCastLocal.ps1").read_text(encoding="utf-8")

    assert "Unregister-ScheduledTask" in uninstaller
    assert "Nothing was deleted" in uninstaller
    assert "Remove-Item" not in uninstaller
    assert "RunAs" not in uninstaller
    assert "LOCALAPPDATA" in uninstaller


def test_scheduled_cycle_uses_local_archive_and_bounded_recorder():
    runner = (WINDOWS / "Run-OrdCastCycle.ps1").read_text(encoding="utf-8")

    assert "pipelines.local_station" in runner
    assert "ApplicationRoot" in runner
    assert "RepositoryRoot" not in runner
    assert "--deadline-seconds" in runner
    assert "--min-success-ratio" in runner
    assert '"1.0"' in runner
    assert "aws " not in runner.lower()
    assert "r2" not in runner.lower()


def test_gpu_dependencies_are_windows_only_and_pinned():
    requirements = (ROOT / "requirements-windows-gpu.txt").read_text(encoding="utf-8")

    assert "torch-directml==0.2.5.dev240914" in requirements
    assert "onnxruntime-directml==1.24.4" in requirements
    assert "onnx==1.22.0" in requirements
    assert "numpy==1.26.4" in requirements
    assert requirements.count('platform_system == "Windows"') == 4
