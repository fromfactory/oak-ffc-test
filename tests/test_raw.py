import numpy as np
import pytest

from oak_camera.raw import unpack_raw10


def test_mipi_raw10_known_values_and_low_bit_order():
    # Samples 0, 1, 1022, 1023: MSBs 0,0,255,255; low bits 00,01,10,11.
    decoded = unpack_raw10(bytes([0, 0, 255, 255, 0b11100100]), 4, 1)
    np.testing.assert_array_equal(decoded, [[0, 1, 1022, 1023]])
    assert decoded.dtype == np.uint16


def test_raw10_row_padding_is_removed_without_reordering():
    row1 = bytes([0, 0, 255, 255, 0b11100100, 99, 100, 101])
    row2 = bytes([25, 50, 75, 100, 0, 9, 10, 11])
    np.testing.assert_array_equal(unpack_raw10(row1 + row2, 4, 2, 8),
                                  [[0, 1, 1022, 1023], [100, 200, 300, 400]])


@pytest.mark.parametrize("data,width,height,stride", [
    (b"", 4, 1, None), (bytes(6), 4, 1, None), (bytes(5), 3, 1, None),
    (bytes(5), 4, 1, 4), (bytes(5), 0, 1, None), (bytes(5), 4, -1, None),
])
def test_raw10_rejects_inconsistent_layout(data, width, height, stride):
    with pytest.raises(ValueError):
        unpack_raw10(data, width, height, stride)
