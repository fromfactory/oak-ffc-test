#!/usr/bin/env python3
"""Repeatable real-device smoke test; close the browser application first."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oak_camera.backend import CameraBackend
from oak_camera.storage import CaptureStore
from oak_camera.validation import validate_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sockets", nargs="+", default=["CAM_A", "CAM_D"])
    parser.add_argument("--resolution", choices=["1080p", "4k", "12mp"], default="1080p")
    parser.add_argument("--fps", type=float, default=10)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--device-id")
    parser.add_argument("--output", type=Path, default=Path("captures/hardware-checks"))
    args = parser.parse_args()
    if not 3 <= args.seconds <= 300:
        parser.error("--seconds must be between 3 and 300")
    config = validate_config({"cameras": [{"socket": s, "resolution": args.resolution, "fps": args.fps}
                                           for s in args.sockets], "raw_enabled": args.raw})
    folder = args.output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    store = CaptureStore(folder)
    backend = CameraBackend(device_id=args.device_id)
    report = {"demo": False, "config": config, "captures": [], "passed": False}
    try:
        report["discovery"] = backend.scan()
        backend.start(config)
        time.sleep(args.seconds)
        report["status"] = backend.status()
        state = report["status"]
        if not state["running"] or state["error"]:
            raise RuntimeError(state["error"] or "The camera pipeline stopped.")
        for cam in state["cameras"]:
            if not cam["active"]:
                continue
            if cam["frames"] < 2 or cam["last_frame_age"] is None or cam["last_frame_age"] > 2:
                raise RuntimeError(f"{cam['socket']} did not deliver fresh frames.")
            print(f"{cam['socket']}: {cam['fps']} measured FPS, {cam['frames']} frames", flush=True)
        for socket in args.sockets:
            for fmt in (["png", "raw"] if args.raw else ["png"]):
                report["captures"].append(store.save(socket, fmt, backend.capture(socket, fmt)))
        report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
        print(f"Hardware check failed: {exc}", file=sys.stderr)
    finally:
        report["final_status"] = backend.status()
        backend.close()
        (folder / "report.json").write_text(json.dumps(report, indent=2))
        print(f"Report and captures: {folder}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
