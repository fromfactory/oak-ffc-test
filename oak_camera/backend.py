"""Thread-safe camera access for DepthAI 2.30 and an explicit synthetic demo.

The worker owns the device and all host queues. Web clients only read a cached
JPEG, so a slow browser cannot stall the camera pipeline or accumulate frames.
Still images are requested separately at the selected sensor-mode resolution.
"""

from __future__ import annotations

import copy
import importlib
import math
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future
from datetime import datetime, timezone

from .validation import validate_controls, validate_resolution_pair


RESOLUTIONS = {"1080p": (1920, 1080), "4k": (3840, 2160), "12mp": (4056, 3040)}
DEFAULT_CONTROLS = {
    "exposure_mode": "auto", "exposure_us": 10000, "iso": 400,
    "white_balance_mode": "auto", "white_balance_kelvin": 4500,
    "focus_mode": "continuous", "focus": 128, "exposure_compensation": 0,
    "brightness": 0, "contrast": 0, "saturation": 0, "sharpness": 1,
    "luma_denoise": 1, "chroma_denoise": 1, "anti_banding": "auto",
    "exposure_lock": False, "white_balance_lock": False,
    "auto_exposure_limit_us": 0, "effect_mode": "off",
}
_WHITE_BALANCE_MODES = {
    "auto": "AUTO", "incandescent": "INCANDESCENT", "fluorescent": "FLUORESCENT",
    "warm_fluorescent": "WARM_FLUORESCENT", "daylight": "DAYLIGHT", "cloudy": "CLOUDY_DAYLIGHT",
    "twilight": "TWILIGHT", "shade": "SHADE",
}


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _socket_name(value):
    name = getattr(value, "name", str(value).split(".")[-1])
    return {"RGB": "CAM_A", "LEFT": "CAM_B", "RIGHT": "CAM_C", "CAM_AA": "CAM_A"}.get(name, name)


def _label(socket):
    return socket


class CameraBackend:
    """A single OAK device, with independent settings for each selected camera."""

    def __init__(self, demo=False, device_id=None):
        self.demo = bool(demo)
        self.device_id = device_id
        self._lock = threading.RLock()
        self._commands = queue.Queue()
        self._closed = False
        self._exit = False
        self._running = False
        self._error = None
        self._device = None
        self._device_info = None
        self._cameras = {}
        self._warnings = []
        self._config = {"cameras": [], "raw_enabled": False}
        self._queues = {}
        self._previews = {}
        self._times = {}
        self._last_frames = {}
        self._started_at = 0.0
        self._serial = 0
        self._dai = None
        self._cv2 = None
        self._np = None
        self._thread = threading.Thread(target=self._work, name="oak-camera", daemon=True)
        self._thread.start()

    def _request(self, operation, *args):
        with self._lock:
            if self._closed:
                raise RuntimeError("The camera backend is closed.")
            future = Future()
            self._commands.put((operation, args, future))
        # Booting a USB device can take several seconds. Captures have a separate
        # shorter deadline inside the worker, with live preview polling meanwhile.
        try:
            return future.result(timeout=120)
        except TimeoutError as exc:
            future.cancel()  # Prevent an operation still in the queue running later.
            raise RuntimeError("Camera operation timed out. Check USB and device power.") from exc

    def scan(self):
        return self._request("scan")

    def start(self, config):
        return self._request("start", copy.deepcopy(config))

    def stop(self):
        return self._request("stop")

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            future = Future()
            self._commands.put(("close", (), future))
        try:
            future.result(timeout=30)
        finally:
            self._thread.join(timeout=5)

    def status(self):
        now = time.monotonic()
        with self._lock:
            cameras = copy.deepcopy(list(self._cameras.values()))
            for camera in cameras:
                socket = camera["socket"]
                last = self._last_frames.get(socket)
                camera["last_frame_age"] = round(now - last, 3) if last is not None else None
                times = self._times.get(socket, ())
                recent = [stamp for stamp in times if now - stamp <= 5]
                camera["fps"] = round((len(recent) - 1) / (recent[-1] - recent[0]), 2) if len(recent) > 1 else 0.0
            return {
                "running": self._running, "error": self._error,
                "device": copy.deepcopy(self._device_info), "cameras": cameras,
                "warnings": list(self._warnings), "demo": self.demo,
                "raw_enabled": self._config["raw_enabled"],
                "config": copy.deepcopy(self._config),
            }

    def preview(self, socket):
        with self._lock:
            return self._previews.get(_socket_name(socket))

    def controls(self, socket, settings):
        return self._request("controls", _socket_name(socket), copy.deepcopy(settings))

    def capture(self, socket, format):
        return self._request("capture", _socket_name(socket), str(format).lower())

    def _work(self):
        while not self._exit:
            try:
                operation, args, future = self._commands.get(timeout=0.015)
            except queue.Empty:
                operation = None
            if operation:
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = getattr(self, "_do_" + operation)(*args)
                except Exception as exc:
                    if not isinstance(exc, (ValueError, RuntimeError)):
                        exc = RuntimeError(str(exc))
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            if self._running:
                try:
                    self._poll()
                except Exception as exc:
                    self._fail(str(exc))

    def _image_support(self):
        if self._cv2 is None:
            try:
                self._cv2 = importlib.import_module("cv2")
                self._np = importlib.import_module("numpy")
            except ImportError as exc:
                raise RuntimeError("Install the application requirements (OpenCV and NumPy are required).") from exc

    def _open_device(self):
        if self._device is not None:
            return
        try:
            if self._dai is None:
                self._dai = importlib.import_module("depthai")
        except ImportError as exc:
            raise RuntimeError("DepthAI is not installed. Install requirements.txt, or launch with --demo.") from exc
        if not str(self._dai.__version__).startswith("2."):
            raise RuntimeError("This application requires DepthAI 2.30.0.0. Install the pinned requirements.")
        devices = self._dai.Device.getAllAvailableDevices()
        if self.device_id:
            devices = [d for d in devices if d.getMxId() == self.device_id or d.name == self.device_id]
        if not devices:
            connected = self._dai.XLinkConnection.getAllConnectedDevices(skipInvalidDevices=False)
            if any("INSUFFICIENT_PERMISSIONS" in str(d.status) for d in connected):
                raise RuntimeError("OAK USB access was denied. Install the udev rule shown in README.md, then unplug and reconnect USB.")
            raise RuntimeError("No available OAK device found. Check USB, power and udev permissions; close other camera applications.")
        if len(devices) > 1:
            raise RuntimeError("Multiple OAK devices found. Restart with --device-id and the desired device MX ID.")
        self._device = self._dai.Device(devices[0])

    def _camera_record(self, socket, sensor, width, height, autofocus):
        defaults = copy.deepcopy(DEFAULT_CONTROLS)
        if not autofocus:
            defaults["focus_mode"] = "manual"
        return {
            "socket": socket, "label": _label(socket), "sensor": sensor,
            "width": width, "height": height, "autofocus": autofocus,
            "active": False, "frames": 0, "fps": 0.0, "last_frame_age": None,
            "metadata": {}, "controls": defaults, "resolution": "1080p", "requested_fps": 10,
        }

    def _do_scan(self):
        if self._running:
            state = self.status()
            return {key: state[key] for key in ("device", "cameras", "warnings", "demo")}
        try:
            if self.demo:
                device_info = {"id": "DEMO", "name": "Synthetic OAK-FFC 4P demo", "usb_speed": "DEMO"}
                discovered = [self._camera_record(s, "IMX378 (simulated)", 4056, 3040, True)
                              for s in ("CAM_A", "CAM_B", "CAM_D")]
                warnings = ["Demo mode: all images and camera readings are synthetic; no hardware is accessed."]
            else:
                self._open_device()
                dev = self._device
                info = dev.getDeviceInfo()
                device_info = {"id": dev.getMxId(), "name": dev.getDeviceName() or info.name,
                               "usb_speed": dev.getUsbSpeed().name}
                discovered = [self._camera_record(_socket_name(f.socket), f.sensorName,
                                                  int(f.width), int(f.height),
                                                  bool(getattr(f, "hasAutofocusIC", f.hasAutofocus)))
                              for f in dev.getConnectedCameraFeatures()]
                warnings = []
                if device_info["usb_speed"] not in ("SUPER", "SUPER_PLUS"):
                    warnings.append("USB 2 connection detected. Use a USB 3 host port and a USB 3 cable for faster captures.")
                if not discovered:
                    warnings.append("No camera sensors were detected. Power off before checking FFC cables and socket orientation.")
            with self._lock:
                self._device_info = device_info
                self._cameras = {cam["socket"]: cam for cam in discovered}
                self._warnings = warnings
                self._error = None
                self._last_frames.clear()
                self._times.clear()
            return {"device": copy.deepcopy(device_info), "cameras": copy.deepcopy(discovered),
                    "warnings": list(warnings), "demo": self.demo}
        except Exception as exc:
            self._fail(str(exc))
            raise

    def _validate_config(self, config):
        if not isinstance(config, dict) or not isinstance(config.get("cameras"), list):
            raise ValueError("Choose between one and three cameras.")
        if not 1 <= len(config["cameras"]) <= 3:
            raise ValueError("Choose between one and three cameras.")
        if not isinstance(config.get("raw_enabled", False), bool):
            raise ValueError("raw_enabled must be a boolean.")
        selected = []
        for cam in config["cameras"]:
            if not isinstance(cam, dict):
                raise ValueError("Invalid camera configuration.")
            socket = _socket_name(cam.get("socket", ""))
            resolution = cam.get("resolution", "1080p")
            fps = cam.get("fps", 10)
            if socket not in self._cameras:
                raise ValueError(f"{socket} was not detected. Scan the connected cameras first.")
            if "IMX378" not in self._cameras[socket]["sensor"].upper():
                raise ValueError(f"{socket}: this application currently supports IMX378 sensor modes only.")
            if socket in [item["socket"] for item in selected]:
                raise ValueError("Each camera socket may only be selected once.")
            if not isinstance(resolution, str) or resolution not in RESOLUTIONS:
                raise ValueError("Resolution must be 1080p, 4k or 12mp.")
            if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not 2 <= fps <= 30:
                raise ValueError("Frame rate must be between 2 and 30 FPS.")
            selected.append({"socket": socket, "resolution": resolution, "fps": fps})
        validate_resolution_pair(selected)
        return {"cameras": selected, "raw_enabled": config.get("raw_enabled", False)}

    def _do_start(self, config):
        # Validate before stopping an existing pipeline, to leave it usable after
        # a typo or an invalid configuration request.
        if not self._cameras:
            self._do_scan()
        config = self._validate_config(config)
        if self._running:
            self._do_stop()
        self._image_support()
        try:
            if not self.demo:
                self._open_device()
                pipeline = self._make_pipeline(config)
                self._device.startPipeline(pipeline)
                self._queues = {}
                for cam in config["cameras"]:
                    socket = cam["socket"]
                    self._queues[socket] = {
                        "preview": self._device.getOutputQueue(socket + "_preview", maxSize=1, blocking=False),
                        "still": self._device.getOutputQueue(socket + "_still", maxSize=1, blocking=False),
                        "control": self._device.getInputQueue(socket + "_control", maxSize=4, blocking=False),
                    }
                    if config["raw_enabled"]:
                        self._queues[socket]["raw"] = self._device.getOutputQueue(socket + "_raw", maxSize=1, blocking=False)
                        self._queues[socket]["raw_trigger"] = self._device.getInputQueue(socket + "_raw_trigger", maxSize=1, blocking=False)
            with self._lock:
                self._config = config
                self._error = None
                self._previews.clear()
                self._last_frames.clear()
                self._times.clear()
                self._warnings = [w for w in self._warnings if not w.startswith(("High sensor", "CAM_B and CAM_C"))]
                for camera in self._cameras.values():
                    camera["active"] = False
                for selected in config["cameras"]:
                    socket = selected["socket"]
                    cam = self._cameras[socket]
                    cam.update(active=True, frames=0, metadata={}, resolution=selected["resolution"],
                               requested_fps=selected["fps"], controls=copy.deepcopy(DEFAULT_CONTROLS))
                    if not cam["autofocus"]:
                        cam["controls"]["focus_mode"] = "manual"
                    self._times[socket] = deque(maxlen=150)
                pixels_per_second = sum(math.prod(RESOLUTIONS[c["resolution"]]) * c["fps"] for c in config["cameras"])
                if pixels_per_second > 450_000_000:
                    self._warnings.append("High sensor throughput requested. If frames stall or FPS falls, reduce resolution or FPS.")
                sockets = {c["socket"] for c in config["cameras"]}
                if {"CAM_B", "CAM_C"} <= sockets:
                    self._warnings.append("CAM_B and CAM_C can share an I2C bus on OAK-FFC 4P. Identical sensors may have coupled controls; prefer CAM_A + CAM_D + CAM_B or CAM_C.")
                self._started_at = time.monotonic()
                self._running = True
            return self.status()
        except Exception as exc:
            self._fail(str(exc))
            raise RuntimeError(f"Could not start cameras: {exc}. Try 1080p at 10 FPS and check power and FFC connections.") from exc

    def _make_pipeline(self, config):
        dai = self._dai
        pipeline = dai.Pipeline()
        modes = {"1080p": dai.ColorCameraProperties.SensorResolution.THE_1080_P,
                 "4k": dai.ColorCameraProperties.SensorResolution.THE_4_K,
                 "12mp": dai.ColorCameraProperties.SensorResolution.THE_12_MP}
        for selected in config["cameras"]:
            socket = selected["socket"]
            cam = pipeline.create(dai.node.ColorCamera)
            cam.setBoardSocket(getattr(dai.CameraBoardSocket, socket))
            cam.setResolution(modes[selected["resolution"]])
            cam.setFps(selected["fps"])
            width, height = RESOLUTIONS[selected["resolution"]]
            # v2's native 12 MP NV12 still width is 4032 (sensor RAW is 4056).
            # Preserve that alignment instead of claiming every output is RAW size.
            if selected["resolution"] != "12mp":
                cam.setStillSize(width, height)
            preview_size = (640, 480 if selected["resolution"] == "12mp" else 360)
            cam.setPreviewSize(*preview_size)
            cam.setInterleaved(True)
            cam.setColorOrder(dai.ColorCameraProperties.ColorOrder.BGR)
            cam.setPreviewKeepAspectRatio(False)
            # Keep native capture buffers bounded; high-resolution combinations
            # still need verification against the device's available memory.
            cam.setNumFramesPool(3, 2, 2, 1, 1)
            cam.setRawOutputPacked(True)
            self._control_message(DEFAULT_CONTROLS, DEFAULT_CONTROLS,
                                  self._cameras[socket]["autofocus"], cam.initialControl,
                                  frame_period_us=int(1_000_000 / selected["fps"]))
            control_in = pipeline.create(dai.node.XLinkIn)
            control_in.setStreamName(socket + "_control")
            # XLinkIn defaults to 8 x 5 MiB, even for tiny control messages.
            # Reserve small buffers so camera frames can use the device RAM.
            control_in.setMaxDataSize(1024)
            control_in.setNumFrames(4)
            control_in.out.link(cam.inputControl)
            for kind in ("preview", "still"):
                output = pipeline.create(dai.node.XLinkOut)
                output.setStreamName(socket + "_" + kind)
                output.input.setQueueSize(1)
                output.input.setBlocking(False)
                getattr(cam, kind).link(output.input)
            if config["raw_enabled"]:
                trigger = pipeline.create(dai.node.XLinkIn)
                trigger.setStreamName(socket + "_raw_trigger")
                trigger.setMaxDataSize(1024)
                trigger.setNumFrames(4)
                gate = pipeline.create(dai.node.Script)
                gate.inputs["raw"].setQueueSize(1)
                gate.inputs["raw"].setBlocking(False)
                gate.inputs["trigger"].setQueueSize(1)
                gate.inputs["trigger"].setBlocking(False)
                cam.raw.link(gate.inputs["raw"])
                trigger.out.link(gate.inputs["trigger"])
                # The raw queue continually replaces its one retained frame on
                # device. On request discard that frame and forward the next one.
                gate.setScript("""
while True:
    request = node.io['trigger'].get()
    stale = node.io['raw'].tryGet()
    stale = None
    frame = node.io['raw'].get()
    node.io['out'].send(frame)
    frame = None
""")
                output = pipeline.create(dai.node.XLinkOut)
                output.setStreamName(socket + "_raw")
                output.input.setQueueSize(1)
                output.input.setBlocking(False)
                gate.outputs["out"].link(output.input)
        return pipeline

    def _do_stop(self):
        with self._lock:
            self._running = False
            self._previews.clear()
            for camera in self._cameras.values():
                camera["active"] = False
        device, self._device = self._device, None
        self._queues.clear()
        if device is not None:
            device.close()
        return self.status()

    def _do_close(self):
        try:
            return self._do_stop()
        finally:
            self._exit = True

    def _fail(self, message):
        with self._lock:
            self._error = message
        try:
            self._do_stop()
        except Exception:
            pass

    def _active_camera(self, socket):
        if not self._running:
            raise RuntimeError(self._error or "Start a camera configuration first.")
        if socket not in self._cameras or not self._cameras[socket]["active"]:
            raise ValueError(f"{socket} is not active in this configuration.")
        return self._cameras[socket]

    def _validate_controls(self, camera, settings):
        settings = validate_controls(settings)
        if not camera["autofocus"] and ({"focus_mode", "focus"} & settings.keys()):
            raise ValueError("This camera reports a fixed-focus lens; focus controls are unavailable.")
        merged = {**DEFAULT_CONTROLS, **camera["controls"], **settings}
        frame_period = int(1_000_000 / camera["requested_fps"])
        if merged["exposure_mode"] == "manual" and merged["exposure_us"] > frame_period:
            raise ValueError("Exposure must not exceed the selected frame period. Lower FPS or shorten exposure.")
        if merged["auto_exposure_limit_us"] > frame_period:
            raise ValueError("Auto exposure limit must not exceed the selected frame period. Use 0 for the frame-period limit.")
        for mode, lock in (("exposure_mode", "exposure_lock"), ("white_balance_mode", "white_balance_lock")):
            if merged[mode] != "auto":
                if settings.get(lock) is True:
                    raise ValueError(f"{lock} is available only when {mode} is auto.")
                # A mode transition must not leave a previous automatic lock
                # latent in either requested state or the device's control state.
                merged[lock] = False
        return merged

    def _control_message(self, settings, merged, autofocus, target=None, *, frame_period_us=None):
        dai = self._dai
        ctrl = target if target is not None else dai.CameraControl()
        if {"exposure_mode", "exposure_us", "iso"} & settings.keys():
            if merged["exposure_mode"] == "manual":
                ctrl.setManualExposure(merged["exposure_us"], merged["iso"])
            else:
                ctrl.setAutoExposureEnable()
        if merged["exposure_mode"] == "auto" and {"exposure_mode", "auto_exposure_limit_us"} & settings.keys():
            limit = merged["auto_exposure_limit_us"] or frame_period_us
            if not limit:
                raise ValueError("A frame period is required for the automatic exposure limit.")
            ctrl.setAutoExposureLimit(limit)
        if {"exposure_mode", "exposure_us", "iso", "exposure_lock"} & settings.keys():
            ctrl.setAutoExposureLock(merged["exposure_lock"])
        if {"white_balance_mode", "white_balance_kelvin"} & settings.keys():
            if merged["white_balance_mode"] == "manual":
                ctrl.setManualWhiteBalance(merged["white_balance_kelvin"])
            else:
                ctrl.setAutoWhiteBalanceMode(getattr(dai.CameraControl.AutoWhiteBalanceMode,
                                                     _WHITE_BALANCE_MODES[merged["white_balance_mode"]]))
        if {"white_balance_mode", "white_balance_kelvin", "white_balance_lock"} & settings.keys():
            ctrl.setAutoWhiteBalanceLock(merged["white_balance_lock"])
        if autofocus and ({"focus_mode", "focus"} & settings.keys()):
            if merged["focus_mode"] == "manual":
                ctrl.setManualFocus(merged["focus"])
            elif merged["focus_mode"] == "auto":
                ctrl.setAutoFocusMode(dai.CameraControl.AutoFocusMode.AUTO)
                ctrl.setAutoFocusTrigger()
            else:
                ctrl.setAutoFocusMode(dai.CameraControl.AutoFocusMode.CONTINUOUS_VIDEO)
        setters = {
            "exposure_compensation": "setAutoExposureCompensation", "brightness": "setBrightness",
            "contrast": "setContrast", "saturation": "setSaturation", "sharpness": "setSharpness",
            "luma_denoise": "setLumaDenoise", "chroma_denoise": "setChromaDenoise",
        }
        for name, setter in setters.items():
            if name in settings:
                getattr(ctrl, setter)(merged[name])
        if "anti_banding" in settings:
            modes = {"off": "OFF", "50hz": "MAINS_50_HZ", "60hz": "MAINS_60_HZ", "auto": "AUTO"}
            ctrl.setAntiBandingMode(getattr(dai.CameraControl.AntiBandingMode, modes[merged["anti_banding"]]))
        if "effect_mode" in settings:
            ctrl.setEffectMode(getattr(dai.CameraControl.EffectMode, merged["effect_mode"].upper()))
        return ctrl

    def _do_controls(self, socket, settings):
        camera = self._active_camera(socket)
        merged = self._validate_controls(camera, settings)
        if not self.demo:
            try:
                self._queues[socket]["control"].send(self._control_message(
                    settings, merged, camera["autofocus"],
                    frame_period_us=int(1_000_000 / camera["requested_fps"]),
                ))
            except Exception as exc:
                self._fail(str(exc))
                raise RuntimeError(f"Could not send camera controls: {exc}") from exc
        with self._lock:
            camera["controls"] = merged
        # These are requested settings. Exposure, ISO, lens and WB readbacks are
        # separately recorded from ImgFrame metadata as subsequent frames arrive.
        return copy.deepcopy(merged)

    def _frame_metadata(self, frame):
        metadata = {"received_at": _utc_now(), "demo": False}
        methods = {
            "width": "getWidth", "height": "getHeight", "sequence": "getSequenceNum",
            "iso": "getSensitivity", "lens_position": "getLensPosition",
            "white_balance_kelvin": "getColorTemperature", "row_stride_bytes": "getStride",
        }
        for key, method in methods.items():
            try:
                metadata[key] = int(getattr(frame, method)())
            except (AttributeError, TypeError, RuntimeError):
                metadata[key] = None
        for key, method, scale in (("exposure_us", "getExposureTime", 1_000_000),
                                   ("timestamp_device_s", "getTimestampDevice", 1)):
            try:
                metadata[key] = getattr(frame, method)().total_seconds() * scale
            except (AttributeError, TypeError, RuntimeError):
                metadata[key] = None
        try:
            metadata["frame_type"] = frame.getType().name
        except (AttributeError, RuntimeError):
            metadata["frame_type"] = str(frame.getType())
        return metadata

    def _record_preview(self, socket, frame, metadata, now):
        ok, encoded = self._cv2.imencode(".jpg", frame, [self._cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            raise RuntimeError(f"Could not encode {socket} preview.")
        with self._lock:
            self._serial += 1
            self._previews[socket] = (self._serial, encoded.tobytes())
            self._last_frames[socket] = now
            self._times[socket].append(now)
            self._cameras[socket]["frames"] += 1
            self._cameras[socket]["metadata"] = metadata

    def _poll(self, check_stall=True):
        now = time.monotonic()
        if not self.demo and self._device.isClosed():
            raise RuntimeError("OAK device disconnected. Check power and USB, then scan and start again.")
        for selected in self._config["cameras"]:
            socket = selected["socket"]
            camera = self._cameras[socket]
            if self.demo:
                last = self._last_frames.get(socket, 0)
                if now - last < 1 / camera["requested_fps"]:
                    continue
                height = 480 if camera["resolution"] == "12mp" else 360
                frame = self._demo_frame(camera, 640, height, now)
                metadata = self._demo_metadata(camera, 640, height)
            else:
                message = self._queues[socket]["preview"].tryGet()
                if message is None:
                    if check_stall:
                        last = self._last_frames.get(socket, self._started_at)
                        timeout = 15 if socket not in self._last_frames else 8
                        if now - last > timeout:
                            raise RuntimeError(f"No recent frames from {socket}. Reduce FPS/resolution and check power and FFC cables.")
                    continue
                frame, metadata = message.getCvFrame(), self._frame_metadata(message)
            self._record_preview(socket, frame, metadata, now)

    def _wait_frame(self, socket, kind):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            frame = self._queues[socket][kind].tryGet()
            if frame is not None:
                return frame
            self._poll(check_stall=False)
            time.sleep(0.01)
        raise RuntimeError(f"Timed out waiting for {socket} {kind} capture. Check USB and power; reduce resolution or FPS.")

    def _capture_metadata(self, camera, metadata, format):
        result = {**metadata, "socket": camera["socket"], "sensor": camera["sensor"],
                "resolution": camera["resolution"], "requested_fps": camera["requested_fps"],
                "requested_controls": copy.deepcopy(camera["controls"]),
                "format": format, "device": copy.deepcopy(self._device_info)}
        if camera["resolution"] == "12mp" and format != "raw":
            result["sensor_mode_width"] = 4056
            result["sensor_mode_height"] = 3040
            result["crop_note"] = "DepthAI v2 native processed still output uses width 4032; sensor RAW width is 4056."
        return result

    def _do_capture(self, socket, format):
        camera = self._active_camera(socket)
        if format not in ("jpeg", "jpg", "png", "tiff", "tif", "bmp", "raw"):
            raise ValueError("Capture format must be jpeg, png, tiff, bmp or raw.")
        format = {"jpg": "jpeg", "tif": "tiff"}.get(format, format)
        if format == "raw" and not self._config["raw_enabled"]:
            raise ValueError("Enable RAW and restart the configuration before capturing RAW.")
        if self.demo:
            width, height = RESOLUTIONS[camera["resolution"]]
            if camera["resolution"] == "12mp" and format != "raw":
                width = 4032
            metadata = self._demo_metadata(camera, width, height)
            if format == "raw":
                data = self._demo_raw(width, height)
                metadata.update(frame_type="PACK10", source="synthetic_sensor_pattern")
                metadata = self._raw_metadata(metadata, data)
                return {"data": data, "extension": "raw", "metadata": self._capture_metadata(camera, metadata, format)}
            frame = self._demo_frame(camera, width, height, time.monotonic())
        else:
            kind = "raw" if format == "raw" else "still"
            try:
                # Remove any late response to an earlier timed-out request.
                self._queues[socket][kind].tryGetAll()
                if kind == "raw":
                    trigger = self._dai.Buffer()
                    trigger.setData([1])
                    self._queues[socket]["raw_trigger"].send(trigger)
                else:
                    control = self._dai.CameraControl()
                    control.setCaptureStill(True)
                    self._queues[socket]["control"].send(control)
                message = self._wait_frame(socket, kind)
                metadata = self._frame_metadata(message)
                if kind == "raw":
                    data = message.getData().tobytes()
                    metadata = self._raw_metadata(metadata, data)
                    return {"data": data, "extension": "raw", "metadata": self._capture_metadata(camera, metadata, format)}
                frame = message.getCvFrame()
            except Exception as exc:
                self._fail(str(exc))
                raise RuntimeError(str(exc)) from exc
        extension = {"jpeg": "jpg", "tiff": "tiff"}.get(format, format)
        params = [self._cv2.IMWRITE_JPEG_QUALITY, 95] if format == "jpeg" else []
        ok, encoded = self._cv2.imencode("." + extension, frame, params)
        if not ok:
            raise RuntimeError(f"OpenCV could not encode {format}.")
        metadata.update(width=int(frame.shape[1]), height=int(frame.shape[0]),
                        source="synthetic_still" if self.demo else "camera_still",
                        encoding="8-bit processed BGR converted to " + format,
                        byte_count=int(encoded.size), bit_depth=8)
        return {"data": encoded.tobytes(), "extension": extension,
                "metadata": self._capture_metadata(camera, metadata, format)}

    def _raw_metadata(self, metadata, data):
        width, height = metadata.get("width"), metadata.get("height")
        frame_type = str(metadata.get("frame_type", "")).split(".")[-1]
        # DepthAI distinguishes packed MIPI samples (PACK10/PACK12) from
        # RAW10/12/14, whose values occupy 16-bit words. Preserve the payload
        # exactly and describe its storage instead of guessing from bit depth.
        layouts = {
            "PACK10": (10, 10, True, "MIPI_RAW10"),
            "PACK12": (12, 12, True, "MIPI_RAW12"),
            "RAW8": (8, 8, False, "UINT8"),
            "RAW10": (10, 16, False, "UINT16_LE"),
            "RAW12": (12, 16, False, "UINT16_LE"),
            "RAW14": (14, 16, False, "UINT16_LE"),
            "RAW16": (16, 16, False, "UINT16_LE"),
        }
        layout = layouts.get(frame_type)
        diagnostic = (f"frame_type={metadata.get('frame_type')!r}, width={width!r}, "
                      f"height={height!r}, bytes={len(data)}")
        if (layout is None or any(isinstance(size, bool) or not isinstance(size, int) or size <= 0
                                 for size in (width, height))):
            raise RuntimeError(f"RAW frame has unsupported or missing layout metadata ({diagnostic}).")
        bits, storage_bits, packed, packing = layout
        row_bytes = (width * storage_bits + 7) // 8
        reported_stride = metadata.get("row_stride_bytes")
        stride = reported_stride or (len(data) // height if len(data) % height == 0 else row_bytes)
        if (isinstance(stride, bool) or not isinstance(stride, int)
                or stride < row_bytes or len(data) != stride * height):
            raise RuntimeError(f"RAW payload does not match its layout ({diagnostic}, stride={stride}); capture refused to avoid an ambiguous file.")
        metadata.update(
            source="synthetic_sensor_pattern" if self.demo else "camera_raw",
            bit_depth=bits, byte_count=len(data), row_stride_bytes=stride,
            stride_source="ImgFrame.getStride" if reported_stride else "payload length / height",
            active_row_bytes=row_bytes, row_padding_bytes=stride - row_bytes,
            packing=packing, packed=packed, cfa_pattern=None,
            cfa_pattern_note="CFA order is not exposed by DepthAI v2 ImgFrame; determine it from sensor orientation before demosaicing.",
        )
        if frame_type == "PACK10":
            metadata["unpacking"] = "Each group of 5 bytes b0..b4 gives pixels (b0<<2)|(b4&3), (b1<<2)|((b4>>2)&3), (b2<<2)|((b4>>4)&3), (b3<<2)|((b4>>6)&3). Skip row padding."
        elif not packed:
            metadata["sample_storage_bits"] = storage_bits
            metadata["unpacking"] = f"Read {packing} samples with {bits} valid least-significant bits. Skip row padding."
        return metadata

    def _demo_readback(self, camera):
        controls = camera["controls"]
        if controls["exposure_mode"] == "manual":
            exposure, iso = controls["exposure_us"], controls["iso"]
        else:
            limit = controls["auto_exposure_limit_us"] or int(1_000_000 / camera["requested_fps"])
            exposure = min(10000, limit)
            iso = min(1600, max(100, round(4_000_000 / exposure)))
        # Approximate preset colors for a synthetic demonstration only.
        temperatures = {"auto": 4500, "incandescent": 2800, "fluorescent": 4000,
                        "warm_fluorescent": 3500, "daylight": 5500, "cloudy": 6500,
                        "twilight": 7500, "shade": 8000}
        temperature = (controls["white_balance_kelvin"] if controls["white_balance_mode"] == "manual"
                       else temperatures[controls["white_balance_mode"]])
        return {"exposure_us": exposure, "iso": iso, "white_balance_kelvin": temperature,
                "lens_position": controls["focus"] if controls["focus_mode"] == "manual" else 128}

    def _demo_metadata(self, camera, width, height):
        return {
            "demo": True, "received_at": _utc_now(), "width": width, "height": height,
            "sequence": camera["frames"] + 1, "timestamp_device_s": time.monotonic() - self._started_at,
            **self._demo_readback(camera), "frame_type": "BGR888i",
            "simulation_note": "Demo auto controls and locks use static synthetic readings; they do not verify hardware behavior.",
        }

    def _demo_frame(self, camera, width, height, now):
        np, cv2 = self._np, self._cv2
        # Render the scene at preview size, then scale for synthetic full-mode
        # captures. Demo files are clearly labelled and cannot validate a sensor.
        sw, sh = 640, 480 if camera["resolution"] == "12mp" else 360
        x = np.linspace(0, 1, sw, dtype=np.float32)[None, :]
        y = np.linspace(0, 1, sh, dtype=np.float32)[:, None]
        frame = np.empty((sh, sw, 3), dtype=np.uint8)
        frame[:, :, 0] = (40 + 120 * x + 30 * y).astype(np.uint8)
        frame[:, :, 1] = (30 + 90 * x + 90 * y).astype(np.uint8)
        frame[:, :, 2] = (30 + 140 * y + 20 * x).astype(np.uint8)
        index = list(self._cameras).index(camera["socket"])
        colors = [(80, 220, 80), (220, 150, 50), (100, 90, 240)]
        cv2.rectangle(frame, (35, 90), (200, 245), colors[index % len(colors)], -1)
        for step in range(12):
            cv2.line(frame, (250 + 6 * step, 95), (250 + 6 * step, 240), (255, 255, 255), 2)
        cv2.circle(frame, (440 + int(55 * math.sin(now)), 180), 45, (80, 225, 245), -1)
        controls = camera["controls"]
        readback = self._demo_readback(camera)
        gain = min(4, max(0.05, readback["exposure_us"] * readback["iso"] / 4_000_000))
        gain *= 2 ** (controls["exposure_compensation"] / 6)
        adjusted = frame.astype(np.float32)
        if controls["white_balance_mode"] != "auto":
            warmth = (readback["white_balance_kelvin"] - 4500) / 15000
            adjusted[:, :, 0] *= 1 - warmth
            adjusted[:, :, 2] *= 1 + warmth
        adjusted = (adjusted - 128) * (1 + controls["contrast"] / 15) + 128
        frame = np.clip(adjusted * gain + controls["brightness"] * 8, 0, 255).astype(np.uint8)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        hsv[:, :, 1] = np.clip(hsv[:, :, 1].astype(np.float32) * (1 + controls["saturation"] / 10), 0, 255).astype(np.uint8)
        frame = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        if controls["focus_mode"] == "manual":
            sigma = abs(controls["focus"] - 128) / 20
            if sigma > 0.2:
                frame = cv2.GaussianBlur(frame, (0, 0), sigma)
        if controls["effect_mode"] == "mono":
            frame = cv2.cvtColor(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        elif controls["effect_mode"] == "negative":
            frame = 255 - frame
        elif controls["effect_mode"] == "sepia":
            transform = np.array([[0.131, 0.534, 0.272], [0.168, 0.686, 0.349],
                                  [0.189, 0.769, 0.393]], dtype=np.float32)
            frame = np.clip(cv2.transform(frame.astype(np.float32), transform), 0, 255).astype(np.uint8)
        cv2.putText(frame, "SYNTHETIC DEMO  " + _label(camera["socket"]), (20, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
        cv2.putText(frame, camera["resolution"] + "  " + _utc_now()[11:19], (20, sh - 22), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
        return cv2.resize(frame, (width, height)) if (width, height) != (sw, sh) else frame

    def _demo_raw(self, width, height):
        np = self._np
        # A deterministic, explicitly synthetic 10-bit ramp in authentic MIPI
        # packing, for exercising the download path without inventing sensor data.
        pixels = (np.arange(width, dtype=np.uint16) % 1024).reshape(-1, 4)
        packed = np.empty((width // 4, 5), dtype=np.uint8)
        packed[:, :4] = pixels >> 2
        packed[:, 4] = ((pixels[:, 0] & 3) | ((pixels[:, 1] & 3) << 2)
                        | ((pixels[:, 2] & 3) << 4) | ((pixels[:, 3] & 3) << 6))
        return packed.tobytes() * height
