"""OCR argument parsing must work without model weights or cloud credentials."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from building_kb.commands import ocr_cloud
from building_kb.ingest import ocr


class OcrCommandTests(unittest.TestCase):
    def test_local_help_does_not_load_models(self):
        with patch.object(ocr, "_allow_globals", side_effect=AssertionError("model loaded")):
            with redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as result:
                    ocr.main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("--work", output.getvalue())

    def test_cloud_help_does_not_read_token_or_submit_documents(self):
        with patch.object(ocr_cloud, "_token", side_effect=AssertionError("token read")):
            with redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as result:
                    ocr_cloud.main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("--submit-only", output.getvalue())
