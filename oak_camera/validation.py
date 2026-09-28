"""Validate browser input before passing it to the camera worker."""

SOCKETS = {"CAM_A", "CAM_B", "CAM_C", "CAM_D"}
FORMATS = {"jpeg", "png", "tiff", "bmp", "raw"}
RESOLUTIONS = {"1080p", "4k", "12mp"}
ENUMS = {
    "exposure_mode": {"auto", "manual"},
    "white_balance_mode": {"auto", "manual", "incandescent", "fluorescent", "warm_fluorescent",
                           "daylight", "cloudy", "twilight", "shade"},
    "focus_mode": {"continuous", "auto", "manual"},
    "anti_banding": {"off", "50hz", "60hz", "auto"},
    "effect_mode": {"off", "mono", "negative", "sepia"},
}
BOOLEANS = {"exposure_lock", "white_balance_lock"}
RANGES = {
    "exposure_us": (1, 1_000_000), "iso": (100, 1600),
    "auto_exposure_limit_us": (0, 1_000_000),
    "white_balance_kelvin": (1000, 12000), "focus": (0, 255),
    "exposure_compensation": (-9, 9), "brightness": (-10, 10),
    "contrast": (-10, 10), "saturation": (-10, 10),
    "sharpness": (0, 4), "luma_denoise": (0, 4), "chroma_denoise": (0, 4),
}


def object_body(value):
    if not isinstance(value, dict):
        raise ValueError("The request body must be a JSON object.")
    return value


def number(value, name, low, high, integer=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number.")
    # Bounds also reject NaN/infinity without coercing arbitrarily large JSON
    # integers to float (which would raise OverflowError before validation).
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}.")
    if integer and value != int(value):
        raise ValueError(f"{name} must be a whole number.")
    return int(value) if integer else float(value)


def socket_name(value):
    if not isinstance(value, str) or value not in SOCKETS:
        raise ValueError("Choose a detected socket: CAM_A, CAM_B, CAM_C, or CAM_D.")
    return value


def validate_resolution_pair(cameras):
    """Avoid the mixed-mode A/D failure reproduced on OAK-FFC 4P with v2.30.

    Keep actual sensor modes explicit: silently scaling a different sensor mode
    would change the meaning and dimensions of RAW captures.
    """
    resolutions = {camera["socket"]: camera["resolution"] for camera in cameras}
    if {"CAM_A", "CAM_D"} <= resolutions.keys() and resolutions["CAM_A"] != resolutions["CAM_D"]:
        raise ValueError(
            "CAM_A and CAM_D must use the same sensor resolution with this DepthAI 2.30 pipeline. "
            "Mixed sensor modes can stall previews or crash the device. "
            "Use Match resolutions, or select the same mode on both cameras (for example, 4K at 10 FPS)."
        )


def validate_config(value):
    value = object_body(value)
    if set(value) - {"cameras", "raw_enabled"}:
        raise ValueError("Unknown pipeline configuration field.")
    cameras = value.get("cameras")
    if not isinstance(cameras, list) or not 1 <= len(cameras) <= 3:
        raise ValueError("Select one, two, or three detected cameras.")
    raw = value.get("raw_enabled", False)
    if not isinstance(raw, bool):
        raise ValueError("raw_enabled must be true or false.")
    validated = []
    for camera in cameras:
        object_body(camera)
        if set(camera) - {"socket", "resolution", "fps"}:
            raise ValueError("Unknown camera configuration field.")
        socket = socket_name(camera.get("socket"))
        resolution = camera.get("resolution", "1080p")
        if not isinstance(resolution, str) or resolution not in RESOLUTIONS:
            raise ValueError("Choose 1080p, 4k, or 12mp resolution.")
        fps = number(camera.get("fps", 10), "FPS", 2, 30, integer=False)
        validated.append({"socket": socket, "resolution": resolution, "fps": fps})
    if len({c["socket"] for c in validated}) != len(validated):
        raise ValueError("Each camera socket may be selected only once.")
    validate_resolution_pair(validated)
    return {"cameras": validated, "raw_enabled": raw}


def validate_controls(value):
    value = object_body(value)
    if not value or set(value) - (ENUMS.keys() | RANGES.keys() | BOOLEANS):
        raise ValueError("Unknown or empty camera controls.")
    result = {}
    for key, v in value.items():
        if key in BOOLEANS:
            if not isinstance(v, bool):
                raise ValueError(f"{key} must be true or false.")
            result[key] = v
        elif key in ENUMS:
            if not isinstance(v, str) or v not in ENUMS[key]:
                raise ValueError(f"Invalid {key} value.")
            result[key] = v
        else:
            result[key] = number(v, key, *RANGES[key])
    return result


def validate_capture(value):
    value = object_body(value)
    if set(value) - {"sockets", "format"}:
        raise ValueError("Unknown capture field.")
    sockets = value.get("sockets")
    if not isinstance(sockets, list) or not 1 <= len(sockets) <= 3:
        raise ValueError("Choose one to three cameras to capture.")
    sockets = [socket_name(s) for s in sockets]
    if len(set(sockets)) != len(sockets):
        raise ValueError("Capture sockets must be unique.")
    fmt = value.get("format", "jpeg")
    if not isinstance(fmt, str) or fmt not in FORMATS:
        raise ValueError("Choose jpeg, png, tiff, bmp, or raw.")
    return sockets, fmt
