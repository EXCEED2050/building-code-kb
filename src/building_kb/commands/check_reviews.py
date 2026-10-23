"""核对校订记录与原始条文/PDF的指纹、页序；不代替内容视觉核对。"""

import hashlib
import json

import fitz

from building_kb import paths, source_pdf
from building_kb.verification import fingerprint


def main():
    root = paths.ROOT
    review = json.loads((root / "data/catalog/verification.json").read_text())
    raw = {}
    for path in (root / "data/chunks").glob("*.jsonl"):
        for line in path.read_text().splitlines():
            c = json.loads(line)
            raw[c["chunk_id"]] = c
    errors = []
    lengths = {}
    for code, book in review["books"].items():
        path = root / book["pdf_path"]
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != book["pdf_sha256"]
        ):
            errors.append(f"{code}: PDF缺失或指纹变化")
            continue
        with fitz.open(path) as pdf:
            lengths[code] = len(pdf)
        for extra in book.get("additional_sources", []):
            p = root / extra["path"]
            if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != extra["sha256"]:
                errors.append(f"{code}: 补充修订件缺失或指纹变化")
                continue
            with fitz.open(p) as pdf:
                if any(not 1 <= n <= len(pdf) for n in extra["pages"]):
                    errors.append(f"{code}: 补充修订件核对页序无效")
    for key, record in review["chunks"].items():
        c = raw.get(key)
        if c is None or fingerprint(c) != record["expected_sha256"]:
            errors.append(f"{key}: 原始转录缺失或指纹变化")
            continue
        if set(record.get("replace", {})) - {"text", "tables", "page_idx"}:
            errors.append(f"{key}: 校订记录包含未经允许的字段")
        if any(not 1 <= p <= lengths.get(c["std_code"], 0) for p in record["pdf_pages"]):
            errors.append(f"{key}: 核对页序无效")
    documents = json.loads((root / "data/catalog/source_documents.json").read_text())
    for code, record in documents.items():
        try:
            path, offset = source_pdf.pdf_document(code)
            with fitz.open(path) as pdf:
                if len(pdf) != record["page_count"] or not 0 <= offset < len(pdf):
                    errors.append(f"{code}: 完整阅读本页数或偏移无效")
                if record["mapped_source_pages"] != lengths.get(code) or offset + record[
                    "mapped_source_pages"
                ] > len(pdf):
                    errors.append(f"{code}: 原定位副本页数与完整本不匹配")
        except (FileNotFoundError, ValueError) as exc:
            errors.append(f"{code}: {exc}")
    print(
        json.dumps(
            {
                "verification": "FAIL" if errors else "PASS",
                "reviewed_chunks": len(review["chunks"]),
                "corrected_chunks": sum(bool(r.get("replace")) for r in review["chunks"].values()),
                "blocked_chunks": sum(
                    r.get("block_answer", False) for r in review["chunks"].values()
                ),
                "errors": errors,
            },
            ensure_ascii=False,
        )
    )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
