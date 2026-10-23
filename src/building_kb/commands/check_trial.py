"""30题的检索与数据预检；不把预检结果当成人工验收或真实模型验收。"""

import json
from unittest.mock import patch

import jieba
from fastapi import HTTPException

from building_kb import paths
from building_kb.search.retrieve import Retriever
from building_kb.web import main as api


def main():
    jieba.setLogLevel(60)
    base = paths.ROOT
    cases = json.loads((paths.CATALOG_DIR / "trial_cases.json").read_text())["cases"]
    r = Retriever(str(base / "data/chunks"), chroma_dir=str(paths.VAR / "no-index"))
    records = []
    for case in cases:
        errors, hits = [], []
        query, reason = case["question"], None
        if case.get("check") == "missing_pdf_test":
            with (
                patch.object(api, "_sources", {}),
                patch.object(api.source_pdf, "resolve", return_value=None),
            ):
                try:
                    api.page_response("GB 50016-2014", 1)
                    errors.append("缺原件未报错")
                except HTTPException as exc:
                    if exc.status_code != 404:
                        errors.append("缺原件状态码错误")
        else:
            if "previous_ids" in case:
                body = api.FollowupRequest(
                    question=query,
                    chunk_ids=case["previous_ids"],
                    history=[
                        api.ConversationTurn(
                            question=case["previous_question"],
                            answer="此前回答仅供指代，不是本轮依据",
                        )
                    ],
                )
                query, reason = api.followup_query(r, body)
            if reason != case.get("expected_reason"):
                errors.append(f"追问范围状态不符：{reason}")
            if not reason:
                hits, _ = r.search(query, 8, rerank=False)
            for term in case.get("query_contains", []):
                if term not in query:
                    errors.append(f"追问检索主题缺失：{term}")
            if case.get("expect_empty") and hits:
                errors.append("无结果题返回了其他条文")
            if target := case.get("expected_id"):
                match = next((h.chunk for h in hits if h.chunk["chunk_id"] == target), None)
                if not match:
                    errors.append("前8条未命中目标")
                else:
                    text = match["text"] + "\n" + "\n".join(match["tables"])
                    if not match.get("answerable", True):
                        errors.append("目标仍被隔离")
                    for term in case["expected_text"]:
                        if term not in text:
                            errors.append(f"已核对的预期文字缺失：{term}")
                    for term in case.get("forbidden_text", []):
                        if term in text:
                            errors.append(f"错误转录残留：{term}")
                    if match["status"] != "待核":
                        errors.append("整库待核状态被错误升级")
        record = {
            "id": case["id"],
            "result": "FAIL" if errors else "PASS",
            "search_query": query,
            "reason": reason,
            "hit_ids": [h.chunk["chunk_id"] for h in hits],
            "errors": errors,
        }
        records.append(record)
        print(f"{case['id']}: {record['result']}" + ("；" + "；".join(errors) if errors else ""))
    failed = sum(bool(row["errors"]) for row in records)
    report = paths.REVIEWS / "trial-precheck-20261022.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {"mode": "关键词检索及数据预检，未调用生成模型", "cases": records},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(
        f"预检：{len(cases) - failed}/{len(cases)}；人工验收待签；记录：{report.relative_to(base)}"
    )
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
