"""Exercise HTTP boundaries with an injected backend; never open a USB device."""

from copy import deepcopy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from oak_camera.app import create_app


class FakeBackend:
    def __init__(self):
        self.running = False
        self.raw_enabled = False
        self.calls = []
        self.fail_capture = None
        self.cameras = [
            {"socket": socket, "sensor": "IMX378", "active": False,
             "frames_received": 12, "fps": 9.8, "last_frame_age": 0.05}
            for socket in ("CAM_A", "CAM_D", "CAM_B")
        ]

    def status(self):
        return deepcopy({"running": self.running, "raw_enabled": self.raw_enabled,
                         "cameras": self.cameras, "usb_speed": "SUPER",
                         "error": None})

    def scan(self):
        self.calls.append(("scan",))

    def start(self, config):
        self.calls.append(("start", deepcopy(config)))
        self.running = True
        self.raw_enabled = config["raw_enabled"]
        selected = {camera["socket"] for camera in config["cameras"]}
        for camera in self.cameras:
            camera["active"] = camera["socket"] in selected

    def stop(self):
        self.calls.append(("stop",))
        self.running = False
        for camera in self.cameras:
            camera["active"] = False

    def controls(self, socket, settings):
        self.calls.append(("controls", socket, settings))
        return settings

    def capture(self, socket, fmt):
        self.calls.append(("capture", socket, fmt))
        if socket == self.fail_capture:
            raise TimeoutError("No fresh frame arrived")
        return {"data": b"native-camera-bytes", "extension": "jpg" if fmt == "jpeg" else fmt,
                "metadata": {"width": 1920, "height": 1080,
                             "requested": {"exposure_mode": "auto"},
                             "actual": {"exposure_us": 8000, "sequence": 41}}}


@pytest.fixture
def api(tmp_path):
    backend = FakeBackend()
    app = create_app(backend=backend, capture_dir=tmp_path / "captures", demo=False)
    app.config["TESTING"] = True
    return SimpleNamespace(app=app, client=app.test_client(), backend=backend,
                           root=tmp_path / "captures", tmp=tmp_path)


@pytest.fixture
def frontend(api):
    """Model Vite output without requiring Node for the Python API test suite."""
    api.app.static_folder = str(api.tmp / "static")
    output = api.tmp / "static" / "dist"
    assets = output / "assets"
    assets.mkdir(parents=True)
    (assets / "index-test.js").write_text("console.log('synthetic test asset');")
    (assets / "index-test.css").write_text("body { color: white; }")
    (output / "index.html").write_text(
        '<!doctype html><html lang="en"><head><title>OAK FFC TEST</title>'
        '<link rel="stylesheet" href="/static/dist/assets/index-test.css">'
        '<script type="module" src="/static/dist/assets/index-test.js"></script>'
        '</head><body><div id="root"></div></body></html>'
    )
    api.app.config["FRONTEND_DIST"] = output
    return output


def test_missing_frontend_explains_build_and_keeps_api_available(api):
    api.app.config["FRONTEND_DIST"] = api.tmp / "not-built"
    response = api.client.get("/")
    assert response.status_code == 503
    assert response.mimetype == "text/plain"
    assert b"npm ci && npm run build" in response.data
    assert response.headers["Cache-Control"] == "no-store"
    assert api.client.get("/api/status").status_code == 200
    assert api.backend.calls == []


def test_frontend_document_revalidates_after_a_new_build(api, frontend):
    response = api.client.get("/")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    (frontend / "index.html").write_text('<!doctype html><div id="root">Updated build</div>')
    assert b"Updated build" in api.client.get("/").data


def start(api, sockets=("CAM_A",), raw=False):
    return api.client.post("/api/start", json={
        "cameras": [{"socket": socket, "resolution": "1080p", "fps": 10} for socket in sockets],
        "raw_enabled": raw,
    })


@pytest.mark.parametrize("config", [
    [],
    {"cameras": []},
    {"cameras": [{"socket": "CAM_A"}] * 2},
    {"cameras": [{"socket": socket} for socket in ("CAM_A", "CAM_B", "CAM_C", "CAM_D")]},
    {"cameras": [{"socket": "../../etc"}]},
    {"cameras": [{"socket": ["CAM_A"]}]},
    {"cameras": [{"socket": "CAM_A", "resolution": "13mp"}]},
    {"cameras": [{"socket": "CAM_A", "fps": True}]},
    {"cameras": [{"socket": "CAM_A", "fps": float("nan")}]},
    {"cameras": [{"socket": "CAM_A", "fps": float("inf")}]},
    {"cameras": [{"socket": "CAM_A", "fps": 10 ** 1000}]},
    {"cameras": [{"socket": "CAM_A", "fps": -1}]},
    {"cameras": [{"socket": "CAM_A", "fps": 31}]},
    {"cameras": [{"socket": "CAM_A", "script": "arbitrary code"}]},
    {"cameras": [{"socket": "CAM_A"}], "raw_enabled": "false"},
    {"cameras": [{"socket": "CAM_A"}], "capture_dir": "/etc"},
])
def test_invalid_pipeline_never_reaches_device(api, config):
    response = api.client.post("/api/start", json=config)
    assert response.status_code == 400
    assert "error" in response.json
    assert api.backend.calls == []


@pytest.mark.parametrize("settings", [
    {}, {"iso": True}, {"iso": 99}, {"iso": 1601}, {"iso": 100.5}, {"iso": 10 ** 1000},
    {"focus": 256}, {"exposure_us": 0}, {"white_balance_kelvin": 999},
    {"brightness": float("nan")}, {"focus_mode": "laser"},
    {"anti_banding": []}, {"write_eeprom": True},
])
def test_invalid_controls_never_reach_device(api, settings):
    response = api.client.post("/api/controls/CAM_A", json=settings)
    assert response.status_code == 400
    assert api.backend.calls == []


def test_json_media_type_malformed_and_oversized_requests_are_rejected(api):
    assert api.client.post("/api/start", data="{}").status_code == 400
    assert api.client.post("/api/start", data="{", content_type="application/json").status_code == 400
    oversized = json.dumps({"cameras": [], "padding": "x" * (17 * 1024)})
    assert api.client.post("/api/start", data=oversized,
                           content_type="application/json").status_code == 413
    assert api.backend.calls == []


@pytest.mark.parametrize("origin", ["https://attacker.example", "http://localhost.evil", "null"])
def test_cross_origin_mutations_are_rejected_before_backend_access(api, origin):
    response = api.client.post("/api/stop", json={}, headers={"Origin": origin})
    assert response.status_code == 403
    assert api.backend.calls == []


def test_same_origin_and_originless_local_requests_work(api):
    assert api.client.post("/api/scan", json={}, headers={"Origin": "http://localhost"}).status_code == 200
    assert start(api, ("CAM_A", "CAM_D"), raw=True).status_code == 200
    assert api.backend.calls[1] == ("start", {
        "cameras": [{"socket": "CAM_A", "resolution": "1080p", "fps": 10.0},
                    {"socket": "CAM_D", "resolution": "1080p", "fps": 10.0}],
        "raw_enabled": True,
    })
    settings = {"exposure_mode": "manual", "exposure_us": 8000, "iso": 200}
    assert api.client.post("/api/controls/CAM_D", json=settings).json == settings
    assert api.client.post("/api/scan", json={}).status_code == 409
    assert api.client.post("/api/stop", json={}).status_code == 200
    assert not api.client.get("/api/status").json["running"]


def test_hotspot_browser_can_use_assets_camera_controls_streams_and_downloads(api, frontend, monkeypatch):
    origin = "http://10.42.0.1:8080"
    browser = {"base_url": origin, "headers": {"Origin": origin}}
    page = api.client.get("/", base_url=origin)
    assert page.status_code == 200
    assert b'href="/static/dist/assets/index-test.css"' in page.data
    assert b'src="/static/dist/assets/index-test.js"' in page.data
    for asset in ("/static/dist/assets/index-test.css", "/static/dist/assets/index-test.js"):
        assert api.client.get(asset, base_url=origin).status_code == 200

    assert api.client.post("/api/scan", json={}, **browser).status_code == 200
    config = {"cameras": [{"socket": "CAM_A", "resolution": "1080p", "fps": 10}],
              "raw_enabled": True}
    assert api.client.post("/api/start", json=config, **browser).status_code == 200
    assert api.client.get("/api/status", base_url=origin).json["running"]
    settings = {"exposure_mode": "manual", "exposure_us": 8000, "iso": 200}
    controls = api.client.post("/api/controls/CAM_A", json=settings, **browser)
    assert controls.status_code == 200
    assert controls.json == settings

    jpeg = b"preview-frame-from-camera"
    monkeypatch.setattr(api.backend, "preview", lambda socket: (1, jpeg), raising=False)
    stream = api.client.get("/stream/CAM_A", base_url=origin, buffered=False)
    try:
        assert stream.status_code == 200
        assert stream.mimetype == "multipart/x-mixed-replace"
        assert "boundary=frame" in stream.content_type
        assert stream.headers["Cache-Control"] == "no-store"
        assert next(iter(stream.response)) == (
            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
    finally:
        stream.close()

    response = api.client.post("/api/capture", json={"sockets": ["CAM_A"], "format": "raw"},
                               **browser)
    assert response.status_code == 200
    capture, = response.json["captures"]
    assert api.client.get("/api/captures", base_url=origin).json["captures"] == [capture]
    for file in capture["files"]:
        assert file["url"].startswith(f"/captures/{capture['id']}/")
        download = api.client.get(file["url"], base_url=origin)
        assert download.status_code == 200
        assert download.headers["Content-Disposition"].startswith("attachment;")
        if file["name"] == "image.raw":
            assert download.data == b"native-camera-bytes"
        else:
            assert download.json == capture["metadata"]
    report = api.client.get("/api/report", base_url=origin)
    assert report.status_code == 200
    assert report.json["status"]["running"]
    assert "attachment" in report.headers["Content-Disposition"]
    assert api.client.post("/api/stop", json={}, **browser).status_code == 200
    assert not api.client.get("/api/status", base_url=origin).json["running"]


@pytest.mark.parametrize("origin", ["http://localhost:8080", "http://10.42.0.2:8080",
                                    "http://10.42.0.1:8081"])
def test_hotspot_rejects_mutations_from_another_host_or_port(api, origin):
    response = api.client.post("/api/stop", json={}, base_url="http://10.42.0.1:8080",
                               headers={"Origin": origin})
    assert response.status_code == 403
    assert api.backend.calls == []


def test_capture_requires_running_selected_camera_and_raw_opt_in(api):
    request = {"sockets": ["CAM_A"], "format": "raw"}
    assert api.client.post("/api/capture", json=request).status_code == 409
    assert start(api).status_code == 200
    assert api.client.post("/api/capture", json={"sockets": ["CAM_D"], "format": "png"}).status_code == 400
    response = api.client.post("/api/capture", json=request)
    assert response.status_code == 400
    assert "RAW" in response.json["error"]
    assert not any(call[0] == "capture" for call in api.backend.calls)
    assert api.client.get("/api/captures").json == {"captures": []}


@pytest.mark.parametrize("capture_body", [
    {"sockets": ["CAM_A", "CAM_A"], "format": "png"},
    {"sockets": [], "format": "png"},
    {"sockets": ["CAM_A"], "format": "../outside"},
    {"sockets": ["CAM_A"], "format": "png", "path": "/tmp/outside"},
])
def test_invalid_capture_requests_have_no_side_effects(api, capture_body):
    start(api)
    assert api.client.post("/api/capture", json=capture_body).status_code == 400
    assert not any(call[0] == "capture" for call in api.backend.calls)
    assert list(api.root.iterdir()) == []


def test_capture_downloads_original_bytes_and_metadata(api):
    start(api, raw=True)
    response = api.client.post("/api/capture", json={"sockets": ["CAM_A"], "format": "raw"})
    assert response.status_code == 200
    capture, = response.json["captures"]
    assert capture["metadata"]["actual"]["sequence"] == 41
    assert capture["metadata"]["requested"] == {"exposure_mode": "auto"}
    assert capture["metadata"]["demo"] is False
    files = {file["name"]: file for file in capture["files"]}
    image = api.client.get(files["image.raw"]["url"])
    assert image.status_code == 200
    assert image.data == b"native-camera-bytes"
    assert image.headers["Content-Disposition"].startswith("attachment;")
    assert image.headers["X-Content-Type-Options"] == "nosniff"
    assert api.client.get(files["metadata.json"]["url"]).json == capture["metadata"]
    assert api.client.get("/api/captures").json["captures"] == [capture]


def test_partial_sequential_capture_retains_earlier_success_and_stops(api):
    start(api, ("CAM_A", "CAM_D", "CAM_B"))
    api.backend.fail_capture = "CAM_D"
    response = api.client.post("/api/capture", json={
        "sockets": ["CAM_A", "CAM_D", "CAM_B"], "format": "png",
    })
    assert response.status_code == 409
    saved, = response.json["captures"]
    assert saved["socket"] == "CAM_A"
    assert "1 capture(s) saved" in response.json["error"]
    assert [call for call in api.backend.calls if call[0] == "capture"] == [
        ("capture", "CAM_A", "png"), ("capture", "CAM_D", "png"),
    ]
    assert api.client.get("/api/captures").json["captures"] == [saved]
    assert (api.root / saved["id"] / "image.png").read_bytes() == b"native-camera-bytes"
    report = api.client.get("/api/report").json
    assert report["event_counts"]["capture_failures"] == 1
    assert "events" not in report
    assert "captures" not in report["status"]


def test_downloads_reject_traversal_private_records_and_escaping_symlinks(api):
    outside = api.tmp / "outside"
    outside.mkdir()
    (outside / "image.png").write_bytes(b"private")
    (api.root / "escape").symlink_to(outside, target_is_directory=True)
    folder = api.root / "safe"
    folder.mkdir()
    (folder / "image.png").symlink_to(outside / "image.png")
    hidden = api.root / ".pending-test"
    hidden.mkdir()
    (hidden / "image.png").write_bytes(b"incomplete")
    for url in ("/captures/escape/image.png", "/captures/safe/image.png",
                "/captures/.pending-test/image.png", "/captures/../image.png",
                "/captures/..%2Foutside/image.png", "/captures/safe/record.json"):
        response = api.client.get(url)
        assert response.status_code == 404, url
        assert response.data not in (b"private", b"incomplete")


def test_status_keeps_local_details_and_report_exports_technical_health(api):
    start(api)
    status_response = api.client.get("/api/status")
    assert status_response.headers["Cache-Control"] == "no-store"
    status = status_response.json
    assert status["cameras"][0]["frames_received"] == 12
    assert status["cameras"][0]["fps"] == 9.8
    assert status["cameras"][0]["last_frame_age"] == 0.05
    assert status["usb_speed"] == "SUPER"
    assert status["capture_directory"] == str(api.root.resolve())
    report_response = api.client.get("/api/report")
    assert report_response.status_code == 200
    assert "attachment" in report_response.headers["Content-Disposition"]
    report = report_response.json
    assert report["status"]["running"] == status["running"]
    assert report["status"]["cameras"][0]["fps"] == 9.8
    assert report["status"]["cameras"][0]["last_frame_age"] == 0.05
    assert "capture_directory" not in report["status"]
    assert "captures" not in report["status"]
    assert "events" not in report
    assert report["event_counts"] == {"errors": 0, "capture_failures": 0}
    assert report["python"] and report["platform"] and report["generated_at"]
    assert "depthai" in report["packages"]
    assert "sequential" in report["note"]


def saved(api, count=1, fmt="raw"):
    """Create isolated fixture files directly without starting hardware."""
    store = api.app.extensions["capture_store"]
    return [store.save("CAM_A", fmt, api.backend.capture("CAM_A", fmt)) for _ in range(count)]


def test_capture_library_lists_all_and_status_keeps_recent_fifty_with_total(api):
    records = saved(api, count=56)
    assert api.client.get("/api/captures").json["captures"] == list(reversed(records))
    status = api.client.get("/api/status").json
    assert status["captures"] == list(reversed(records))[:50]
    assert status["capture_count"] == 56
    result = api.client.post("/api/captures/delete", json={"all": True})
    assert result.status_code == 200
    assert set(result.json["deleted"]) == {record["id"] for record in records}
    assert result.json["capture_count"] == 0
    assert result.json["captures"] == []
    assert list(api.root.iterdir()) == []


@pytest.mark.parametrize("endpoint", ["delete", "download"])
@pytest.mark.parametrize("selection", [
    {}, [], {"all": False}, {"all": 1}, {"all": "true"},
    {"ids": []}, {"ids": "capture"}, {"ids": [None]}, {"ids": [1]},
    {"ids": ["../outside"]}, {"ids": [".pending-test"]},
    {"ids": ["id", "id"]}, {"ids": ["id"], "all": True},
    {"ids": ["id"], "path": "arbitrary"}, {"all": True, "ids": []},
])
def test_capture_bulk_endpoints_reject_invalid_selectors_without_changes(api, endpoint, selection):
    record, = saved(api)
    before = set((api.root / record["id"]).iterdir())
    response = api.client.post(f"/api/captures/{endpoint}", json=selection)
    assert response.status_code == 400
    assert set((api.root / record["id"]).iterdir()) == before


@pytest.mark.parametrize("endpoint", ["delete", "download", "download?prepare=1"])
def test_capture_bulk_endpoints_reject_cross_origin_and_unknown_ids(api, endpoint):
    record, = saved(api)
    url = f"/api/captures/{endpoint}"
    assert api.client.post(url, json={"all": True}, headers={"Origin": "https://attacker.example"}).status_code == 403
    assert api.client.post(url, json={"ids": [record["id"], "unknown"]}).status_code == 404
    assert api.client.get("/api/captures").json["captures"] == [record]


def test_subset_delete_retains_other_images_and_sidecars(api):
    records = saved(api, count=3)
    response = api.client.post("/api/captures/delete", json={"ids": [records[0]["id"], records[2]["id"]]})
    assert response.status_code == 200
    assert response.json == {"deleted": [records[0]["id"], records[2]["id"]],
                             "captures": [records[1]], "capture_count": 1}
    assert api.client.get(records[0]["files"][0]["url"]).status_code == 404
    assert api.client.get(records[1]["files"][0]["url"]).data == b"native-camera-bytes"


def test_partial_delete_failure_returns_completed_ids_and_refreshed_library(api, monkeypatch):
    records = saved(api, count=3)
    original = Path.unlink

    def fail(path, *args, **kwargs):
        if path.parent.name == records[1]["id"]:
            raise PermissionError("permission denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    response = api.client.post("/api/captures/delete", json={"ids": [record["id"] for record in records]})
    assert response.status_code == 507
    assert response.json["deleted"] == [records[0]["id"]]
    assert response.json["failed"] == [records[1]["id"]]
    assert response.json["captures"] == list(reversed(records[1:]))
    assert response.json["capture_count"] == 2
    assert "1 capture(s) permanently deleted" in response.json["error"]


@pytest.mark.parametrize("all_captures", [False, True])
def test_download_zip_includes_every_selected_image_metadata_and_no_record(api, all_captures):
    records = saved(api, count=54)
    chosen = records if all_captures else [records[0], records[3], records[53]]
    selection = {"all": True} if all_captures else {"ids": [record["id"] for record in chosen]}
    response = api.client.post("/api/captures/download", json=selection)
    try:
        assert response.status_code == 200
        assert response.mimetype == "application/zip"
        assert "attachment;" in response.headers["Content-Disposition"]
        assert response.headers["Cache-Control"] == "no-store"
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            assert set(archive.namelist()) == {f"{record['id']}/{file['name']}" for record in chosen for file in record["files"]}
            assert archive.read(f"{chosen[0]['id']}/image.raw") == b"native-camera-bytes"
            assert json.loads(archive.read(f"{chosen[0]['id']}/metadata.json")) == chosen[0]["metadata"]
        assert response.content_length == len(response.data)
        assert api.client.get("/api/status").json["capture_count"] == 54
    finally:
        response.close()


def test_prepared_download_streams_once_and_cleans_up_archive(api):
    record, = saved(api)
    prepare = api.client.post("/api/captures/download?prepare=1", json={"ids": [record["id"]]})
    assert prepare.status_code == 200
    assert prepare.json["count"] == 1
    assert prepare.json["filename"].endswith(".zip")
    url = prepare.json["download_url"]
    token = url.rsplit("/", 1)[1]
    archive = api.app.extensions["capture_archives"][token]["file"]
    assert not archive.closed
    response = api.client.get(url)
    try:
        assert response.status_code == 200
        assert response.mimetype == "application/zip"
        assert response.content_length == len(response.data)
        with zipfile.ZipFile(io.BytesIO(response.data)) as contents:
            assert contents.read(f"{record['id']}/image.raw") == b"native-camera-bytes"
        assert api.app.extensions["capture_archives"] == {}
        assert api.client.get(url).status_code == 404
    finally:
        response.close()
    assert archive.closed


def test_expired_download_cleans_up_archive_and_requests_can_be_prepared_again(api):
    saved(api)
    prepare = api.client.post("/api/captures/download?prepare=1", json={"all": True})
    url = prepare.json["download_url"]
    pending = api.app.extensions["capture_archives"][url.rsplit("/", 1)[1]]
    pending["expires"] = 0
    assert api.client.get(url).status_code == 404
    assert pending["file"].closed
    assert api.app.extensions["capture_archives"] == {}


def test_abandoned_prepared_download_timer_closes_archive(api):
    saved(api)
    prepare = api.client.post("/api/captures/download?prepare=1", json={"all": True})
    token = prepare.json["download_url"].rsplit("/", 1)[1]
    pending = api.app.extensions["capture_archives"][token]
    timer = pending["timer"]
    timer.cancel()
    timer.function(*timer.args, **timer.kwargs)
    assert pending["file"].closed
    assert api.app.extensions["capture_archives"] == {}
    assert api.client.get(prepare.json["download_url"]).status_code == 404


@pytest.mark.parametrize("extension", ["jpg", "png", "tiff", "bmp"])
def test_processed_capture_thumbnail_has_small_browser_supported_jpeg(api, extension):
    import cv2
    import numpy as np

    image = np.zeros((600, 1200, 3), dtype=np.uint8)
    image[:, :, 1] = 120
    ok, encoded = cv2.imencode(f".{extension}", image)
    assert ok
    record = api.app.extensions["capture_store"].save(
        "CAM_A", "jpeg" if extension == "jpg" else extension,
        {"extension": extension, "data": encoded.tobytes(), "metadata": {"width": 1200, "height": 600}},
    )
    response = api.client.get(f"/captures/{record['id']}/thumbnail")
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert "attachment" not in response.headers.get("Content-Disposition", "")
    thumbnail = cv2.imdecode(np.frombuffer(response.data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert thumbnail.shape[:2] == (160, 320)
    assert response.headers["Cache-Control"] == "private, max-age=86400"


def test_thumbnail_rejects_raw_invalid_encoded_images_and_symlinks(api):
    raw, = saved(api)
    invalid, = saved(api, fmt="png")
    for record in (raw, invalid):
        assert api.client.get(f"/captures/{record['id']}/thumbnail").status_code == 404
    (api.root / invalid["id"] / "image.png").unlink()
    outside = api.tmp / "image.png"
    outside.write_bytes(b"private")
    (api.root / invalid["id"] / "image.png").symlink_to(outside)
    assert api.client.get(f"/captures/{invalid['id']}/thumbnail").status_code == 400
    assert api.client.get("/captures/.pending-test/thumbnail").status_code == 400
    assert api.client.get("/captures/missing/thumbnail").status_code == 404


@pytest.mark.parametrize("target", ["directory", "record"])
def test_thumbnail_rejects_symlinked_directories_and_private_records(api, target):
    record, = saved(api, fmt="png")
    directory = api.root / record["id"]
    if target == "directory":
        destination = api.tmp / "outside-capture"
        directory.rename(destination)
        directory.symlink_to(destination, target_is_directory=True)
    else:
        destination = api.tmp / "outside-record.json"
        (directory / "record.json").rename(destination)
        (directory / "record.json").symlink_to(destination)
    response = api.client.get(f"/captures/{record['id']}/thumbnail")
    assert response.status_code in {400, 404}


@pytest.mark.parametrize("name", ["metadata.json", "image.raw"])
def test_bulk_download_validates_missing_files_before_creating_archive(api, name):
    records = saved(api, count=2)
    (api.root / records[1]["id"] / name).unlink()
    response = api.client.post("/api/captures/download?prepare=1", json={"all": True})
    assert response.status_code == 404
    assert api.app.extensions["capture_archives"] == {}
    assert api.client.get("/api/status").json["capture_count"] == 2
    assert api.client.post("/api/captures/delete", json={"all": True}).json["capture_count"] == 0


def test_empty_bulk_library_deletes_safely_and_download_is_not_created(api):
    assert api.client.post("/api/captures/delete", json={"all": True}).json == {
        "deleted": [], "captures": [], "capture_count": 0,
    }
    assert api.client.post("/api/captures/download", json={"all": True}).status_code == 400
    assert api.app.extensions["capture_archives"] == {}


def test_large_capture_id_selection_keeps_camera_request_limit_small(api):
    records = saved(api, count=400)
    ids = [record["id"] for record in records]
    assert len(json.dumps({"ids": ids})) > 16 * 1024
    response = api.client.post("/api/captures/delete", json={"ids": ids})
    assert response.status_code == 200
    assert len(response.json["deleted"]) == 400
    assert api.client.post("/api/stop", json={"padding": "x" * (17 * 1024)}).status_code == 413
