"""Atomic capture folders with native image bytes and JSON sidecars."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
from uuid import uuid4
import zipfile

from .validation import capture_id


IMAGE_FILES = {f"image.{extension}" for extension in ("jpg", "jpeg", "png", "tiff", "tif", "bmp", "raw")}
PUBLIC_FILES = IMAGE_FILES | {"metadata.json"}


class CaptureNotFound(FileNotFoundError):
    """The requested capture or one of its files is unavailable."""


class CaptureDeletionError(OSError):
    """Report completed deletions when a later directory cannot be removed."""

    def __init__(self, capture_id, deleted, error):
        super().__init__(f"Could not completely delete {capture_id}: {error}. "
                         f"{len(deleted)} capture(s) permanently deleted.")
        self.capture_id = capture_id
        self.deleted = deleted


class CaptureStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._record_cache = {}

    def save(self, socket, fmt, result, demo=False):
        now = datetime.now(timezone.utc)
        capture_id = now.strftime("%Y%m%dT%H%M%S.%fZ") + "_" + socket + "_" + uuid4().hex[:8]
        extension = str(result["extension"]).lstrip(".").lower()
        if extension not in {"jpg", "jpeg", "png", "tiff", "tif", "bmp", "raw"}:
            raise ValueError("Unsupported backend capture extension.")
        metadata = {**result.get("metadata", {}), "demo": bool(demo),
                    "socket": socket, "format": fmt, "created_at": now.isoformat(),
                    "capture_id": capture_id, "image_file": f"image.{extension}"}
        record = {"id": capture_id, "created_at": now.isoformat(), "socket": socket,
                  "format": fmt, "metadata": metadata, "files": []}
        payloads = {f"image.{extension}": result["data"],
                    "metadata.json": json.dumps(metadata, indent=2, allow_nan=False).encode()}
        temp = self.root / (".pending-" + capture_id)
        final = self.root / capture_id
        with self._lock:
            temp.mkdir()
            try:
                for name, content in payloads.items():
                    (temp / name).write_bytes(content)
                    record["files"].append({"name": name, "size": len(content),
                                            "url": f"/captures/{capture_id}/{name}"})
                (temp / "record.json").write_text(json.dumps(record, indent=2, allow_nan=False))
                temp.rename(final)
            except BaseException:
                shutil.rmtree(temp, ignore_errors=True)
                raise
        return record

    def _directory(self, value):
        """Never accept a hidden folder, another directory, or any symlink."""
        capture_id(value)
        directory = self.root / value
        if directory.is_symlink() or not directory.is_dir():
            raise CaptureNotFound(f"Saved capture {value} is unavailable.")
        if directory.resolve().parent != self.root:
            raise ValueError("Unsafe saved capture directory.")
        return directory

    @staticmethod
    def _regular_file(directory, name):
        path = directory / name
        if path.is_symlink():
            raise ValueError("Saved capture files must not be symbolic links.")
        if not path.is_file():
            raise CaptureNotFound(f"Saved capture file {name} is unavailable.")
        if path.resolve().parent != directory:
            raise ValueError("Unsafe saved capture file.")
        return path

    def _record(self, value):
        directory = self._directory(value)
        path = self._regular_file(directory, "record.json")
        info = path.stat()
        key = (info.st_mtime_ns, info.st_size, info.st_ino)
        cached = self._record_cache.get(value)
        if cached and cached[0] == key:
            record = cached[1]
        else:
            try:
                record = json.loads(path.read_text())
            except (ValueError, OSError) as exc:
                raise CaptureNotFound(f"Saved capture {value} has an unreadable record.") from exc
            if not isinstance(record, dict) or record.get("id") != value:
                raise CaptureNotFound(f"Saved capture {value} has an invalid record.")
            self._record_cache[value] = (key, record)
        return directory, record

    def _records(self):
        result = []
        for directory in sorted(self.root.iterdir(), reverse=True):
            try:
                _, record = self._record(directory.name)
            except (ValueError, OSError):
                continue
            result.append(record)
        visible = {record["id"] for record in result}
        self._record_cache = {key: value for key, value in self._record_cache.items() if key in visible}
        return result

    def snapshot(self, limit=50):
        """Return recent records and the total without parsing unchanged JSON again."""
        with self._lock:
            records = self._records()
            return (records if limit is None else records[:limit]), len(records)

    def list(self, limit=50):
        return self.snapshot(limit)[0]

    def _selection(self, ids):
        records = self._records() if ids is None else [self._record(value)[1] for value in ids]
        # Validate the complete selection before creating archives or deleting anything.
        result = []
        for record in records:
            directory = self._directory(record["id"])
            for path in directory.rglob("*"):
                if path.is_symlink():
                    raise ValueError("Saved capture folders must not contain symbolic links.")
            result.append((directory, record))
        return result

    def delete(self, ids=None):
        with self._lock:
            selected = self._selection(ids)
            deleted = []
            for directory, record in selected:
                try:
                    # Remove the record last so an interrupted deletion stays visible
                    # and can be retried, even if an image or sidecar was removed.
                    children = sorted(directory.iterdir(), key=lambda path: path.name == "record.json")
                    for child in children:
                        if child.is_dir() and not child.is_symlink():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                    directory.rmdir()
                except OSError as exc:
                    if directory.exists() and not (directory / "record.json").exists():
                        try:
                            (directory / "record.json").write_text(json.dumps(record, indent=2, allow_nan=False))
                        except OSError:
                            pass
                    raise CaptureDeletionError(record["id"], deleted, exc) from exc
                self._record_cache.pop(record["id"], None)
                deleted.append(record["id"])
            return deleted

    def _image_file(self, directory, record):
        metadata = record.get("metadata")
        name = metadata.get("image_file") if isinstance(metadata, dict) else None
        if not isinstance(name, str) or name not in IMAGE_FILES:
            raise ValueError("Saved capture has an invalid image filename.")
        return self._regular_file(directory, name)

    def archive(self, ids=None):
        """Build a disk-backed archive; the response owns and closes this file."""
        with self._lock:
            selected = self._selection(ids)
            if not selected:
                raise ValueError("There are no saved captures to download.")
            files = []
            for directory, record in selected:
                for path in (self._image_file(directory, record),
                             self._regular_file(directory, "metadata.json")):
                    files.append((path, f"{record['id']}/{path.name}"))
            output = tempfile.TemporaryFile(mode="w+b", dir=self.root, prefix=".download-")
            try:
                # Images are already compressed or high-volume RAW. Storing them
                # avoids a CPU-heavy recompression step on Raspberry Pi.
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                    for path, name in files:
                        # O_NOFOLLOW also rejects a replaced file after prevalidation.
                        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                        with os.fdopen(descriptor, "rb") as source:
                            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                                raise ValueError("Saved capture files must be regular files.")
                            with archive.open(name, "w", force_zip64=True) as destination:
                                shutil.copyfileobj(source, destination, length=1024 * 1024)
                output.seek(0)
                return output
            except BaseException:
                output.close()
                raise

    def file(self, value, name):
        if name not in PUBLIC_FILES:
            raise CaptureNotFound("Unknown capture file.")
        with self._lock:
            # Individual downloads remain available for captures whose record is
            # damaged, while using the same directory and symlink restrictions.
            return self._regular_file(self._directory(value), name)

    def thumbnail(self, value):
        with self._lock:
            directory, record = self._record(value)
            image = self._image_file(directory, record)
            if image.suffix == ".raw":
                raise CaptureNotFound("RAW captures do not have a thumbnail.")
            import cv2

            frame = cv2.imread(str(image), cv2.IMREAD_COLOR)
            if frame is None:
                raise CaptureNotFound("This saved image cannot be previewed.")
            height, width = frame.shape[:2]
            scale = min(320 / width, 240 / height, 1)
            if scale < 1:
                frame = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))),
                                   interpolation=cv2.INTER_AREA)
            ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if not ok:
                raise CaptureNotFound("This saved image cannot be previewed.")
            return jpeg.tobytes()
