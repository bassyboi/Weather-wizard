# OrdCast on one Windows PC

This is the supported starter deployment for the farm: one ordinary Windows desktop,
one local data drive, and the Radeon GPU already in the machine. AWS, Cloudflare, and
another always-on server are **not required**.

## What runs where

| Work | Hardware | Why |
|---|---|---|
| Download Bureau, GEFS, and Himawari data | CPU + internet | Network and file work does not benefit from a GPU |
| Validate, checksum, and archive source bytes | CPU + local disk | This must remain exact and auditable |
| Train local bias and storm models | RX 6600 XT through DirectML | This is the heavy matrix/image work |
| Run trained forecasts | RX 6600 XT through ONNX/DirectML | Low-latency local prediction |
| Display the future farm dashboard | CPU + browser | The dashboard is lightweight |

“Fully GPU” should mean that every expensive model operation uses the GPU. Moving
downloads and checksums to the GPU would add complexity without making the forecast
better or faster.

## Hardware and software

- Windows 10 version 1903 or newer, or Windows 11, 64 bit.
- Python 3.11, 64 bit. A normal per-user Python.org installation is supported.
- Microsoft Visual C++ 2019 runtime (or newer compatible runtime).
- Current AMD Radeon graphics driver.
- Radeon RX 6600 XT with 8 GB VRAM.
- At least 250 GB free; a dedicated 500 GB or larger SSD is recommended.
- A reliable internet connection.

The present live profile is expected to grow by roughly 150–250 GB per year, mostly
from ten-minute Himawari imagery. This is an operating estimate, not a storage cap.
The status command reports remaining disk space, and the archive never silently
deletes old training evidence.

## Install

1. Download or clone this repository, then extract it if it arrived as a ZIP. Do not
   run the installer from inside the temporary ZIP preview.
2. Install 64-bit Python 3.11 from Python.org for your normal Windows account.
3. Update the AMD graphics driver.
4. Double-click ops\windows\INSTALL-ORDCAST-LOCAL.cmd.
5. Choose a weather-data folder. D:\OrdCastData is preferable when a large second
   drive exists; otherwise accept the suggested OrdCastData folder in your profile.

Administrator approval is not required. OrdCast deliberately runs with the same
limited permissions as the Windows account that installs it.

The installer:

- creates a fresh versioned collector and GPU environment under
  %LOCALAPPDATA%\OrdCast on every install or update;
- installs a pinned top-level DirectML GPU stack;
- performs an actual GPU matrix calculation rather than merely detecting the card;
- creates an append-only archive on the selected data drive;
- registers **OrdCast Local Weather Station** as a Windows scheduled task;
- starts a collection immediately, then repeats every six hours and after sign-in;
- asks Windows to wake a sleeping PC while that account remains signed in.

Each network object has bounded retries inside one frozen collection plan. Windows does
not restart the entire cycle after one missing upstream object, which avoids repeating
thousands of successful downloads. The next scheduled cycle overlaps recent satellite
data and the status command keeps any unresolved continuity fault visible.

The first full collection can take 20–45 minutes. The computer may be used normally
while it runs. It must have power, internet access, and that Windows account signed in;
a powered-off or signed-out PC cannot archive short-retention source data. A sign-in
cycle resumes current collection but cannot reconstruct satellite scans that disappeared
while the PC was unavailable. Windows wake timers must also be enabled in the active
power plan for sleep wake-up to work.

## Check it

Double-click `%LOCALAPPDATA%\OrdCast\ORDCAST-STATUS.cmd`. The installer copies this
stable shortcut so the downloaded repository can be moved or deleted afterward.

It reports:

- whether the newest collection is healthy, incomplete, or stale;
- the most recent run time and artifact count;
- remaining disk space;
- whether real DirectML training arithmetic passed;
- whether ONNX Runtime can use the DirectML prediction provider;
- the scheduled task's next run and last result.

Before the first cycle finishes, NOT_STARTED is expected. A later STALE or DEGRADED
state means the logs under DataRoot\logs need attention. A gap longer than seven hours
is marked DEGRADED even if the newest cycle succeeds, because missed satellite scans
need an explicit backfill.

## GPU boundary

The RX 6600 XT is not listed in AMD's current official Windows ROCm/PyTorch support
matrix, which now targets selected Radeon 7000/9000 cards. OrdCast therefore does not
pretend that ROCm is supported on this card.

For this PC:

- training uses Microsoft's torch-directml DirectX 12 backend in a separate Python
  environment;
- prediction uses onnxruntime-directml, with a planned move to Windows ML when the
  first trained model is exported;
- CPU fallback is reported as a fault for heavy model work rather than silently
  accepted.

torch-directml is on an older PyTorch stack, and DirectML itself is now in
sustained-engineering mode. That is why it is isolated from
the recorder. The installer proves basic DirectML arithmetic; it does not yet prove
that every future storm-model operator stays on the GPU. Each trained model must pass
a full forward/backward and provider-only inference test on the actual RX 6600 XT
before a season-long shadow run.

The DirectML package pins PyTorch 2.4.1. PyTorch's official security advisory says
versions through 2.5.1 can execute code when loading an untrusted model with
`torch.load`. OrdCast must never load downloaded or third-party .pt/.pth checkpoint
files in this environment. Use only locally produced model weights, and deploy reviewed
models as ONNX. See [PyTorch advisory GHSA-53q9-r3pm-6pq6](https://github.com/pytorch/pytorch/security/advisories/GHSA-53q9-r3pm-6pq6).

Local execution does not mean zero telemetry: Microsoft's torch-directml package says
it may collect GPU identity and unsupported-operator fallback data. Review the
[torch-directml telemetry notice](https://pypi.org/project/torch-directml/) before
installing the optional GPU environment.

Primary compatibility references, checked 12 August 2026:

- [Microsoft: PyTorch with DirectML on Windows](https://learn.microsoft.com/en-us/windows/ai/directml/pytorch-windows)
- [AMD: current Windows Radeon ROCm support matrix](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/compatibility/compatibilityrad/windows/windows_compatibility.html)
- [ONNX Runtime: DirectML execution provider](https://onnxruntime.ai/docs/execution-providers/DirectML-ExecutionProvider.html)

## Safe removal and backup

`%LOCALAPPDATA%\OrdCast\Uninstall-OrdCastLocal.ps1` removes only the scheduled task.
It deliberately preserves all weather data, logs, settings, and versioned Python
environments. Updates also preserve older runtimes so a failed installation cannot
destroy the last working copy; cleanup will be added only after rollback is automated.

Local-only operation removes cloud dependence but makes the PC and its disk the single
copy. Once collection is stable, use a second external drive for an append-only backup.
Do not copy raw Bureau feed data to a public repository; its public-feed terms restrict
use and redistribution.

## What comes next

This installer makes the evidence collector and GPU runtime local. It does not turn the
current research scaffold into a trained forecast by itself. The next code milestone is:

1. decode each locally archived GEFS and Himawari file into aligned training arrays;
2. issue a raw GEFS Kununurra benchmark forecast;
3. train an RX-6600-XT bias-correction model and compare it with the raw ensemble;
4. export the winner to ONNX/DirectML;
5. display a local shadow forecast with rain probabilities and farm-relevant windows.
