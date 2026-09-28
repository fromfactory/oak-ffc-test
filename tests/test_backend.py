"""Backend integration tests use synthetic images and never open USB hardware."""

import copy
import time

import cv2
import numpy as np
import pytest

from oak_camera.backend import CameraBackend


@pytest.fixture
def backend():
    instance = CameraBackend(demo=True)
    instance.scan()
    try:
        yield instance
    finally:
        instance.close()


def configuration(sockets=("CAM_A",), resolution="1080p", raw=False):
    return {"cameras": [{"socket": socket, "resolution": resolution, "fps": 10}
                        for socket in sockets], "raw_enabled": raw}


def wait_for_frames(backend, count):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        active = [cam for cam in backend.status()["cameras"] if cam["active"]]
        if len(active) == count and all(cam["frames"] >= 2 for cam in active):
            return active
        time.sleep(0.02)
    pytest.fail("Synthetic camera worker did not produce previews")


def test_one_two_three_cameras_and_restart(backend):
    for sockets in (("CAM_D",), ("CAM_A", "CAM_D"), ("CAM_A", "CAM_B", "CAM_D")):
        backend.start(configuration(sockets))
        cameras = wait_for_frames(backend, len(sockets))
        assert {camera["socket"] for camera in cameras} == set(sockets)
        for camera in cameras:
            serial, payload = backend.preview(camera["socket"])
            frame = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            assert serial > 0
            assert frame.shape == (360, 640, 3)
            assert camera["metadata"]["demo"] is True
        backend.stop()
        assert backend.preview(sockets[0]) is None
        assert all(not camera["active"] for camera in backend.status()["cameras"])


def test_invalid_changes_leave_running_pipeline_and_settings_intact(backend):
    backend.start(configuration())
    before = copy.deepcopy(backend.status()["cameras"][0]["controls"])
    with pytest.raises(ValueError):
        backend.start(configuration(("CAM_C",)))
    assert backend.status()["running"]
    with pytest.raises(ValueError, match="frame period"):
        backend.controls("CAM_A", {"exposure_mode": "manual", "exposure_us": 100001})
    assert backend.status()["cameras"][0]["controls"] == before
    with pytest.raises(ValueError, match="not active"):
        backend.capture("CAM_D", "jpeg")
    with pytest.raises(ValueError, match="Enable RAW"):
        backend.capture("CAM_A", "raw")


@pytest.mark.parametrize("format", ["jpeg", "png", "tiff", "bmp"])
def test_processed_captures_use_full_mode_dimensions(backend, format):
    backend.start(configuration())
    capture = backend.capture("CAM_A", format)
    frame = cv2.imdecode(np.frombuffer(capture["data"], np.uint8), cv2.IMREAD_UNCHANGED)
    assert frame.shape == (1080, 1920, 3)
    assert capture["metadata"]["source"] == "synthetic_still"
    assert capture["metadata"]["demo"] is True
    assert capture["metadata"]["requested_controls"]["exposure_mode"] == "auto"


def test_manual_settings_change_demo_pixels_and_report_actual_values(backend):
    backend.start(configuration())
    before = backend.capture("CAM_A", "png")
    requested = {"exposure_mode": "manual", "exposure_us": 2000, "iso": 100,
                 "white_balance_mode": "manual", "white_balance_kelvin": 6000,
                 "focus_mode": "manual", "focus": 200}
    backend.controls("CAM_A", requested)
    after = backend.capture("CAM_A", "png")
    before_pixels = cv2.imdecode(np.frombuffer(before["data"], np.uint8), cv2.IMREAD_COLOR)
    after_pixels = cv2.imdecode(np.frombuffer(after["data"], np.uint8), cv2.IMREAD_COLOR)
    assert after_pixels.mean() < before_pixels.mean() / 2
    assert after["metadata"]["exposure_us"] == 2000
    assert after["metadata"]["iso"] == 100
    assert after["metadata"]["white_balance_kelvin"] == 6000
    assert after["metadata"]["lens_position"] == 200


def test_twelve_megapixel_still_and_raw_have_distinct_native_widths(backend):
    backend.start(configuration(resolution="12mp", raw=True))
    capture = backend.capture("CAM_A", "jpeg")
    assert (capture["metadata"]["width"], capture["metadata"]["height"]) == (4032, 3040)
    assert capture["metadata"]["sensor_mode_width"] == 4056
    raw = backend.capture("CAM_A", "raw")
    metadata = raw["metadata"]
    assert (metadata["width"], metadata["height"]) == (4056, 3040)
    assert metadata["packing"] == "MIPI_RAW10"
    assert metadata["frame_type"] == "PACK10"
    assert metadata["cfa_pattern"] is None
    assert metadata["demo"] is True
    assert len(raw["data"]) == metadata["byte_count"] == metadata["row_stride_bytes"] * 3040
    # Decode the first two MIPI groups independently to check byte ordering.
    b = raw["data"][:10]
    decoded = [(b[group * 5 + pixel] << 2) | ((b[group * 5 + 4] >> (pixel * 2)) & 3)
               for group in range(2) for pixel in range(4)]
    assert decoded == list(range(8))


def test_ambiguous_raw_payload_is_rejected(backend):
    metadata = {"width": 1920, "height": 1080, "frame_type": "PACK10"}
    with pytest.raises(RuntimeError, match="ambiguous"):
        backend._raw_metadata(metadata, b"\x00" * 17)


def test_native_pipeline_builds_without_accessing_hardware(backend):
    dai = pytest.importorskip("depthai")
    backend._dai = dai
    pipeline = backend._make_pipeline(configuration(("CAM_A", "CAM_B", "CAM_D"), "12mp", raw=True))
    cameras = [node for node in pipeline.getAllNodes() if isinstance(node, dai.node.ColorCamera)]
    assert len(cameras) == 3
    for camera in cameras:
        assert camera.getStillSize() == (4032, 3040)
        assert camera.getPreviewSize() == (640, 480)
        assert camera.getResolutionSize() == (4056, 3040)


def test_close_is_idempotent_and_rejects_new_requests(backend):
    backend.close()
    backend.close()
    with pytest.raises(RuntimeError, match="closed"):
        backend.start(configuration())


def test_stream_failure_stops_all_cameras_and_clears_previews(backend, monkeypatch):
    backend.start(configuration(("CAM_A", "CAM_D")))
    wait_for_frames(backend, 2)

    def disconnected(*args, **kwargs):
        raise RuntimeError("Simulated USB disconnect")

    monkeypatch.setattr(backend, "_poll", disconnected)
    deadline = time.monotonic() + 2
    while backend.status()["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    status = backend.status()
    assert not status["running"]
    assert status["error"] == "Simulated USB disconnect"
    assert all(not cam["active"] for cam in status["cameras"])
    assert backend.preview("CAM_A") is None
    assert backend.preview("CAM_D") is None
    with pytest.raises(RuntimeError, match="Simulated USB disconnect"):
        backend.capture("CAM_A", "jpeg")
