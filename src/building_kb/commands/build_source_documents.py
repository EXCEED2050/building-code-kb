"""从已核对指纹的分卷重建完整阅读本，保持原始页码定位副本。"""

import hashlib
import json

import fitz

from building_kb import paths


def main():
    root = paths.ROOT
    manifest = root / "data/catalog/source_documents.json"
    records = json.loads(manifest.read_text(encoding="utf-8"))
    for code, record in records.items():
        target = root / record["full_pdf_path"]
        doc = fitz.open()
        for part in record["parts"]:
            path = root / part["path"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != part["sha256"]:
                raise ValueError(f"{path.name}: 分卷指纹变化，停止重建")
            with fitz.open(path) as pdf:
                doc.insert_pdf(pdf)
        if len(doc) != record["page_count"]:
            raise ValueError(f"{code}: 分卷合并页数变化，停止重建")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".building.pdf")
        doc.save(temporary, garbage=3, deflate=True, no_new_id=True)
        doc.close()
        temporary.replace(target)
        record["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        print(f"{code}: 完整阅读本 {record['page_count']} 页，已重建")
    manifest.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
