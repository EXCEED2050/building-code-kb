"""按明确白名单导出公开开发数据，不修改完整私有条文库。

从 kb/ 运行：python -m tools.export_public；核验：python -m tools.export_public --check。
公开范围的人工核对记录在 public-data/sources.json，不能按 GB 文件名前缀自动放行。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

KB = Path(__file__).resolve().parents[1]
PUBLIC = KB / "public-data"
FIELDS = ("chunk_id", "std_code", "std_name", "version", "clause_no", "chapter_path",
          "section_type", "text", "tables", "page_idx", "is_mandatory",
          "mandatory_basis", "superseded", "status", "caveat")


def sources() -> dict:
    doc = json.loads((PUBLIC / "sources.json").read_text(encoding="utf-8"))
    seen = set()
    for row in doc["standards"]:
        code = row["std_code"]
        host = urlsplit(row["official_source_url"]).hostname or ""
        if (not re.fullmatch(r"GB 55\d{3}-20\d{2}", code)
                or not row["full_text_mandatory"] or not host.endswith(".gov.cn")
                or row["file"] != code.replace(" ", "_") + ".jsonl" or code in seen):
            raise ValueError(f"公开白名单记录不符合要求：{code}")
        seen.add(code)
    return doc


def check() -> dict:
    doc = sources()
    manifest = json.loads((PUBLIC / "manifest.json").read_text(encoding="utf-8"))
    expected = {s["file"]: s for s in doc["standards"]}
    actual = {p.name for p in (PUBLIC / "chunks").iterdir() if p.is_file()}
    if actual != set(expected) or set(expected) != {f["file"] for f in manifest["files"]}:
        raise ValueError("公开文件清单与白名单不一致")
    ids = set()
    count = 0
    for record in manifest["files"]:
        content = (PUBLIC / "chunks" / record["file"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != record["sha256"]:
            raise ValueError(f"内容校验失败：{record['file']}")
        rows = [json.loads(line) for line in content.decode("utf-8").splitlines()]
        if len(rows) != record["chunks"]:
            raise ValueError("条文数不一致")
        for row in rows:
            if (row["std_code"] != expected[record["file"]]["std_code"]
                    or row["section_type"] != "正文" or row["superseded"]
                    or row["status"] == "废止" or not row["is_mandatory"]
                    or row["chunk_id"] in ids or set(row) != set(FIELDS)):
                raise ValueError("发现非白名单正文、重复条文或意外字段")
            ids.add(row["chunk_id"])
        count += len(rows)
    if count != manifest["chunks"] or len(expected) != manifest["books"]:
        raise ValueError("汇总数量不一致")
    return {"books": len(expected), "chunks": count, "verification": "PASS"}


def export() -> dict:
    doc = sources()
    chunks = PUBLIC / "chunks"
    chunks.mkdir(exist_ok=True)
    manifest = {"snapshot_date": doc["snapshot_date"], "books": len(doc["standards"]),
                "chunks": 0, "latest_status_verified": False, "files": []}
    catalog = io.StringIO(newline="")
    writer = csv.DictWriter(catalog, fieldnames=["file_path", "std_code", "std_name",
                                                "version_year", "status", "source_url"],
                            lineterminator="\n")
    writer.writeheader()
    for source in doc["standards"]:
        rows = []
        for line in (KB / "data" / "chunks" / source["file"]).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["std_code"] != source["std_code"]:
                raise ValueError("源文件中混入其他规范")
            if row["section_type"] != "正文" or row["superseded"] or row["status"] == "废止":
                continue
            row = {key: row[key] for key in FIELDS}
            # 效力属性来自该规范的全文强制公告；不改变 OCR 文本、数值或页码。
            row["is_mandatory"] = True
            row["mandatory_basis"] = "全文强制性工程建设规范，来源：" + source["official_source_url"]
            row["caveat"] = "开发数据快照；文本未逐条验收，现行状态及页码须对照官方原件核验。"
            rows.append(row)
        if not rows:
            raise ValueError("白名单规范没有正文")
        content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
        (chunks / source["file"]).write_bytes(content)
        manifest["files"].append({"file": source["file"], "std_code": source["std_code"],
                                  "chunks": len(rows), "bytes": len(content),
                                  "sha256": hashlib.sha256(content).hexdigest()})
        manifest["chunks"] += len(rows)
        writer.writerow({"file_path": "", "std_code": source["std_code"],
                         "std_name": source["std_name"], "version_year": source["std_code"][-4:],
                         "status": "待核", "source_url": source["official_source_url"]})
    (PUBLIC / "catalog.csv").write_text(catalog.getvalue(), encoding="utf-8")
    (PUBLIC / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
    return check()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="仅核验已导出的公开快照，无需私有语料")
    args = parser.parse_args()
    print(json.dumps(check() if args.check else export(), ensure_ascii=False))


if __name__ == "__main__":
    main()
