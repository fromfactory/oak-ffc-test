"""HTTP interface. The backend alone owns the USB device."""

from collections import deque
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import platform
import secrets
import threading
import time
from urllib.parse import urlsplit
import weakref

from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from . import __version__
from .diagnostics import build_diagnostics
from .storage import CaptureDeletionError, CaptureNotFound, CaptureStore
from .validation import (object_body, socket_name, validate_capture,
                         validate_capture_selection, validate_config, validate_controls)


def create_app(backend=None, capture_dir="captures", demo=False, device_id=None):
    if backend is None:
        from .backend import CameraBackend
        backend = CameraBackend(demo=demo, device_id=device_id)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    app.config["FRONTEND_DIST"] = Path(app.static_folder) / "dist"
    store = CaptureStore(capture_dir)
    mutations = threading.Lock()
    events = deque(maxlen=100)
    archives = {}
    archive_lock = threading.Lock()
    app.extensions.update(camera_backend=backend, capture_store=store, capture_archives=archives)

    def expire_archive(token):
        with archive_lock:
            pending = archives.pop(token, None)
        if pending:
            pending["file"].close()

    def close_archives():
        with archive_lock:
            pending = list(archives.values())
            archives.clear()
        for item in pending:
            item["timer"].cancel()
            item["file"].close()

    weakref.finalize(app, close_archives)

    def event(action, **details):
        events.append({"time": datetime.now(timezone.utc).isoformat(),
                       "action": action, **details})

    def status():
        captures, count = store.snapshot()
        return {**backend.status(), "demo": demo, "captures": captures, "capture_count": count,
                "capture_directory": str(store.root), "version": __version__}

    def body():
        if not request.is_json:
            raise ValueError("Use application/json for camera requests.")
        return object_body(request.get_json())

    @app.before_request
    def check_origin():
        if request.path in {"/api/captures/delete", "/api/captures/download"}:
            request.max_content_length = 1024 * 1024
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                return jsonify(error="Cross-origin camera changes are disabled."), 403

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.path.startswith(("/api/", "/stream/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, HTTPException):
            code = exc.code
        elif isinstance(exc, CaptureNotFound):
            code = 404
        elif isinstance(exc, ValueError):
            code = 400
        elif isinstance(exc, (RuntimeError, TimeoutError)):
            code = 409
        elif isinstance(exc, OSError):
            code = 507
        else:
            logging.exception("Application error")
            code = 500
        message = str(exc) if code != 500 else "Unexpected application error; check the terminal log."
        event("error", error=message, path=request.path)
        return jsonify(error=message), code

    @app.get("/")
    def index():
        # Vite writes all scripts and styles with same-origin /static/dist/ URLs.
        # The API remains usable when a fresh checkout has not been built yet.
        frontend = Path(app.config["FRONTEND_DIST"])
        if not (frontend / "index.html").is_file():
            return Response(
                "The camera interface has not been built yet.\n"
                "From the project directory, run npm ci && npm run build, "
                "or bash scripts/install.sh. Then reload this page.\n"
                "Node.js 22.12+ (22.x) or 24+ is required to build the interface.\n",
                status=503, mimetype="text/plain",
                headers={"Cache-Control": "no-store"},
            )
        response = send_from_directory(frontend, "index.html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/status")
    def get_status():
        return jsonify(status())

    @app.post("/api/scan")
    def scan():
        body()
        with mutations:
            if backend.status().get("running"):
                raise RuntimeError("Stop the cameras before scanning again.")
            backend.scan()
            event("scan", cameras=backend.status().get("cameras", []))
        return jsonify(status())

    @app.post("/api/start")
    def start():
        config = validate_config(body())
        with mutations:
            backend.start(config)
            event("start", config=config)
        return jsonify(status())

    @app.post("/api/stop")
    def stop():
        body()
        with mutations:
            before = backend.status()
            backend.stop()
            event("stop", cameras=before.get("cameras", []))
        return jsonify(status())

    @app.post("/api/controls/<socket>")
    def controls(socket):
        socket_name(socket)
        settings = validate_controls(body())
        with mutations:
            result = backend.controls(socket, settings)
            event("controls", socket=socket, requested=result)
        return jsonify(result)

    @app.post("/api/capture")
    def capture():
        sockets, fmt = validate_capture(body())
        records = []
        with mutations:
            current = backend.status()
            if not current.get("running"):
                raise RuntimeError("Start the cameras before capturing an image.")
            active = {c["socket"] for c in current.get("cameras", []) if c.get("active")}
            if set(sockets) - active:
                raise ValueError("Every selected capture camera must be running.")
            if fmt == "raw" and not current.get("raw_enabled"):
                raise ValueError("Stop and enable RAW capture before starting the cameras.")
            for socket in sockets:
                try:
                    result = backend.capture(socket, fmt)
                    record = store.save(socket, fmt, result, demo=demo)
                    records.append(record)
                    event("capture", socket=socket, format=fmt, id=record["id"])
                except Exception as exc:
                    event("capture_failed", socket=socket, error=str(exc))
                    return jsonify(error=f"{socket}: {exc}. {len(records)} capture(s) saved; see Recent captures.",
                                   captures=records), 409
        return jsonify(captures=records)

    @app.get("/api/captures")
    def captures():
        return jsonify(captures=store.list(limit=None))

    @app.post("/api/captures/delete")
    def delete_captures():
        ids = validate_capture_selection(body())
        with mutations:
            try:
                deleted = store.delete(ids)
            except CaptureDeletionError as exc:
                remaining, count = store.snapshot(limit=None)
                event("capture_delete_failed", deleted=exc.deleted, id=exc.capture_id, error=str(exc))
                return jsonify(error=str(exc), deleted=exc.deleted, failed=[exc.capture_id],
                               captures=remaining, capture_count=count), 507
            remaining, count = store.snapshot(limit=None)
            event("capture_delete", deleted=deleted)
        return jsonify(deleted=deleted, captures=remaining, capture_count=count)

    def archive_response(archive, filename):
        try:
            # A file object prevents Flask from delegating temporary-file lifetime
            # to X-Sendfile. The WSGI file wrapper and close callback release it.
            response = send_file(archive, mimetype="application/zip", as_attachment=True,
                                 download_name=filename, conditional=False)
            response.content_length = archive.seek(0, 2)
            archive.seek(0)
            response.call_on_close(archive.close)
            return response
        except BaseException:
            archive.close()
            raise

    @app.post("/api/captures/download")
    def download_captures():
        ids = validate_capture_selection(body())
        with mutations:
            archive = store.archive(ids)
            count = len(ids) if ids is not None else store.snapshot()[1]
        filename = "oak-captures-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".zip"
        if request.args.get("prepare") == "1":
            token = secrets.token_urlsafe(32)
            timer = threading.Timer(300, expire_archive, args=(token,))
            timer.daemon = True
            with archive_lock:
                archives[token] = {"file": archive, "filename": filename,
                                   "expires": time.monotonic() + 300, "timer": timer}
            timer.start()
            return jsonify(download_url=f"/api/captures/download/{token}", filename=filename, count=count)
        return archive_response(archive, filename)

    @app.get("/api/captures/download/<token>")
    def download_prepared_captures(token):
        with archive_lock:
            pending = archives.pop(token, None)
        if not pending:
            raise CaptureNotFound("This download expired or has already been used. Prepare it again.")
        pending["timer"].cancel()
        if pending["expires"] <= time.monotonic():
            pending["file"].close()
            raise CaptureNotFound("This download expired. Prepare it again.")
        return archive_response(pending["file"], pending["filename"])

    @app.get("/captures/<capture_id>/thumbnail")
    def thumbnail(capture_id):
        jpeg = store.thumbnail(capture_id)
        return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/captures/<capture_id>/<name>")
    def download(capture_id, name):
        try:
            target = store.file(capture_id, name)
        except (ValueError, CaptureNotFound):
            return jsonify(error="Unknown capture file."), 404
        return send_file(target, as_attachment=True, download_name=f"{capture_id}_{name}")

    @app.get("/stream/<socket>")
    def stream(socket):
        socket_name(socket)

        def frames():
            previous = None
            while True:
                item = backend.preview(socket)
                if item and item[0] != previous:
                    previous, jpeg = item
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                elif not backend.status().get("running"):
                    break
                time.sleep(0.04)
        return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.get("/api/report")
    def report():
        import importlib.metadata
        packages = {}
        for name in ("depthai", "Flask", "numpy", "opencv-python-headless"):
            try:
                packages[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                packages[name] = "not installed"
        value = build_diagnostics(
            backend.status(), list(events), app_version=__version__, packages=packages,
            python_version=platform.python_version(), system=platform.system(), machine=platform.machine(),
        )
        return Response(json.dumps(value, indent=2), mimetype="application/json",
                        headers={"Content-Disposition": 'attachment; filename="oak-camera-report.json"'})

    return app
