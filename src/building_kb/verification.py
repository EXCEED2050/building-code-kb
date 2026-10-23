"""带内容指纹的原件核对记录；保留原始快照及其待核状态。"""

import hashlib
import json
from pathlib import Path


def fingerprint(chunk: dict) -> str:
    content = json.dumps(
        {k: chunk[k] for k in ("text", "tables", "page_idx")}, ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(content.encode()).hexdigest()


def load_reviews(chunks_dir: str) -> dict:
    base = Path(chunks_dir).parent
    path = base / "catalog" / "verification.json"
    if not path.exists():
        path = base / "verification.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def apply_review(chunk: dict, reviews: dict) -> dict:
    c = dict(chunk)
    book = reviews.get("books", {}).get(c["std_code"], {})
    c["mandatory_historical"] = book.get("mandatory_historical", False)
    notes = [c.get("caveat", ""), book.get("notice", "")]
    record = reviews.get("chunks", {}).get(c["chunk_id"])
    if record:
        if fingerprint(c) != record["expected_sha256"]:
            c["answerable"] = False
            notes.append("原始条文已变化，既有核对记录失效；暂停将此条转录用于 AI 作答。")
        else:
            c.update(record.get("replace", {}))
            c["answerable"] = not record.get("block_answer", False)
            c["source_review"] = record["result"]
            notes.append(record.get("note", ""))
        if not c.get("answerable", True):
            # 错误转录仍保留在原始 JSONL 以便复查；页面不再传播已知错值。
            c["text"] = "该条转录已发现错误或修订标记混入，暂停展示；请打开 PDF 原件核对。"
            c["tables"] = []
    c["caveat"] = "\n".join(note for note in notes if note)
    return c
