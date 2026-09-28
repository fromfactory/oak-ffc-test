"""Capture persistence must not publish incomplete image/metadata pairs."""

import json
from pathlib import Path

import pytest

from oak_camera.storage import CaptureStore


def payload(extension="raw"):
    return {
        "data": b"\x00\x01\x02\x03\xe4",
        "extension": extension,
        "metadata": {"width": 4, "height": 1, "row_stride": 5,
                     "bit_depth": 10, "packing": "MIPI_RAW10", "bayer_order": None,
                     "actual": {"exposure_us": 1000, "sequence": 7}},
    }


def test_capture_round_trip_preserves_bytes_metadata_and_reloads(tmp_path):
    store = CaptureStore(tmp_path / "captures")
    result = payload()
    record = store.save("CAM_D", "raw", result)
    directory = store.root / record["id"]
    assert {path.name for path in directory.iterdir()} == {"image.raw", "metadata.json", "record.json"}
    assert (directory / "image.raw").read_bytes() == result["data"]
    metadata = json.loads((directory / "metadata.json").read_text())
    assert all(metadata[key] == value for key, value in result["metadata"].items())
    assert metadata["socket"] == "CAM_D"
    assert metadata["format"] == "raw"
    assert metadata["image_file"] == "image.raw"
    assert metadata["demo"] is False
    assert metadata["capture_id"] == record["id"]
    assert record["metadata"] == metadata
    assert CaptureStore(store.root).list() == [record]
    for file in record["files"]:
        assert file["size"] == (directory / file["name"]).stat().st_size
        assert file["url"] == f"/captures/{record['id']}/{file['name']}"


def test_repeated_capture_does_not_overwrite_and_demo_provenance_cannot_be_spoofed(tmp_path):
    store = CaptureStore(tmp_path)
    result = payload("PNG")
    result["metadata"].update(demo=False, socket="CAM_B", format="jpeg", capture_id="fake")
    first = store.save("CAM_A", "png", result, demo=True)
    second = store.save("CAM_A", "png", result, demo=True)
    assert first["id"] != second["id"]
    assert first["metadata"]["demo"] is True
    assert first["metadata"]["socket"] == "CAM_A"
    assert first["metadata"]["format"] == "png"
    assert first["metadata"]["capture_id"] == first["id"]
    assert (tmp_path / first["id"] / "image.png").exists()
    assert store.list(limit=1) == [second]
    assert len(store.list()) == 2


@pytest.mark.parametrize("failure", ["image.raw", "metadata.json", "record.json", "rename"])
def test_disk_failure_leaves_previous_captures_and_no_partial_directory(tmp_path, monkeypatch, failure):
    store = CaptureStore(tmp_path)
    good = store.save("CAM_A", "raw", payload())
    original_write_bytes = Path.write_bytes
    original_write_text = Path.write_text
    original_rename = Path.rename

    def write_bytes(path, data):
        if path.name == failure:
            original_write_bytes(path, data[:1])
            raise OSError("simulated full disk")
        return original_write_bytes(path, data)

    def write_text(path, data, *args, **kwargs):
        if path.name == failure:
            raise OSError("simulated full disk")
        return original_write_text(path, data, *args, **kwargs)

    def rename(path, destination):
        if failure == "rename":
            raise OSError("simulated rename failure")
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError, match="simulated"):
        store.save("CAM_D", "raw", payload())
    assert store.list() == [good]
    assert [path.name for path in tmp_path.iterdir()] == [good["id"]]
    assert (tmp_path / good["id"] / "image.raw").read_bytes() == payload()["data"]


def test_incomplete_invalid_and_mismatched_records_are_not_listed(tmp_path):
    store = CaptureStore(tmp_path)
    good = store.save("CAM_A", "raw", payload())
    records = {
        ".pending-incomplete": {"id": ".pending-incomplete"},
        "wrong-id": {"id": "different-id"},
        "wrong-type": [],
        "bad-json": None,
    }
    for name, record in records.items():
        folder = tmp_path / name
        folder.mkdir()
        (folder / "record.json").write_text("{" if record is None else json.dumps(record))
    empty = tmp_path / "image-only"
    empty.mkdir()
    (empty / "image.raw").write_bytes(b"partial")
    assert CaptureStore(tmp_path).list() == [good]


@pytest.mark.parametrize("extension", ["../outside", "png/../../escape", "exe", ""])
def test_backend_filename_extension_cannot_escape_capture_directory(tmp_path, extension):
    store = CaptureStore(tmp_path)
    with pytest.raises(ValueError, match="extension"):
        store.save("CAM_A", "raw", payload(extension))
    assert list(tmp_path.iterdir()) == []


def test_nonfinite_metadata_is_rejected_without_creating_a_capture(tmp_path):
    store = CaptureStore(tmp_path)
    result = payload()
    result["metadata"]["exposure_us"] = float("nan")
    with pytest.raises(ValueError):
        store.save("CAM_A", "raw", result)
    assert store.list() == []
    assert list(tmp_path.iterdir()) == []
