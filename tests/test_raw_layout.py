"""Real DepthAI message enums must describe the bytes saved from sensor RAW.

Messages are constructed on the host. These tests never instantiate dai.Device
or reserve a connected camera.
"""

import pytest

from oak_camera.backend import CameraBackend
from oak_camera.raw import unpack_raw10


dai = pytest.importorskip("depthai")


@pytest.fixture
def backend():
    instance = CameraBackend(demo=True)
    try:
        yield instance
    finally:
        instance.close()


def image_message(kind, width, height, payload, row_stride=None):
    frame = dai.ImgFrame()
    frame.setType(getattr(dai.ImgFrame.Type, kind))
    frame.setWidth(width)
    frame.setHeight(height)
    frame.setSequenceNum(17)
    frame.setData(list(payload))
    if row_stride is not None:
        # v2 exposes the field even though ImgFrame.getStride is unavailable.
        frame.getRaw().fb.stride = row_stride
    return frame


@pytest.mark.parametrize("kind,bits,row,samples", [
    ("PACK10", 10, bytes([0, 0, 255, 128, 0x34]), [0, 1, 1023, 512]),
    ("PACK12", 12, bytes([0, 0, 0x10, 255, 128, 0x0F]), [0, 1, 4095, 2048]),
])
@pytest.mark.parametrize("padding", [b"", b"\xab\xcd"])
def test_packed_depthai_enums_keep_sensor_depth_and_row_padding(backend, kind, bits, row, samples, padding):
    stride = len(row) + len(padding)
    payload = (row + padding) * 2
    frame = image_message(kind, 4, 2, payload, row_stride=stride)
    extracted = backend._frame_metadata(frame)
    assert extracted["frame_type"] == kind
    assert extracted["sequence"] == 17
    layout = backend._raw_metadata(extracted, frame.getData().tobytes())
    assert layout["width"] == 4
    assert layout["height"] == 2
    assert layout["frame_type"] == kind
    assert layout["bit_depth"] == bits
    assert layout["packing"] == f"MIPI_RAW{bits}"
    assert layout["packed"] is True
    assert layout["row_stride_bytes"] == stride
    assert layout["active_row_bytes"] == len(row)
    assert layout["row_padding_bytes"] == len(padding)
    assert layout["byte_count"] == len(payload)
    assert layout["cfa_pattern"] is None
    if bits == 10:
        # This separately tests the saved layout against known sensor values,
        # including padding bytes that must not be interpreted as pixels.
        decoded = unpack_raw10(payload, 4, 2, layout["row_stride_bytes"])
        assert decoded.tolist() == [samples, samples]


@pytest.mark.parametrize("kind,bits", [
    ("RAW10", 10), ("RAW12", 12), ("RAW14", 14), ("RAW16", 16),
])
def test_raw_depthai_enums_use_sixteen_bit_storage_not_mipi_packing(backend, kind, bits):
    samples = [0, 1, 1 << (bits - 1), (1 << bits) - 1]
    row = b"".join(sample.to_bytes(2, "little") for sample in samples)
    payload = row * 2
    frame = image_message(kind, 4, 2, payload, row_stride=8)
    layout = backend._raw_metadata(backend._frame_metadata(frame), frame.getData().tobytes())
    assert layout["frame_type"] == kind
    assert layout["bit_depth"] == bits
    assert layout["packing"] == "UINT16_LE"
    assert layout["packed"] is False
    assert layout["active_row_bytes"] == 8
    assert layout["row_stride_bytes"] == 8
    assert layout["row_padding_bytes"] == 0
    assert layout["byte_count"] == 16


def test_raw8_is_single_byte_storage(backend):
    payload = bytes([0, 1, 128, 255]) * 2
    frame = image_message("RAW8", 4, 2, payload, row_stride=4)
    layout = backend._raw_metadata(backend._frame_metadata(frame), frame.getData().tobytes())
    assert layout["packing"] == "UINT8"
    assert layout["packed"] is False
    assert layout["bit_depth"] == 8
    assert layout["active_row_bytes"] == 4
    assert layout["byte_count"] == 8


def test_packed_bytes_cannot_be_mislabeled_as_unpacked_raw10(backend):
    payload = bytes([0, 0, 255, 128, 0x34]) * 2
    frame = image_message("RAW10", 4, 2, payload, row_stride=5)
    with pytest.raises(RuntimeError):
        backend._raw_metadata(backend._frame_metadata(frame), frame.getData().tobytes())


def test_unsupported_type_error_includes_frame_layout_for_diagnosis(backend):
    payload = bytes(23)
    frame = image_message("NV12", 37, 11, payload)
    with pytest.raises(RuntimeError) as failure:
        backend._raw_metadata(backend._frame_metadata(frame), frame.getData().tobytes())
    message = str(failure.value)
    for detail in ("NV12", "37", "11", "23"):
        assert detail in message


@pytest.mark.parametrize("width,height,length", [
    (8, 3, 29),  # Truncated final row.
    (8, 2, 18),  # Whole rows, but each row is too short for eight RAW10 pixels.
    (8, 3, 31),  # Extra bytes that cannot represent uniform row padding.
    (4, 0, 5),
    (0, 2, 10),
    (-4, 2, 10),
    (4, -2, 10),
])
def test_malformed_raw_layout_is_rejected(backend, width, height, length):
    metadata = {"frame_type": "PACK10", "width": width, "height": height,
                "row_stride_bytes": None}
    with pytest.raises(RuntimeError):
        backend._raw_metadata(metadata, bytes(length))
