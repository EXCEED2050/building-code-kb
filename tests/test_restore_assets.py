"""Published legacy archives must restore into the new layout without data loss."""

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from building_kb.commands.restore_assets import destination, restore


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.downloads = self.base / "downloads"
        self.downloads.mkdir()
        self.root = self.base / "project"
        self.root.mkdir()

    def archive(self, modern_manifest=False, member="建筑规范/pdf/test.pdf"):
        content = b"original PDF bytes"
        record = {
            "path": member,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        archive = self.downloads / "originals-part01.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr(member, content)
            z.writestr("ASSET_MANIFEST_" + archive.name + ".json", json.dumps({"files": [record]}))
        current = dict(record)
        if modern_manifest:
            current.update(path="data/sources/pdf/test.pdf", archive_path=member)
        self.manifest = {
            "files": [current],
            "assets": [
                {
                    "file": archive.name,
                    "files": 1,
                    "bytes": archive.stat().st_size,
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                }
            ],
        }
        self.write_manifest()
        return content

    def write_manifest(self):
        (self.downloads / "asset-manifest.json").write_text(json.dumps(self.manifest))

    def test_legacy_manifest_restores_to_new_layout_and_is_repeatable(self):
        content = self.archive()
        first = restore(self.downloads, self.root)
        self.assertEqual(first["restored"], 1)
        self.assertEqual((self.root / "data/sources/pdf/test.pdf").read_bytes(), content)
        self.assertFalse((self.root / "建筑规范").exists())
        self.assertEqual(restore(self.downloads, self.root)["unchanged"], 1)

    def test_new_manifest_matches_unchanged_embedded_legacy_manifest(self):
        self.archive(modern_manifest=True)
        self.assertEqual(restore(self.downloads, self.root)["restored"], 1)

    def test_dry_run_does_not_write_files(self):
        self.archive()
        self.assertEqual(restore(self.downloads, self.root, True)["checked_files"], 1)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_conflicting_local_document_is_preserved(self):
        self.archive()
        target = self.root / "data/sources/pdf/test.pdf"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"a newer document")
        with self.assertRaises(FileExistsError):
            restore(self.downloads, self.root)
        self.assertEqual(target.read_bytes(), b"a newer document")

    def test_mismatched_hash_or_remapped_destination_is_rejected(self):
        self.archive(modern_manifest=True)
        self.manifest["files"][0]["path"] = "data/sources/pdf/other.pdf"
        self.write_manifest()
        with self.assertRaises(ValueError):
            restore(self.downloads, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_traversal_secrets_and_outside_symlinks_are_rejected(self):
        for relative in [
            "../escape",
            "/tmp/escape",
            "建筑规范/../../escape",
            "data/sources/.env",
            "data/chunks/a.jsonl",
            "kb/data/ocr/../.env",
        ]:
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                destination(self.root, relative)
        (self.root / "data").symlink_to(self.base)
        with self.assertRaises(ValueError):
            destination(self.root, "建筑规范/pdf/test.pdf")

    def test_all_three_published_source_roots_are_mapped(self):
        for old, new in [
            ("建筑规范/a.pdf", "data/sources/a.pdf"),
            ("MinerU/a.json", "data/processed/mineru/a.json"),
            ("kb/data/ocr/a.json", "data/processed/ocr/a.json"),
        ]:
            self.assertEqual(destination(self.root, old), self.root / new)
