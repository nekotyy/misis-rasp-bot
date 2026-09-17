from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.import_schedule_photos import read_images


class ReadPhotoImagesTests(unittest.TestCase):
    def test_reads_images_in_argument_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.jpg"
            second = Path(directory) / "second.png"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            self.assertEqual(read_images([first, second]), [b"first", b"second"])

    def test_rejects_missing_file(self) -> None:
        with self.assertRaisesRegex(ValueError, "Файл не найден"):
            read_images([Path("missing-photo.jpg")])


if __name__ == "__main__":
    unittest.main()
