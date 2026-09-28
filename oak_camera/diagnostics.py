"""Build shareable technical reports without copying local operational records.

This is an allowlist, not a search-and-replace redactor. New status fields remain
private unless explicitly added here. Freeform errors and warnings are never
exported because their contents can originate outside the application.
"""

from datetime import datetime, timezone
import math
import re

from .validation import BOOLEANS, ENUMS, RANGES, RESOLUTIONS, SOCKETS


_PACKAGES = {"depthai", "Flask", "numpy", "opencv-python-headless"}
_USB_SPEEDS = {"SUPER", "SUPER_PLUS", "HIGH", "FULL", "LOW", "UNKNOWN", "DEMO"}
_FRAME_TYPES = {
    "BGR888i", "BGR888p", "RGB888i", "RGB888p", "NV12", "YUV420p", "I420",
    "GRAY8", "RAW8", "RAW10", "RAW12", "RAW14", "RAW16", "PACK10", "PACK12",
}
_SYSTEMS = {"Linux", "Windows", "Darwin", "FreeBSD", "OpenBSD", "NetBSD"}
_MACHINES = {"aarch64", "arm64", "armv6l", "armv7l", "x86_64", "AMD64",
             "i386", "i686", "ppc64", "ppc64le", "riscv64", "s390x"}
_READBACK_RANGES = {
    "width": (1, 65535), "height": (1, 65535), "sequence": (0, 2**63 - 1),
    "exposure_us": (0, 1_000_000), "iso": (0, 65535), "lens_position": (0, 255),
    "white_balance_kelvin": (0, 20000),
}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not low <= value <= high or not math.isfinite(value):
        return None
    return value


def _choice(value, choices, fallback=None):
    return value if isinstance(value, str) and value in choices else fallback


def _version(value):
    if value == "not installed":
        return value
    if isinstance(value, str):
        # A local build suffix may contain a workstation/user-specific label.
        public = value.partition("+")[0]
        if re.fullmatch(r"\d+(?:\.\d+){1,3}(?:(?:a|b|rc|\.post|\.dev)\d+)?", public):
            return public
    return "unknown"


def _sensor(value):
    if isinstance(value, str) and re.fullmatch(
        r"(?:IMX\d{3,4}|OV[A-Z0-9]{3,7}|AR\d{4})(?: \(simulated\))?", value
    ):
        return value
    return "unknown"


def _controls(value):
    result = {}
    for name, setting in _mapping(value).items():
        if name in BOOLEANS:
            accepted = setting if isinstance(setting, bool) else None
        elif name in ENUMS:
            accepted = _choice(setting, ENUMS[name])
        elif name in RANGES:
            accepted = _number(setting, *RANGES[name])
        else:
            continue
        if accepted is not None:
            result[name] = accepted
    return result


def _camera(value):
    camera = _mapping(value)
    socket = _choice(camera.get("socket"), SOCKETS)
    if socket is None:
        return None
    result = {
        "socket": socket, "sensor": _sensor(camera.get("sensor")),
        "active": bool(camera.get("active")), "autofocus": bool(camera.get("autofocus")),
        "resolution": _choice(camera.get("resolution"), RESOLUTIONS),
        "controls": _controls(camera.get("controls")),
    }
    for name, bounds in {
        "width": (1, 65535), "height": (1, 65535), "frames": (0, 2**63 - 1),
        "fps": (0, 1000), "requested_fps": (0, 1000), "last_frame_age": (0, 1_000_000_000),
    }.items():
        result[name] = _number(camera.get(name), *bounds)
    readback = _mapping(camera.get("metadata"))
    result["metadata"] = {
        name: accepted for name, bounds in _READBACK_RANGES.items()
        if (accepted := _number(readback.get(name), *bounds)) is not None
    }
    frame_type = _choice(readback.get("frame_type"), _FRAME_TYPES)
    if frame_type is not None:
        result["metadata"]["frame_type"] = frame_type
    return result


def build_diagnostics(status, events, *, app_version, packages, python_version, system, machine):
    """Return JSON-safe diagnostics without identifiers, captures, or error text."""
    status = _mapping(status)
    device = _mapping(status.get("device"))
    config = _mapping(status.get("config"))
    cameras = status.get("cameras")
    selected = config.get("cameras")
    safe_config = []
    for item in selected if isinstance(selected, list) else []:
        item = _mapping(item)
        socket = _choice(item.get("socket"), SOCKETS)
        resolution = _choice(item.get("resolution"), RESOLUTIONS)
        fps = _number(item.get("fps"), 2, 30)
        if socket is not None and resolution is not None and fps is not None:
            safe_config.append({"socket": socket, "resolution": resolution, "fps": fps})
    actions = [_mapping(item).get("action") for item in events if isinstance(item, dict)]
    warnings = status.get("warnings")
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "app_version": _version(app_version),
        "platform": {"system": _choice(system, _SYSTEMS, "other"),
                     "machine": _choice(machine, _MACHINES, "other")},
        "python": _version(python_version),
        "packages": {name: _version(version) for name, version in _mapping(packages).items()
                     if name in _PACKAGES},
        "status": {
            "running": bool(status.get("running")), "demo": bool(status.get("demo")),
            "raw_enabled": bool(status.get("raw_enabled")),
            "error_present": bool(status.get("error")),
            "warning_count": len(warnings) if isinstance(warnings, list) else 0,
            "device": {"connected": bool(device),
                       "usb_speed": _choice(device.get("usb_speed"), _USB_SPEEDS, "UNKNOWN")},
            "config": {"cameras": safe_config, "raw_enabled": bool(config.get("raw_enabled"))},
            "cameras": [safe for camera in cameras if (safe := _camera(camera)) is not None]
                       if isinstance(cameras, list) else [],
        },
        "event_counts": {"errors": actions.count("error"),
                         "capture_failures": actions.count("capture_failed")},
        "note": "Capture all is sequential; frame arrival is not proof of hardware synchronization. "
                "This report excludes local identifiers, file paths, capture history, and freeform error text.",
    }
