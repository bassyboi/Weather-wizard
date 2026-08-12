import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ordcast.accelerator import probe_accelerator
from pipelines.local_gpu import write_gpu_status


UTC = timezone.utc


class FakeTensor:
    device = "cpu"

    def to(self, device):
        self.device = device
        return self

    def __matmul__(self, _other):
        return self

    def sum(self):
        return self

    def item(self):
        return 54.0


class FakeOnnxModel:
    ir_version = None

    def SerializeToString(self):
        return b"onnx-smoke"


class FakeSessionOptions:
    def add_session_config_entry(self, name, value):
        assert (name, value) == ("session.disable_cpu_ep_fallback", "1")


class FakeInferenceSession:
    def __init__(self, model, *, sess_options, providers):
        assert model == b"onnx-smoke"
        assert sess_options.enable_mem_pattern is False
        self.providers = [entry[0] if isinstance(entry, tuple) else entry for entry in providers]
        assert providers == [("DmlExecutionProvider", {"device_id": "1"})]

    def get_providers(self):
        return self.providers

    def run(self, names, inputs):
        assert names == ["result"]
        assert inputs == {"x": [[1.0, 3.0]]}
        return [[[2.0, 6.0]]]


def test_directml_probe_runs_real_arithmetic_contract():
    torch = SimpleNamespace(
        __version__="2.4.1",
        float32="float32",
        tensor=lambda *_args, **_kwargs: FakeTensor(),
        cuda=SimpleNamespace(is_available=lambda: False),
    )
    directml = SimpleNamespace(
        default_device=lambda: 0,
        device_count=lambda: 2,
        device=lambda index: f"privateuseone:{index}",
        device_name=lambda index: (
            "Integrated graphics" if index == 0 else "AMD Radeon RX 6600 XT"
        ),
    )
    onnxruntime = SimpleNamespace(
        __version__="1.24.4",
        SessionOptions=FakeSessionOptions,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        InferenceSession=FakeInferenceSession,
        get_available_providers=lambda: [
            "DmlExecutionProvider",
            "CPUExecutionProvider",
        ],
    )
    helper = SimpleNamespace(
        make_node=lambda *args, **kwargs: (args, kwargs),
        make_tensor_value_info=lambda *args, **kwargs: (args, kwargs),
        make_tensor=lambda *args, **kwargs: (args, kwargs),
        make_graph=lambda *args, **kwargs: (args, kwargs),
        make_opsetid=lambda *args, **kwargs: (args, kwargs),
        make_model=lambda *args, **kwargs: FakeOnnxModel(),
    )
    onnx = SimpleNamespace(helper=helper, TensorProto=SimpleNamespace(FLOAT=1))
    numpy = SimpleNamespace(
        float32="float32",
        asarray=lambda value, dtype: value,
        allclose=lambda observed, expected, **kwargs: observed == expected,
    )
    modules = {
        "torch": torch,
        "torch_directml": directml,
        "onnxruntime": onnxruntime,
        "onnx": onnx,
        "numpy": numpy,
    }
    times = iter([10.0, 10.012])

    status = probe_accelerator(
        importer=modules.__getitem__,
        system="Windows",
        now=datetime(2026, 8, 12, tzinfo=UTC),
        monotonic=lambda: next(times),
    )

    assert status.training_ready
    assert status.training_backend == "directml"
    assert status.device_name == "AMD Radeon RX 6600 XT"
    assert status.arithmetic_check_ms == pytest.approx(12.0)
    assert status.inference_ready
    assert status.inference_backend == "directml"
    assert status.fully_ready


def test_missing_gpu_packages_return_an_honest_cpu_status():
    def missing(_name):
        raise ImportError("not installed")

    status = probe_accelerator(
        importer=missing,
        system="Windows",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert not status.training_ready
    assert not status.inference_ready
    assert status.training_backend == "cpu"
    assert any("fall back" in note for note in status.notes)


def test_onnx_provider_custom_exception_becomes_not_ready_status():
    class ProviderFailure(Exception):
        pass

    class FailingSession:
        def __init__(self, *_args, **_kwargs):
            raise ProviderFailure("adapter could not initialize")

    onnxruntime = SimpleNamespace(
        __version__="1.24.4",
        SessionOptions=FakeSessionOptions,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        InferenceSession=FailingSession,
        get_available_providers=lambda: ["DmlExecutionProvider"],
    )
    helper = SimpleNamespace(
        make_node=lambda *args, **kwargs: (args, kwargs),
        make_tensor_value_info=lambda *args, **kwargs: (args, kwargs),
        make_tensor=lambda *args, **kwargs: (args, kwargs),
        make_graph=lambda *args, **kwargs: (args, kwargs),
        make_opsetid=lambda *args, **kwargs: (args, kwargs),
        make_model=lambda *args, **kwargs: FakeOnnxModel(),
    )
    modules = {
        "onnxruntime": onnxruntime,
        "onnx": SimpleNamespace(
            helper=helper, TensorProto=SimpleNamespace(FLOAT=1)
        ),
        "numpy": SimpleNamespace(float32="float32"),
    }

    def importer(name):
        if name == "torch":
            raise ImportError("not installed")
        return modules[name]

    status = probe_accelerator(
        importer=importer,
        system="Windows",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert not status.inference_ready
    assert any("ProviderFailure" in note for note in status.notes)


def test_gpu_status_write_is_valid_json_and_replaceable(tmp_path):
    path = tmp_path / "state" / "gpu-status.json"
    write_gpu_status(path, {"schema_version": 1, "ready": False})
    write_gpu_status(path, {"schema_version": 1, "ready": True})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "ready": True,
        "schema_version": 1,
    }
    assert not list(path.parent.glob(".ordcast-gpu-*.tmp"))
