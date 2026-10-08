"""Capture persistence must not publish incomplete image/metadata pairs."""

import json
import io
from pathlib import Path
import zipfile

import pytest

from oak_camera.storage import CaptureDeletionError, CaptureNotFound, CaptureStore


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


def test_full_library_and_total_include_captures_older_than_recent_limit(tmp_path):
    store = CaptureStore(tmp_path)
    records = [store.save("CAM_A", "raw", payload()) for _ in range(55)]
    recent, total = store.snapshot()
    assert recent == list(reversed(records))[:50]
    assert total == 55
    assert store.list(limit=None) == list(reversed(records))
    assert store.list(limit=0) == []


def test_record_cache_reloads_changes_and_ignores_symlinked_records(tmp_path):
    store = CaptureStore(tmp_path / "captures")
    record = store.save("CAM_A", "raw", payload())
    assert store.list() == [record]
    updated = {**record, "created_at": "changed"}
    record_path = store.root / record["id"] / "record.json"
    record_path.write_text(json.dumps(updated))
    assert store.list() == [updated]
    outside = tmp_path / "record.json"
    record_path.rename(outside)
    record_path.symlink_to(outside)
    assert store.list() == []


def test_delete_selection_removes_entire_folders_and_all_leaves_unmanaged_files(tmp_path):
    store = CaptureStore(tmp_path)
    records = [store.save("CAM_A", "raw", payload()) for _ in range(3)]
    pending = tmp_path / ".pending-not-a-capture"
    pending.mkdir()
    (pending / "image.raw").write_bytes(b"pending")
    unrelated = tmp_path / "other-data"
    unrelated.mkdir()
    (unrelated / "image.raw").write_bytes(b"unmanaged")
    assert store.delete([records[0]["id"], records[2]["id"]]) == [records[0]["id"], records[2]["id"]]
    assert store.list() == [records[1]]
    assert not (tmp_path / records[0]["id"]).exists()
    assert not (tmp_path / records[2]["id"]).exists()
    assert store.delete() == [records[1]["id"]]
    assert store.list() == []
    assert (pending / "image.raw").read_bytes() == b"pending"
    assert (unrelated / "image.raw").read_bytes() == b"unmanaged"
    assert store.delete() == []


@pytest.mark.parametrize("unsafe", ["../outside", ".pending-test", "/absolute", "missing"])
def test_delete_validates_every_id_before_removing_anything(tmp_path, unsafe):
    store = CaptureStore(tmp_path)
    record = store.save("CAM_A", "raw", payload())
    with pytest.raises((ValueError, CaptureNotFound)):
        store.delete([record["id"], unsafe])
    assert store.list() == [record]
    assert (tmp_path / record["id"] / "image.raw").read_bytes() == payload()["data"]


@pytest.mark.parametrize("target", ["directory", "image", "record", "nested"])
def test_delete_and_archive_reject_symlink_targets_without_touching_originals(tmp_path, target):
    store = CaptureStore(tmp_path / "captures")
    good = store.save("CAM_A", "raw", payload())
    unsafe = store.save("CAM_D", "raw", payload())
    directory = store.root / unsafe["id"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "image.raw").write_bytes(b"private")
    if target == "directory":
        directory.rename(outside / "capture")
        directory.symlink_to(outside / "capture", target_is_directory=True)
    elif target == "nested":
        (directory / "link").symlink_to(outside, target_is_directory=True)
    else:
        filename = "image.raw" if target == "image" else "record.json"
        (directory / filename).rename(outside / filename)
        (directory / filename).symlink_to(outside / filename)
    for operation in (store.delete, store.archive):
        with pytest.raises((ValueError, CaptureNotFound)):
            operation([good["id"], unsafe["id"]])
        assert (store.root / good["id"] / "image.raw").exists()
    assert (outside / "image.raw").exists()


def test_missing_image_can_still_be_deleted_but_archive_rejects_whole_selection(tmp_path):
    store = CaptureStore(tmp_path)
    good = store.save("CAM_A", "raw", payload())
    incomplete = store.save("CAM_D", "raw", payload())
    (tmp_path / incomplete["id"] / "image.raw").unlink()
    with pytest.raises(CaptureNotFound):
        store.archive([good["id"], incomplete["id"]])
    assert len(store.list()) == 2
    assert store.delete([incomplete["id"]]) == [incomplete["id"]]
    assert store.list() == [good]


def test_deletion_error_reports_completed_captures_and_keeps_failed_record_for_retry(tmp_path, monkeypatch):
    store = CaptureStore(tmp_path)
    records = [store.save("CAM_A", "raw", payload()) for _ in range(3)]
    original = Path.unlink

    def fail_unlink(path, *args, **kwargs):
        if path.parent.name == records[1]["id"] and path.name == "metadata.json":
            raise OSError("read-only filesystem")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(CaptureDeletionError) as error:
        store.delete([record["id"] for record in records])
    assert error.value.deleted == [records[0]["id"]]
    assert error.value.capture_id == records[1]["id"]
    assert store.list(limit=None) == list(reversed(records[1:]))
    assert (tmp_path / records[1]["id"] / "record.json").exists()
    assert (tmp_path / records[2]["id"] / "image.raw").exists()
    monkeypatch.undo()
    assert store.delete([record["id"] for record in records[1:]]) == [record["id"] for record in records[1:]]
    assert store.list() == []


def test_all_selection_prevalidates_every_recorded_folder_before_any_deletion(tmp_path):
    store = CaptureStore(tmp_path / "captures")
    records = [store.save("CAM_A", "raw", payload()) for _ in range(3)]
    outside = tmp_path / "private.raw"
    outside.write_bytes(b"private")
    image = store.root / records[0]["id"] / "image.raw"
    image.unlink()
    image.symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic links"):
        store.delete()
    assert len(store.list()) == 3
    assert all((store.root / record["id"]).exists() for record in records)
    assert outside.read_bytes() == b"private"


def test_archive_is_disk_backed_contains_originals_and_excludes_private_records(tmp_path):
    store = CaptureStore(tmp_path)
    records = [store.save("CAM_A", "raw", payload()), store.save("CAM_D", "png", payload("png"))]
    with store.archive() as output:
        assert not isinstance(output, io.BytesIO)
        assert output.fileno() >= 0
        with zipfile.ZipFile(output) as archive:
            names = {name for name in archive.namelist()}
            assert names == {f"{record['id']}/{file['name']}" for record in records for file in record["files"]}
            assert not any("record.json" in name for name in names)
            for record in records:
                assert archive.read(f"{record['id']}/{record['metadata']['image_file']}") == payload()["data"]
                assert json.loads(archive.read(f"{record['id']}/metadata.json")) == record["metadata"]


def test_failed_archive_creation_closes_temporary_file(tmp_path, monkeypatch):
    import oak_camera.storage as storage_module

    store = CaptureStore(tmp_path)
    store.save("CAM_A", "raw", payload())
    original = storage_module.tempfile.TemporaryFile
    opened = []

    def track_file(*args, **kwargs):
        output = original(*args, **kwargs)
        opened.append(output)
        return output

    def fail_copy(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(storage_module.tempfile, "TemporaryFile", track_file)
    monkeypatch.setattr(storage_module.shutil, "copyfileobj", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        store.archive()
    assert len(opened) == 1 and opened[0].closed
