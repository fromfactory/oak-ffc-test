"""Atomic capture folders with native image bytes and JSON sidecars."""

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import threading
from uuid import uuid4


class CaptureStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

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

    def list(self, limit=50):
        result = []
        with self._lock:
            for path in sorted(self.root.glob("*/record.json"), reverse=True):
                if path.parent.name.startswith("."):
                    continue
                try:
                    record = json.loads(path.read_text())
                    if record.get("id") == path.parent.name:
                        result.append(record)
                except (ValueError, OSError, AttributeError):
                    continue
                if len(result) >= limit:
                    break
        return result
