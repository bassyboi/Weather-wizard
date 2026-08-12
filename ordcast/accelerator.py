"""Optional local GPU capability checks without making Torch a core dependency."""

from __future__ import annotations

import importlib
import math
import platform
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import ModuleType
from typing import Callable


UTC = timezone.utc


@dataclass(frozen=True)
class AcceleratorStatus:
    """Observed training and inference capability for the current Python environment."""

    training_backend: str
    training_ready: bool
    device_name: str | None
    torch_version: str | None
    arithmetic_check_ms: float | None
    inference_backend: str
    inference_ready: bool
    onnxruntime_version: str | None
    inference_providers: tuple[str, ...]
    checked_at: datetime
    notes: tuple[str, ...]

    @property
    def fully_ready(self) -> bool:
        return self.training_ready and self.inference_ready

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "training_backend": self.training_backend,
            "training_ready": self.training_ready,
            "device_name": self.device_name,
            "torch_version": self.torch_version,
            "arithmetic_check_ms": self.arithmetic_check_ms,
            "inference_backend": self.inference_backend,
            "inference_ready": self.inference_ready,
            "onnxruntime_version": self.onnxruntime_version,
            "inference_providers": list(self.inference_providers),
            "checked_at": self.checked_at.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "fully_ready": self.fully_ready,
            "notes": list(self.notes),
        }


def _module_version(module: ModuleType) -> str | None:
    value = getattr(module, "__version__", None)
    return value if isinstance(value, str) and value else None


def _verify_torch_device(
    torch: ModuleType,
    device: object,
    *,
    monotonic: Callable[[], float],
) -> float:
    """Run a synchronizing matrix operation rather than trusting enumeration alone."""

    started = monotonic()
    matrix = torch.tensor([[1.0, 2.0], [3.0, 4.0]], dtype=torch.float32).to(device)
    expected_device = str(device).split(":", maxsplit=1)[0]
    actual_device = str(matrix.device).split(":", maxsplit=1)[0]
    if actual_device != expected_device:
        raise RuntimeError(
            f"tensor remained on {actual_device!r} instead of {expected_device!r}"
        )
    observed = float((matrix @ matrix).sum().item())
    elapsed_ms = (monotonic() - started) * 1_000.0
    if not math.isclose(observed, 54.0, rel_tol=0, abs_tol=1e-5):
        raise RuntimeError(f"GPU arithmetic check returned {observed}, expected 54")
    if not math.isfinite(elapsed_ms) or elapsed_ms < 0:
        raise RuntimeError("GPU arithmetic timer returned an invalid duration")
    return elapsed_ms


def _verify_onnx_provider(
    onnxruntime: ModuleType,
    provider: str,
    *,
    importer: Callable[[str], ModuleType],
    device_id: int = 0,
) -> None:
    """Initialize one GPU-only ONNX session and verify its numerical output."""

    onnx = importer("onnx")
    numpy = importer("numpy")
    helper = onnx.helper
    tensor_type = onnx.TensorProto.FLOAT
    graph = helper.make_graph(
        [helper.make_node("Mul", ["x", "scale"], ["result"])],
        "ordcast-gpu-smoke",
        [helper.make_tensor_value_info("x", tensor_type, [1, 2])],
        [helper.make_tensor_value_info("result", tensor_type, [1, 2])],
        initializer=[
            helper.make_tensor("scale", tensor_type, [1], [2.0])
        ],
    )
    model = helper.make_model(
        graph,
        producer_name="ordcast",
        opset_imports=[helper.make_opsetid("", 20)],
    )
    # IR 10 is supported by the targeted ORT and avoids depending on the newest IR.
    model.ir_version = 10
    options = onnxruntime.SessionOptions()
    options.enable_mem_pattern = False
    options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
    options.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
    session = onnxruntime.InferenceSession(
        model.SerializeToString(),
        sess_options=options,
        providers=[(provider, {"device_id": str(device_id)})],
    )
    if provider not in session.get_providers():
        raise RuntimeError(f"ONNX session did not activate {provider}")
    inputs = {"x": numpy.asarray([[1.0, 3.0]], dtype=numpy.float32)}
    outputs = session.run(["result"], inputs)
    if len(outputs) != 1 or not bool(
        numpy.allclose(outputs[0], [[2.0, 6.0]], rtol=0, atol=1e-6)
    ):
        raise RuntimeError("ONNX GPU arithmetic check returned an unexpected result")


def probe_accelerator(
    *,
    importer: Callable[[str], ModuleType] = importlib.import_module,
    system: str | None = None,
    now: datetime | None = None,
    monotonic: Callable[[], float] = time.perf_counter,
) -> AcceleratorStatus:
    """Probe real GPU arithmetic and the ONNX/DirectML inference provider.

    CUDA remains supported for contributors with NVIDIA hardware. On native Windows,
    DirectML is attempted next because it supports DirectX 12 GPUs such as the Radeon
    RX 6600 XT. Failure is returned as data so the installer can explain the remedy.
    """

    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        raise ValueError("now must include a timezone")
    operating_system = system or platform.system()
    notes: list[str] = []
    selected_device_index = 0

    torch: ModuleType | None = None
    torch_version: str | None = None
    training_backend = "cpu"
    training_ready = False
    device_name: str | None = None
    arithmetic_ms: float | None = None
    try:
        torch = importer("torch")
        torch_version = _module_version(torch)
    except (ImportError, OSError) as error:
        notes.append(f"PyTorch is unavailable: {type(error).__name__}: {error}")

    if torch is not None:
        try:
            if bool(torch.cuda.is_available()):
                device = torch.device("cuda")
                arithmetic_ms = _verify_torch_device(
                    torch, device, monotonic=monotonic
                )
                training_backend = "cuda"
                training_ready = True
                candidate = torch.cuda.get_device_name(0)
                device_name = str(candidate) if candidate else "CUDA GPU"
        except (AttributeError, RuntimeError, OSError) as error:
            notes.append(f"CUDA check failed: {type(error).__name__}: {error}")

    if torch is not None and not training_ready and operating_system == "Windows":
        try:
            directml = importer("torch_directml")
            default_index = int(directml.default_device())
            device_count = int(directml.device_count())
            if device_count <= 0:
                raise RuntimeError("DirectML reported no graphics adapters")
            if default_index < 0 or default_index >= device_count:
                raise RuntimeError("DirectML returned an invalid default adapter index")
            adapter_names: list[str | None] = []
            for index in range(device_count):
                try:
                    candidate_name = directml.device_name(index)
                    adapter_names.append(
                        str(candidate_name) if candidate_name else None
                    )
                except (AttributeError, RuntimeError, OSError):
                    adapter_names.append(None)
            radeon_6600_indices = [
                index
                for index, name in enumerate(adapter_names)
                if name is not None and "rx 6600 xt" in name.casefold()
            ]
            selected_index = (
                radeon_6600_indices[0] if radeon_6600_indices else default_index
            )
            if selected_index < 0:
                raise RuntimeError("DirectML returned a negative default adapter index")
            device = directml.device(selected_index)
            arithmetic_ms = _verify_torch_device(
                torch, device, monotonic=monotonic
            )
            training_backend = "directml"
            training_ready = True
            selected_device_index = selected_index
            candidate = adapter_names[selected_index]
            device_name = str(candidate) if candidate else "DirectML GPU"
        except (ImportError, AttributeError, RuntimeError, OSError, ValueError) as error:
            notes.append(f"DirectML training check failed: {type(error).__name__}: {error}")

    inference_backend = "cpu"
    inference_ready = False
    onnxruntime_version: str | None = None
    providers: tuple[str, ...] = ()
    try:
        onnxruntime = importer("onnxruntime")
        onnxruntime_version = _module_version(onnxruntime)
        raw_providers = onnxruntime.get_available_providers()
        if not isinstance(raw_providers, (list, tuple)) or not all(
            isinstance(provider, str) for provider in raw_providers
        ):
            raise RuntimeError("ONNX Runtime returned invalid provider metadata")
        providers = tuple(raw_providers)
        if "DmlExecutionProvider" in providers and operating_system == "Windows":
            _verify_onnx_provider(
                onnxruntime,
                "DmlExecutionProvider",
                importer=importer,
                device_id=selected_device_index,
            )
            inference_backend = "directml"
            inference_ready = True
        elif "CUDAExecutionProvider" in providers:
            _verify_onnx_provider(
                onnxruntime, "CUDAExecutionProvider", importer=importer
            )
            inference_backend = "cuda"
            inference_ready = True
        else:
            notes.append("ONNX Runtime has no GPU execution provider enabled")
    # ONNX Runtime exposes provider failures as custom Exception subclasses that do
    # not consistently inherit RuntimeError. Capability probing must return NOT READY
    # rather than abort the installer/status command.
    except Exception as error:
        notes.append(f"ONNX Runtime check failed: {type(error).__name__}: {error}")

    if not training_ready:
        notes.append("Heavy model training would currently fall back to the CPU")
    if inference_ready:
        notes.append("A GPU-only ONNX arithmetic session completed successfully")

    return AcceleratorStatus(
        training_backend=training_backend,
        training_ready=training_ready,
        device_name=device_name,
        torch_version=torch_version,
        arithmetic_check_ms=arithmetic_ms,
        inference_backend=inference_backend,
        inference_ready=inference_ready,
        onnxruntime_version=onnxruntime_version,
        inference_providers=providers,
        checked_at=checked_at.astimezone(UTC),
        notes=tuple(notes),
    )
