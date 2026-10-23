"""Queue generation must use the migrated catalog and work on a fresh checkout."""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from building_kb.commands import queue


class QueuePathTests(unittest.TestCase):
    def test_fresh_output_directory_and_migrated_source_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            with catalog.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(
                    stream, fieldnames=["std_code", "file_path", "page_count", "is_scanned"]
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "std_code": "GB 12345-2099",
                        "file_path": "data/sources/标准库/a.pdf",
                        "page_count": "10",
                        "is_scanned": "是",
                    }
                )
            tiers = root / "tiers.tsv"
            tiers.write_text("1\tGB 12345-2099\ttest\n")
            output = root / "var/queues"
            with patch.object(queue.paths, "ROOT", root):
                self.assertEqual(
                    queue.main(
                        [
                            "--catalog",
                            str(catalog),
                            "--tiers",
                            str(tiers),
                            "--chunks",
                            str(root / "chunks"),
                            "--out",
                            str(output),
                        ]
                    ),
                    0,
                )
            self.assertEqual(
                (output / "ocr_t1.txt").read_text().strip(), str(root / "data/sources/标准库/a.pdf")
            )
