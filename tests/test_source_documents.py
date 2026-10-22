"""完整阅读本的页码映射、指纹和访问鉴权。"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from building_kb.web import main
from building_kb import source_pdf


class SourceDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data/catalog").mkdir(parents=True)
        self.source = self.root / "source.pdf"
        self.source.write_bytes(b"source")
        self.full = self.root / "full.pdf"
        self.full.write_bytes(b"full")
        self.record = {"GB 55037-2022": {
            "full_pdf_path": "full.pdf", "page_offset": 8,
            "source_pdf_sha256": hashlib.sha256(b"source").hexdigest(),
            "sha256": hashlib.sha256(b"full").hexdigest(),
        }}
        (self.root / "data/catalog/source_documents.json").write_text(json.dumps(self.record))
        self.patches = [patch.object(source_pdf, "ROOT", self.root),
                        patch.object(source_pdf, "DOCUMENTS", self.root / "data/catalog/source_documents.json"),
                        patch.object(source_pdf, "resolve", return_value=self.source)]
        for p in self.patches:
            p.start()
        source_pdf.pdf_document.cache_clear()

    def tearDown(self):
        source_pdf.pdf_document.cache_clear()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_mapped_pdf_opens_full_document_at_correct_page(self):
        with patch.object(main.auth, "valid", return_value=True), patch.object(main, "_source", return_value=(self.source, 66)):
            response = main.pdf("GB 55037-2022", source_page=43, kb_session="valid")
            self.assertTrue(response.headers["location"].endswith("#page=51"))
            full = main.pdf("GB 55037-2022", kb_session="valid")
            self.assertEqual(full.path, self.full)

    def test_changed_fingerprint_refuses_mapping(self):
        self.full.write_bytes(b"different full document")
        with self.assertRaises(ValueError):
            source_pdf.pdf_document("GB 55037-2022")

    def test_missing_full_document_returns_404(self):
        self.full.unlink()
        with self.assertRaises(HTTPException) as error:
            main.pdf_response("GB 55037-2022")
        self.assertEqual(error.exception.status_code, 404)

    def test_page_bounds_and_auth_are_checked_before_redirect(self):
        with patch.object(main.auth, "valid", return_value=False):
            with self.assertRaises(HTTPException) as error:
                main.pdf("GB 55037-2022", source_page=43, kb_session="invalid")
            self.assertEqual(error.exception.status_code, 401)
        with patch.object(main.auth, "valid", return_value=True), patch.object(main, "_source", return_value=(self.source, 66)):
            with self.assertRaises(HTTPException) as error:
                main.pdf("GB 55037-2022", source_page=67, kb_session="valid")
            self.assertEqual(error.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
