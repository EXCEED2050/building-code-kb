"""逐轮检索、缺失条号与说明配对的回归检查。"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException

from building_kb.web import main
from building_kb.search.retrieve import Retriever, parse_query


def clause(code, number, kind="正文"):
    return {
        "chunk_id": f"{code}#{kind}#{number}", "std_code": code,
        "std_name": code, "version": "", "clause_no": number,
        "chapter_path": "测试章节", "section_type": kind,
        "text": f"{number} 测试内容", "tables": [], "page_idx": 0,
        "is_mandatory": False, "mandatory_basis": "", "superseded": False,
        "status": "待核", "caveat": "",
    }


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        rows = [clause("GB 50016-2014", "5.1.8"),
                clause("GB 55037-2022", "7.3.1"),
                clause("GB 55037-2022", "7.3.1", "条文说明")]
        Path(self.temp.name, "sample.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8")
        self.r = Retriever(self.temp.name, chroma_dir=self.temp.name + "/missing")
        self.patches = [patch.object(main, "_r", self.r),
                        patch.object(main, "CHUNKS", self.temp.name),
                        patch.object(main, "_explanations", None),
                        patch.object(main.auth, "valid", return_value=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def events(self, response):
        async def collect():
            return [json.loads(block.split("data: ", 1)[1])
                    async for block in response.body_iterator]
        return asyncio.run(collect())

    def test_followup_retrieves_new_standard_instead_of_old_ids(self):
        prompts = []

        class Model:
            def stream(self, system, prompt):
                prompts.append(prompt)
                yield "已找到 [GB 55037-2022 第7.3.1条 P1]"

        body = main.FollowupRequest(
            question="GB 55037 第7.3.1条？",
            chunk_ids=["GB 50016-2014#正文#5.1.8"],
            history=[main.ConversationTurn(question="GB 50016 第5.1.8条？", answer="旧回答")],
        )
        with patch.object(main.providers, "get_llm", return_value=Model()):
            events = self.events(main.followup(body, kb_session="valid"))
        self.assertEqual(events[0]["hits"][0]["std_code"], "GB 55037-2022")
        self.assertEqual(events[0]["hits"][0]["status"], "待核")
        self.assertNotIn("GB 50016-2014 第5.1.8条", prompts[0].split("【此前问答", 1)[0])
        self.assertIn("【数据状态】待核", prompts[0])

    def test_missing_exact_clause_does_not_call_model(self):
        body = main.FollowupRequest(
            question="GB 55037 第99.99.99条？",
            chunk_ids=["GB 50016-2014#正文#5.1.8"],
            history=[main.ConversationTurn(question="第一轮", answer="旧回答")],
        )
        with patch.object(main.providers, "get_llm") as model:
            events = self.events(main.followup(body, kb_session="valid"))
        self.assertEqual(events[0]["hits"], [])
        self.assertEqual(events[-1]["reason"], "no_hits")
        model.assert_not_called()

    def test_short_reference_researches_with_single_previous_standard(self):
        body = main.FollowupRequest(
            question="那这个呢？", chunk_ids=["GB 50016-2014#正文#5.1.8"],
            history=[main.ConversationTurn(question="上一轮", answer="旧回答")],
        )
        with patch.object(self.r, "search", wraps=self.r.search) as search:
            self.events(main.followup(body, kb_session="valid"))
        self.assertEqual(search.call_args.args[0], "GB 50016-2014 那这个呢？")

    def test_implicit_clause_keeps_previous_standard_and_missing_clause_stays_missing(self):
        body = main.FollowupRequest(question="第7.3.1条呢？",
            chunk_ids=["GB 50016-2014#正文#5.1.8"],
            history=[main.ConversationTurn(question="GB 50016 第5.1.8条？", answer="旧回答")])
        with patch.object(main.providers, "get_llm") as model:
            events = self.events(main.followup(body, kb_session="valid"))
        self.assertEqual(events[0]["search_query"], "GB 50016-2014 第7.3.1条呢？")
        self.assertEqual(events[0]["hits"], [])
        model.assert_not_called()

    def test_short_semantic_followup_preserves_topic_without_old_clause_filter(self):
        body = main.FollowupRequest(question="那地下呢？",
            chunk_ids=["GB 55037-2022#正文#7.3.1"],
            history=[main.ConversationTurn(question="GB 55037 第7.3.1条 疏散净宽度？", answer="旧回答")])
        query, reason = main.followup_query(self.r, body)
        self.assertIsNone(reason)
        self.assertIn("疏散净宽度", query)
        self.assertNotIn("7.3.1", query)
        self.assertEqual(parse_query(query)["std_codes"], ["GB 55037-2022"])

    def test_explicit_full_name_does_not_inherit_old_standard(self):
        self.r.names["建筑防火通用规范"] = "GB 55037-2022"
        body = main.FollowupRequest(question="《建筑防火通用规范》第7.3.1条呢？",
            chunk_ids=["GB 50016-2014#正文#5.1.8"],
            history=[main.ConversationTurn(question="GB 50016 第5.1.8条？", answer="旧回答")])
        self.assertEqual(main.followup_query(self.r, body), (body.question, None))

    def test_ambiguous_reference_requests_scope_without_calling_model(self):
        body = main.FollowupRequest(question="那地下呢？",
            chunk_ids=["GB 50016-2014#正文#5.1.8", "GB 55037-2022#正文#7.3.1"],
            history=[main.ConversationTurn(question="疏散净宽度？", answer="旧回答")])
        with patch.object(main.providers, "get_llm") as model:
            events = self.events(main.followup(body, kb_session="valid"))
        self.assertEqual(events[-1]["reason"], "needs_scope")
        model.assert_not_called()

    def test_reference_after_no_evidence_round_does_not_revive_older_sources(self):
        body = main.FollowupRequest(question="那地下呢？", chunk_ids=[],
            history=[main.ConversationTurn(question="GB 50016 第5.1.8条？", answer="旧回答"),
                     main.ConversationTurn(question="GB 55037 第99.99.99条？", answer="本轮未找到")])
        with patch.object(main.providers, "get_llm") as model:
            events = self.events(main.followup(body, kb_session="valid"))
        self.assertEqual(events[0]["hits"], [])
        self.assertEqual(events[-1]["reason"], "needs_scope")
        model.assert_not_called()

    def test_explicit_year_and_standard_number_are_not_prefix_matches(self):
        for query in ("GB 50016-2025 第5.1.8条", "GB 5001 第5.1.8条", "GB/T 50016 第5.1.8条",
                      "GB 50016-2025 测试内容", "GB 5001 测试内容"):
            hits, _ = self.r.search(query)
            self.assertEqual(hits, [], query)
        hits, _ = self.r.search("GB 50016－2014 第5.1.8条")
        self.assertEqual(hits[0].chunk["std_code"], "GB 50016-2014")

    def test_explanation_is_separate_candidate(self):
        result = main._explanation("GB 55037-2022#正文#7.3.1")
        self.assertEqual(result["status"], "待核")
        self.assertEqual(result["clause_no"], "7.3.1")
        self.assertIsNone(main._explanation("GB 50016-2014#正文#5.1.8"))

    def test_blocked_transcription_is_not_sent_to_model(self):
        self.r.pool[0]["answerable"] = False
        body = main.FollowupRequest(
            question="GB 50016 第5.1.8条？",
            history=[main.ConversationTurn(question="第一轮", answer="旧回答")],
        )
        with patch.object(main.providers, "get_llm") as model:
            events = self.events(main.followup(body, kb_session="valid"))
        self.assertEqual(events[-1]["reason"], "unreliable_sources")
        model.assert_not_called()

    def test_missing_pdf_reports_unavailable_without_moving_original(self):
        with patch.object(main, "_sources", {}), patch.object(main.source_pdf, "resolve", return_value=None):
            with self.assertRaises(HTTPException) as error:
                main.page_response("GB 50016-2014", 1)
        self.assertEqual(error.exception.status_code, 404)
        self.assertIn("PDF 原件", error.exception.detail)

    def test_chunk_routes_accept_standard_codes_with_slash(self):
        routes = {r.path: r for r in main.app.routes}
        for path in ("/api/chunk/{chunk_id:path}", "/api/explanation/{chunk_id:path}"):
            prefix = path.split("{", 1)[0]
            self.assertIsNotNone(routes[path].path_regex.fullmatch(prefix + "SH/T_3186-2017#正文#5.1.4"))


if __name__ == "__main__":
    unittest.main()
