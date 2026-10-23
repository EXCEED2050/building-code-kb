import unittest

from test_followup import clause

from building_kb.qa.answer import format_context
from building_kb.search.retrieve import Hit
from building_kb.verification import apply_review, fingerprint


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.raw = clause("GB 50016-2014", "5.1.8")
        self.review = {
            "chunks": {
                self.raw["chunk_id"]: {
                    "expected_sha256": fingerprint(self.raw),
                    "replace": {"text": "与原件对照后的文字"},
                    "result": "转录已对照PDF校订",
                }
            }
        }

    def test_review_changes_only_matching_content_and_keeps_pending_status(self):
        reviewed = apply_review(self.raw, self.review)
        self.assertEqual(reviewed["text"], "与原件对照后的文字")
        self.assertEqual(reviewed["status"], "待核")
        self.assertNotEqual(self.raw["text"], reviewed["text"])

    def test_stale_review_blocks_display_and_model_context(self):
        changed = dict(self.raw, text="新的未经核对的内容")
        reviewed = apply_review(changed, self.review)
        self.assertFalse(reviewed["answerable"])
        self.assertNotIn("新的未经核对", reviewed["text"])
        self.assertEqual(format_context([Hit(reviewed, 1, "测试")]), "")

    def test_historical_mandatory_flag_is_not_current_mandate(self):
        raw = dict(self.raw, is_mandatory=True)
        reviewed = apply_review(
            raw,
            {
                "books": {
                    raw["std_code"]: {
                        "mandatory_historical": True,
                        "notice": "原强条相关条款已调整",
                    }
                }
            },
        )
        context = format_context([Hit(reviewed, 1, "测试")])
        self.assertIn("【原出版物强条标记】", context)
        self.assertNotIn("【强制性条文】", context)
