"""Lossless MIPI RAW10 unpacking; no demosaicing or guessed Bayer order."""

import argparse
import json
from pathlib import Path

import numpy as np


def unpack_raw10(data, width, height, row_stride_bytes=None):
    """Return H×W uint16 sensor samples, preserving the 10-bit value range."""
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise ValueError("RAW width and height must be positive integers.")
    if width % 4:
        raise ValueError("This RAW10 decoder requires a width divisible by four.")
    payload_stride = width // 4 * 5
    stride = payload_stride if row_stride_bytes is None else row_stride_bytes
    if not isinstance(stride, int) or stride < payload_stride:
        raise ValueError("The RAW row stride is smaller than the pixel payload.")
    if len(data) != stride * height:
        raise ValueError(f"Expected {stride * height} RAW bytes; received {len(data)}.")
    groups = np.frombuffer(data, dtype=np.uint8).reshape(height, stride)[:, :payload_stride]
    groups = groups.reshape(height, width // 4, 5).astype(np.uint16)
    pixels = np.empty((height, width // 4, 4), dtype=np.uint16)
    for pixel in range(4):
        pixels[:, :, pixel] = (groups[:, :, pixel] << 2) | ((groups[:, :, 4] >> (pixel * 2)) & 3)
    return pixels.reshape(height, width)


def main():
    parser = argparse.ArgumentParser(description="Unpack saved MIPI RAW10 into a NumPy Bayer sample array")
    parser.add_argument("raw", type=Path, help="Path to image.raw")
    parser.add_argument("--metadata", type=Path, help="Defaults to metadata.json beside the image")
    parser.add_argument("--output", type=Path, help="Defaults to image.npy; existing files are preserved")
    args = parser.parse_args()
    metadata = json.loads((args.metadata or args.raw.with_name("metadata.json")).read_text())
    packing = str(metadata.get("packing", "")).lower().replace("-", "_").replace(" ", "_")
    if "raw10" not in packing:
        parser.error("The metadata must identify MIPI RAW10 packing.")
    pixels = unpack_raw10(args.raw.read_bytes(), metadata["width"], metadata["height"],
                          metadata.get("row_stride_bytes"))
    output = args.output or args.raw.with_suffix(".npy")
    with output.open("xb") as stream:
        np.save(stream, pixels, allow_pickle=False)
    print(f"Saved {pixels.shape[1]} × {pixels.shape[0]} uint16 Bayer samples to {output}")
    print("Values remain 0–1023. Bayer order is not inferred; no demosaicing was applied.")


if __name__ == "__main__":
    main()
