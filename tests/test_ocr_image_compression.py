from __future__ import annotations

import io
import unittest

from PIL import Image

from src.ocr_schedule import compress_image_for_ocr


def _jpeg_bytes(width: int, height: int, *, orientation: int | None = None) -> bytes:
    image = Image.new("RGB", (width, height), color=(120, 60, 30))
    exif = image.getexif()
    if orientation is not None:
        exif[0x0112] = orientation
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


class TestCompressImageForOcr(unittest.TestCase):
    def test_small_image_is_returned_unchanged(self) -> None:
        data = _jpeg_bytes(300, 200)

        result = compress_image_for_ocr(data, max_dimension=2000)

        self.assertEqual(result, data)

    def test_large_image_is_downscaled_to_max_dimension(self) -> None:
        data = _jpeg_bytes(3000, 1500)

        result = compress_image_for_ocr(data, max_dimension=2000)

        with Image.open(io.BytesIO(result)) as resized:
            self.assertEqual(resized.format, "JPEG")
            self.assertEqual(max(resized.size), 2000)
            self.assertAlmostEqual(resized.size[0] / resized.size[1], 2.0, places=1)
        self.assertLess(len(result), len(data))

    def test_exif_orientation_is_applied_before_resizing(self) -> None:
        # Хранится как портрет 1000x2000, но EXIF-тег 6 говорит повернуть на 90° —
        # реальная (отображаемая) ориентация должна быть альбомная 2000x1000.
        data = _jpeg_bytes(1000, 2000, orientation=6)

        result = compress_image_for_ocr(data, max_dimension=800)

        with Image.open(io.BytesIO(result)) as resized:
            self.assertEqual(resized.size, (800, 400))

    def test_broken_image_falls_back_to_original_bytes(self) -> None:
        garbage = b"not an image at all"

        result = compress_image_for_ocr(garbage, max_dimension=2000)

        self.assertEqual(result, garbage)


if __name__ == "__main__":
    unittest.main()
