"""Diagnostic exports retain technical facts and omit synthetic private markers."""

from copy import deepcopy
import json
import math

import pytest

from oak_camera.app import create_app
from oak_camera.diagnostics import build_diagnostics


# Deliberately artificial values: these are not credentials or actual device IDs.
PRIVATE = {
    "id": "PRIVATE_DEVICE_IDENTIFIER",
    "name": "PRIVATE_DEVICE_NAME",
    "path": "/private/example-user/captures",
    "error": "PRIVATE_EXCEPTION_TEXT",
    "capture": "PRIVATE_CAPTURE_REFERENCE",
    "time": "PRIVATE_CAPTURE_TIMESTAMP",
    "unknown": "PRIVATE_UNKNOWN_FIELD",
}


@pytest.fixture
def local_status():
    return {
        "running": True, "demo": False, "raw_enabled": True,
        "error": PRIVATE["error"], "warnings": [PRIVATE["error"]],
        "device": {"id": PRIVATE["id"], "name": PRIVATE["name"], "usb_speed": "SUPER"},
        "capture_directory": PRIVATE["path"],
        "config": {"cameras": [{"socket": "CAM_A", "resolution": "12mp", "fps": 10,
                                 "private": PRIVATE["unknown"]}], "raw_enabled": True},
        "cameras": [{
            "socket": "CAM_A", "label": PRIVATE["name"], "sensor": "IMX378",
            "autofocus": True, "active": True, "width": 4056, "height": 3040,
            "resolution": "12mp", "requested_fps": 10, "frames": 123, "fps": 9.8,
            "last_frame_age": 0.05, "device_id": PRIVATE["id"],
            "controls": {"exposure_mode": "manual", "exposure_us": 8000, "iso": 400,
                         "private": PRIVATE["unknown"]},
            "metadata": {"width": 640, "height": 480, "sequence": 122,
                         "exposure_us": 7999.5, "iso": 400, "lens_position": 120,
                         "white_balance_kelvin": 4500, "frame_type": "BGR888i",
                         "received_at": PRIVATE["time"], "timestamp_device_s": 1234.567,
                         "error": PRIVATE["error"], "device": {"id": PRIVATE["id"]}},
        }],
        "captures": [{"id": PRIVATE["capture"], "created_at": PRIVATE["time"],
                      "metadata": {"device": {"id": PRIVATE["id"]}}}],
        "future_status_field": {"private": PRIVATE["unknown"]},
    }


def report(status, events=(), **overrides):
    options = {
        "app_version": "0.1.0", "packages": {"depthai": "2.30.0.0", "Flask": "3.1.2"},
        "python_version": "3.13.5", "system": "Linux", "machine": "aarch64",
    }
    return build_diagnostics(status, events, **{**options, **overrides})


def test_report_omits_private_records_and_preserves_technical_fields(local_status):
    original = deepcopy(local_status)
    events = [
        {"action": "error", "error": PRIVATE["error"], "path": PRIVATE["path"]},
        {"action": "capture_failed", "error": PRIVATE["error"], "time": PRIVATE["time"]},
        {"action": "capture", "id": PRIVATE["capture"], "time": PRIVATE["time"]},
        {"action": "new_event", "private": PRIVATE["unknown"]},
    ]
    saved_events = deepcopy(events)
    result = report(local_status, events)
    encoded = json.dumps(result, allow_nan=False)
    assert all(marker not in encoded for marker in PRIVATE.values())
    assert "captures" not in result["status"]
    assert "capture_directory" not in result["status"]
    assert "events" not in result
    assert result["event_counts"] == {"errors": 1, "capture_failures": 1}
    assert result["platform"] == {"system": "Linux", "machine": "aarch64"}
    assert result["python"] == "3.13.5"
    assert result["packages"]["depthai"] == "2.30.0.0"
    status = result["status"]
    assert status["running"] is True
    assert status["raw_enabled"] is True
    assert status["error_present"] is True
    assert status["warning_count"] == 1
    assert status["device"] == {"connected": True, "usb_speed": "SUPER"}
    assert status["config"]["cameras"] == [{"socket": "CAM_A", "resolution": "12mp", "fps": 10}]
    camera, = status["cameras"]
    assert camera["sensor"] == "IMX378"
    assert camera["frames"] == 123
    assert camera["fps"] == 9.8
    assert camera["last_frame_age"] == 0.05
    assert camera["controls"] == {"exposure_mode": "manual", "exposure_us": 8000, "iso": 400}
    assert camera["metadata"]["exposure_us"] == 7999.5
    assert camera["metadata"]["sequence"] == 122
    assert "timestamp_device_s" not in camera["metadata"]
    assert "received_at" not in camera["metadata"]
    # Exporting never edits the operational data used by the local application.
    assert local_status == original
    assert events == saved_events


@pytest.mark.parametrize("field", ("fps", "frames", "last_frame_age", "requested_fps"))
@pytest.mark.parametrize("value", (PRIVATE["unknown"], float("nan"), float("inf"), -1, True))
def test_health_fields_cannot_copy_arbitrary_text_or_nonfinite_values(local_status, field, value):
    local_status["cameras"][0][field] = value
    result = report(local_status)
    assert result["status"]["cameras"][0][field] is None
    assert PRIVATE["unknown"] not in json.dumps(result, allow_nan=False)


def test_allowlisted_fields_reject_unrecognized_model_and_enum_values(local_status):
    camera = local_status["cameras"][0]
    camera["sensor"] = PRIVATE["unknown"]
    camera["resolution"] = PRIVATE["unknown"]
    camera["controls"]["exposure_mode"] = PRIVATE["unknown"]
    camera["controls"]["iso"] = PRIVATE["unknown"]
    camera["metadata"]["frame_type"] = PRIVATE["unknown"]
    camera["metadata"]["iso"] = math.nan
    local_status["device"]["usb_speed"] = PRIVATE["unknown"]
    local_status["config"]["cameras"].append({"socket": PRIVATE["unknown"], "resolution": "4k", "fps": 10})
    local_status["cameras"].append({"socket": PRIVATE["unknown"], "sensor": "IMX378"})
    result = report(local_status)
    assert PRIVATE["unknown"] not in json.dumps(result, allow_nan=False)
    safe, = result["status"]["cameras"]
    assert safe["sensor"] == "unknown"
    assert "exposure_mode" not in safe["controls"]
    assert "iso" not in safe["controls"]
    assert "frame_type" not in safe["metadata"]
    assert len(result["status"]["config"]["cameras"]) == 1


def test_version_and_platform_fields_omit_local_build_and_freeform_data(local_status):
    result = report(
        local_status, app_version="0.1.0+private.build.label", python_version=PRIVATE["unknown"],
        system=PRIVATE["unknown"], machine=PRIVATE["unknown"],
        packages={"depthai": "2.30.0.0+private.build.label", "Flask": "not installed",
                  "numpy": PRIVATE["unknown"], "unrelated-package": PRIVATE["unknown"]},
    )
    encoded = json.dumps(result)
    assert PRIVATE["unknown"] not in encoded
    assert "private.build.label" not in encoded
    assert "unrelated-package" not in result["packages"]
    assert result["app_version"] == "0.1.0"
    assert result["packages"]["depthai"] == "2.30.0.0"
    assert result["packages"]["Flask"] == "not installed"
    assert result["platform"] == {"system": "other", "machine": "other"}


def test_http_report_is_sanitized_while_local_status_retains_operational_data(local_status, tmp_path, monkeypatch):
    class Backend:
        def status(self):
            return deepcopy(local_status)

        def stop(self):
            raise RuntimeError(PRIVATE["error"])

    app = create_app(backend=Backend(), capture_dir=tmp_path / "captures")
    app.config["TESTING"] = True
    client = app.test_client()
    live = client.get("/api/status").json
    assert live["device"]["id"] == PRIVATE["id"]
    assert live["capture_directory"] == str((tmp_path / "captures").resolve())
    assert client.post("/api/stop", json={}).status_code == 409

    def forbidden(*args, **kwargs):
        pytest.fail("Reports must not read capture history or collect detailed platform information")

    monkeypatch.setattr(app.extensions["capture_store"], "list", forbidden)
    monkeypatch.setattr("oak_camera.app.platform.platform", forbidden)
    response = client.get("/api/report")
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    result = response.json
    assert all(marker not in response.get_data(as_text=True) for marker in PRIVATE.values())
    assert str(tmp_path) not in response.get_data(as_text=True)
    assert result["event_counts"]["errors"] == 1
    assert result["status"]["error_present"] is True
    assert result["status"]["cameras"][0]["frames"] == 123
