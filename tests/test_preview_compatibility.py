"""Regressions for the observed CAM_A/CAM_D sensor-mode mismatch.

All camera operation below uses synthetic demo frames. The optional DepthAI
check only constructs a graph; it never connects to a device.
"""

import itertools
import time

import pytest

from oak_camera.app import create_app
from oak_camera.backend import CameraBackend
from oak_camera.validation import validate_config


MODES = ("1080p", "4k", "12mp")
MIXED_MODES = tuple(itertools.permutations(MODES, 2))


def configuration(*cameras, raw=False):
    return {
        "cameras": [
            {"socket": socket, "resolution": resolution, "fps": 10}
            for socket, resolution in cameras
        ],
        "raw_enabled": raw,
    }


@pytest.fixture
def backend():
    instance = CameraBackend(demo=True)
    instance.scan()
    try:
        yield instance
    finally:
        instance.close()


def wait_for_preview(backend, socket, after_serial=0):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        item = backend.preview(socket)
        if item and item[0] > after_serial:
            return item
        time.sleep(0.02)
    pytest.fail(f"No fresh synthetic preview from {socket}")


@pytest.mark.parametrize("mode_a,mode_d", MIXED_MODES)
@pytest.mark.parametrize("reverse", (False, True))
def test_mixed_a_d_resolutions_are_rejected_in_either_order(mode_a, mode_d, reverse):
    cameras = [("CAM_A", mode_a), ("CAM_D", mode_d)]
    if reverse:
        cameras.reverse()
    with pytest.raises(ValueError) as caught:
        validate_config(configuration(*cameras))
    message = str(caught.value)
    assert "CAM_A" in message
    assert "CAM_D" in message


@pytest.mark.parametrize("raw", (False, True))
def test_third_camera_does_not_hide_the_incompatible_pair(raw):
    with pytest.raises(ValueError):
        validate_config(configuration(
            ("CAM_A", "4k"), ("CAM_B", "4k"), ("CAM_D", "1080p"), raw=raw,
        ))


@pytest.mark.parametrize("entrypoint", ("http", "backend"))
def test_incompatible_request_preserves_a_running_pipeline(backend, tmp_path, entrypoint):
    original = configuration(("CAM_A", "4k"), ("CAM_D", "4k"))
    backend.start(original)
    backend.controls("CAM_A", {"brightness": 3})
    serial, _ = wait_for_preview(backend, "CAM_A")
    before = backend.status()
    invalid = configuration(("CAM_A", "12mp"), ("CAM_D", "4k"))

    if entrypoint == "http":
        app = create_app(backend=backend, capture_dir=tmp_path / "captures", demo=True)
        response = app.test_client().post("/api/start", json=invalid)
        assert response.status_code == 400
        assert "error" in response.json
    else:
        with pytest.raises(ValueError):
            backend.start(invalid)

    after = backend.status()
    assert after["running"] is True
    assert after["error"] is None
    assert after["config"] == before["config"]
    camera_a = next(camera for camera in after["cameras"] if camera["socket"] == "CAM_A")
    assert camera_a["controls"]["brightness"] == 3
    assert wait_for_preview(backend, "CAM_A", after_serial=serial)[0] > serial


@pytest.mark.parametrize("mode", ("4k", "12mp"))
@pytest.mark.parametrize("raw", (False, True))
def test_matching_high_resolution_pair_remains_available(backend, mode, raw):
    config = configuration(("CAM_A", mode), ("CAM_D", mode), raw=raw)
    # Independent frame rates are unaffected by the resolution compatibility rule.
    config["cameras"][0]["fps"] = 7
    config["cameras"][1]["fps"] = 10
    validated = validate_config(config)
    result = backend.start(validated)
    assert result["running"] is True
    assert result["config"] == validated
    assert result["raw_enabled"] is raw
    assert {camera["socket"] for camera in result["cameras"] if camera["active"]} == {"CAM_A", "CAM_D"}


@pytest.mark.parametrize("socket", ("CAM_A", "CAM_D"))
@pytest.mark.parametrize("mode", ("4k", "12mp"))
def test_a_single_camera_can_use_either_high_resolution(backend, socket, mode):
    config = validate_config(configuration((socket, mode), raw=True))
    result = backend.start(config)
    assert result["running"] is True
    assert result["config"]["cameras"] == [{"socket": socket, "resolution": mode, "fps": 10.0}]


@pytest.mark.parametrize("cameras", (
    (("CAM_A", "4k"), ("CAM_B", "1080p")),
    (("CAM_A", "1080p"), ("CAM_B", "12mp")),
    (("CAM_D", "4k"), ("CAM_B", "1080p")),
    (("CAM_A", "4k"), ("CAM_D", "4k"), ("CAM_B", "1080p")),
))
def test_other_camera_combinations_are_not_blocked_without_evidence(backend, cameras):
    config = validate_config(configuration(*cameras))
    result = backend.start(config)
    assert result["running"] is True
    assert result["config"]["cameras"] == config["cameras"]


def test_native_sensor_modes_keep_small_preview_payloads(backend, monkeypatch):
    dai = pytest.importorskip("depthai")

    def forbid_device(*args, **kwargs):
        pytest.fail("This graph-construction test must never open USB hardware")

    monkeypatch.setattr(dai, "Device", forbid_device)
    backend._dai = dai
    expected = {
        "1080p": {"sensor": (1920, 1080), "still": (1920, 1080), "preview": (640, 360)},
        "4k": {"sensor": (3840, 2160), "still": (3840, 2160), "preview": (640, 360)},
        "12mp": {"sensor": (4056, 3040), "still": (4032, 3040), "preview": (640, 480)},
    }
    payloads = {}
    for mode, sizes in expected.items():
        config = validate_config(configuration(("CAM_A", mode), ("CAM_D", mode), raw=True))
        pipeline = backend._make_pipeline(config)
        cameras = [node for node in pipeline.getAllNodes() if isinstance(node, dai.node.ColorCamera)]
        assert len(cameras) == 2
        for camera in cameras:
            assert camera.getResolutionSize() == sizes["sensor"]
            assert camera.getStillSize() == sizes["still"]
            assert camera.getPreviewSize() == sizes["preview"]
            assert camera.getInterleaved() is True
            assert camera.getColorOrder() == dai.ColorCameraProperties.ColorOrder.BGR
        # Three 8-bit channels per preview pixel; full-mode still and RAW payloads
        # must not replace the preview stream merely because the sensor mode rose.
        width, height = cameras[0].getPreviewSize()
        payloads[mode] = width * height * 3

    assert payloads["1080p"] == payloads["4k"] == 691_200
    assert payloads["12mp"] == 921_600


@pytest.mark.parametrize("raw", (False, True))
@pytest.mark.parametrize("sockets", (
    ("CAM_A",),
    ("CAM_A", "CAM_D"),
    ("CAM_A", "CAM_B", "CAM_D"),
))
def test_camera_command_inputs_have_bounded_device_memory(backend, monkeypatch, raw, sockets):
    dai = pytest.importorskip("depthai")

    def forbid_device(*args, **kwargs):
        pytest.fail("This input-buffer test must never open USB hardware")

    monkeypatch.setattr(dai, "Device", forbid_device)
    backend._dai = dai
    config = validate_config(configuration(*((socket, "12mp") for socket in sockets), raw=raw))
    pipeline = backend._make_pipeline(config)
    inputs = [node for node in pipeline.getAllNodes() if isinstance(node, dai.node.XLinkIn)]
    expected = {socket + "_control" for socket in sockets}
    if raw:
        expected.update(socket + "_raw_trigger" for socket in sockets)
    assert {node.getStreamName() for node in inputs} == expected
    assert len(inputs) == len(expected)

    for node in inputs:
        # XLinkIn defaults reserve 5 MiB × 8 frames per input. These streams
        # carry only camera controls or one-byte triggers, never image payloads.
        assert node.getMaxDataSize() == 1024, node.getStreamName()
        assert node.getNumFrames() == 4, node.getStreamName()
    reserved = sum(node.getMaxDataSize() * node.getNumFrames() for node in inputs)
    assert reserved == len(expected) * 4096
    assert reserved <= 24 * 1024  # Six inputs in the three-camera RAW configuration.
