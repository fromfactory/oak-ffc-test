"""Live-control regressions use demo cameras and native SDK messages, never USB."""

from copy import deepcopy
import json

import numpy as np
import pytest

from oak_camera.app import create_app
from oak_camera.backend import CameraBackend, DEFAULT_CONTROLS
from oak_camera.diagnostics import build_diagnostics
from oak_camera.validation import validate_controls


@pytest.fixture
def backend():
    instance = CameraBackend(demo=True)
    instance.scan()
    try:
        yield instance
    finally:
        instance.close()


def start(backend, fps=10):
    backend.start({"cameras": [{"socket": "CAM_A", "resolution": "1080p", "fps": fps}],
                   "raw_enabled": False})


def native_message(backend, settings, previous=None, fps=10):
    dai = pytest.importorskip("depthai")
    backend._dai = dai
    camera = {"controls": {**DEFAULT_CONTROLS, **(previous or {})},
              "autofocus": True, "requested_fps": fps}
    merged = backend._validate_controls(camera, settings)
    message = backend._control_message(settings, merged, True, frame_period_us=int(1_000_000 / fps))
    return dai, message.get(), merged


@pytest.mark.parametrize("mode,enum_name", (
    ("incandescent", "INCANDESCENT"), ("fluorescent", "FLUORESCENT"),
    ("warm_fluorescent", "WARM_FLUORESCENT"), ("daylight", "DAYLIGHT"),
    ("cloudy", "CLOUDY_DAYLIGHT"), ("twilight", "TWILIGHT"), ("shade", "SHADE"),
))
def test_white_balance_presets_encode_real_sdk_modes_and_unlock(backend, mode, enum_name):
    dai, raw, merged = native_message(backend, {"white_balance_mode": mode},
                                      previous={"white_balance_lock": True})
    assert raw.awbMode == getattr(dai.CameraControl.AutoWhiteBalanceMode, enum_name)
    assert raw.getCommand(dai.RawCameraControl.Command.AWB_MODE)
    assert raw.getCommand(dai.RawCameraControl.Command.AWB_LOCK)
    assert raw.awbLockMode is False
    assert merged["white_balance_lock"] is False


@pytest.mark.parametrize("field,raw_field,command", (
    ("exposure_lock", "aeLockMode", "AE_LOCK"),
    ("white_balance_lock", "awbLockMode", "AWB_LOCK"),
))
@pytest.mark.parametrize("locked", (True, False))
def test_lock_only_updates_do_not_restart_auto_algorithms(backend, field, raw_field, command, locked):
    dai, raw, merged = native_message(backend, {field: locked})
    assert getattr(raw, raw_field) is locked
    assert merged[field] is locked
    assert raw.getCommand(getattr(dai.RawCameraControl.Command, command))
    assert not raw.getCommand(dai.RawCameraControl.Command.AE_AUTO)
    assert not raw.getCommand(dai.RawCameraControl.Command.AWB_MODE)


def test_mode_transitions_apply_unlock_commands_after_mode_commands(backend):
    dai = pytest.importorskip("depthai")
    backend._dai = dai

    class RecordingControl:
        def __init__(self):
            self.native = dai.CameraControl()
            self.calls = []

        def __getattr__(self, name):
            def call(*args):
                self.calls.append(name)
                return getattr(self.native, name)(*args)
            return call

    camera = {"autofocus": True, "requested_fps": 10,
              "controls": {**DEFAULT_CONTROLS, "exposure_lock": True, "white_balance_lock": True}}
    settings = {"exposure_mode": "manual", "white_balance_mode": "manual",
                "exposure_us": 9000, "iso": 500, "white_balance_kelvin": 5200}
    merged = backend._validate_controls(camera, settings)
    target = RecordingControl()
    backend._control_message(settings, merged, True, target, frame_period_us=100000)
    assert target.calls.index("setAutoExposureLock") > target.calls.index("setManualExposure")
    assert target.calls.index("setAutoWhiteBalanceLock") > target.calls.index("setManualWhiteBalance")
    raw = target.native.get()
    assert raw.aeLockMode is False and raw.awbLockMode is False
    assert target.native.getExposureTime().total_seconds() * 1_000_000 == pytest.approx(9000)
    assert target.native.getSensitivity() == 500
    assert raw.wbColorTemp == 5200

    settings = {"exposure_mode": "auto", "white_balance_mode": "auto",
                "exposure_lock": True, "white_balance_lock": True}
    camera["controls"] = merged
    merged = backend._validate_controls(camera, settings)
    target = RecordingControl()
    backend._control_message(settings, merged, True, target, frame_period_us=100000)
    assert target.calls.index("setAutoExposureLock") > target.calls.index("setAutoExposureEnable")
    assert target.calls.index("setAutoWhiteBalanceLock") > target.calls.index("setAutoWhiteBalanceMode")
    assert target.native.get().aeLockMode is True
    assert target.native.get().awbLockMode is True


@pytest.mark.parametrize("fps,requested,expected", ((10, 0, 100000), (30, 0, 33333), (10, 4000, 4000)))
def test_auto_exposure_ceiling_uses_positive_native_frame_period(backend, fps, requested, expected):
    dai, raw, _ = native_message(backend, {"auto_exposure_limit_us": requested}, fps=fps)
    assert raw.aeMaxExposureTimeUs == expected
    assert raw.getCommand(dai.RawCameraControl.Command.AE_TARGET_FPS_RANGE)


def test_stored_exposure_ceiling_is_applied_when_returning_to_auto(backend):
    dai, raw, merged = native_message(
        backend, {"exposure_mode": "auto"},
        previous={"exposure_mode": "manual", "auto_exposure_limit_us": 3000},
    )
    assert raw.getCommand(dai.RawCameraControl.Command.AE_AUTO)
    assert raw.aeMaxExposureTimeUs == 3000
    assert merged["exposure_lock"] is False


@pytest.mark.parametrize("settings", (
    {"exposure_mode": "manual", "exposure_lock": True},
    {"white_balance_mode": "manual", "white_balance_lock": True},
    {"white_balance_mode": "daylight", "white_balance_lock": True},
    {"auto_exposure_limit_us": 33334},
))
def test_invalid_merged_controls_leave_running_camera_unchanged(backend, settings):
    start(backend, fps=30)
    before = backend.status()["cameras"][0]["controls"]
    with pytest.raises(ValueError):
        backend.controls("CAM_A", settings)
    state = backend.status()
    assert state["running"] is True
    assert state["error"] is None
    assert state["cameras"][0]["controls"] == before


@pytest.mark.parametrize("settings", (
    {"exposure_lock": "false"}, {"exposure_lock": 0}, {"white_balance_lock": 1},
    {"white_balance_lock": None}, {"white_balance_mode": "off"},
    {"auto_exposure_limit_us": True}, {"auto_exposure_limit_us": -1},
    {"auto_exposure_limit_us": float("nan")}, {"auto_exposure_limit_us": 1.5},
    {"auto_exposure_limit_us": 1000001}, {"effect_mode": "solarize"},
    {"focus_range_min": 0},
))
def test_http_and_backend_share_new_control_type_validation(backend, tmp_path, settings):
    start(backend)
    before = backend.status()["cameras"][0]["controls"]
    with pytest.raises(ValueError):
        validate_controls(settings)
    with pytest.raises(ValueError):
        backend.controls("CAM_A", settings)
    app = create_app(backend=backend, capture_dir=tmp_path / "captures", demo=True)
    response = app.test_client().post("/api/controls/CAM_A", json=settings)
    assert response.status_code == 400
    assert backend.status()["cameras"][0]["controls"] == before


def test_http_partial_updates_preserve_other_settings_and_normalize_locks(backend, tmp_path):
    start(backend)
    app = create_app(backend=backend, capture_dir=tmp_path / "captures", demo=True)
    client = app.test_client()
    assert client.post("/api/controls/CAM_A", json={"exposure_lock": True, "white_balance_lock": True}).status_code == 200
    response = client.post("/api/controls/CAM_A", json={"effect_mode": "mono"})
    assert response.json["exposure_lock"] is True
    assert response.json["white_balance_lock"] is True
    response = client.post("/api/controls/CAM_A", json={"exposure_mode": "manual", "white_balance_mode": "cloudy"})
    assert response.status_code == 200
    assert response.json["exposure_lock"] is False
    assert response.json["white_balance_lock"] is False
    assert response.json["effect_mode"] == "mono"


@pytest.mark.parametrize("effect", ("off", "mono", "negative", "sepia"))
def test_effects_encode_native_sdk_commands(backend, effect):
    dai, raw, _ = native_message(backend, {"effect_mode": effect})
    assert raw.effectMode == getattr(dai.CameraControl.EffectMode, effect.upper())
    assert raw.getCommand(dai.RawCameraControl.Command.EFFECT_MODE)


def test_fixed_focus_initialization_does_not_send_autofocus_commands(backend):
    dai = pytest.importorskip("depthai")
    backend._dai = dai
    backend._cameras["CAM_A"]["autofocus"] = False
    config = {"cameras": [{"socket": "CAM_A", "resolution": "1080p", "fps": 30}], "raw_enabled": False}
    pipeline = backend._make_pipeline(config)
    camera = next(node for node in pipeline.getAllNodes() if isinstance(node, dai.node.ColorCamera))
    raw = camera.initialControl.get()
    assert not raw.getCommand(dai.RawCameraControl.Command.AF_MODE)
    assert not raw.getCommand(dai.RawCameraControl.Command.AF_TRIGGER)
    assert not raw.getCommand(dai.RawCameraControl.Command.MOVE_LENS)
    assert raw.aeMaxExposureTimeUs == 33333
    assert raw.aeLockMode is False and raw.awbLockMode is False
    assert raw.effectMode == dai.CameraControl.EffectMode.OFF


def test_demo_effects_presets_and_exposure_limit_change_synthetic_pixels(backend):
    backend._image_support()
    camera = deepcopy(backend._cameras["CAM_A"])
    original = backend._demo_frame(camera, 640, 360, now=0)[50:280].copy()
    images = {}
    for mode in ("mono", "negative", "sepia"):
        camera["controls"]["effect_mode"] = mode
        images[mode] = backend._demo_frame(camera, 640, 360, now=0)[50:280]
    assert np.array_equal(images["mono"][:, :, 0], images["mono"][:, :, 1])
    assert np.array_equal(images["mono"][:, :, 1], images["mono"][:, :, 2])
    assert np.array_equal(images["negative"], 255 - original)
    assert images["sepia"][:, :, 2].mean() > images["sepia"][:, :, 0].mean()
    camera["controls"]["effect_mode"] = "off"
    camera["controls"]["white_balance_mode"] = "incandescent"
    preset = backend._demo_frame(camera, 640, 360, now=0)[50:280]
    assert not np.array_equal(preset, original)
    camera["controls"]["auto_exposure_limit_us"] = 1000
    assert backend._demo_readback(camera)["exposure_us"] == 1000
    assert "synthetic" in backend._demo_metadata(camera, 640, 360)["simulation_note"]


def test_diagnostics_export_boolean_controls_without_coercing_private_text(backend):
    start(backend)
    backend.controls("CAM_A", {"exposure_lock": True, "white_balance_lock": False,
                               "effect_mode": "sepia", "auto_exposure_limit_us": 4000})
    state = backend.status()
    options = {"app_version": "1.0.0", "packages": {}, "python_version": "3.13.5",
               "system": "Linux", "machine": "aarch64"}
    report = build_diagnostics(state, [], **options)
    controls = report["status"]["cameras"][0]["controls"]
    assert controls["exposure_lock"] is True
    assert controls["white_balance_lock"] is False
    assert controls["effect_mode"] == "sepia"
    assert controls["auto_exposure_limit_us"] == 4000
    state["cameras"][0]["controls"].update(exposure_lock="SYNTHETIC_PRIVATE_MARKER", white_balance_lock=1)
    report = build_diagnostics(state, [], **options)
    assert "SYNTHETIC_PRIVATE_MARKER" not in json.dumps(report)
    controls = report["status"]["cameras"][0]["controls"]
    assert "exposure_lock" not in controls
    assert "white_balance_lock" not in controls
