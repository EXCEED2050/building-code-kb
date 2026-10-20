"""核验私有协作条文快照：python -m tools.check_snapshot。"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def check(root: Path = ROOT) -> dict:
    manifest = json.loads((root / "kb/data/runtime-manifest.json").read_text(encoding="utf-8"))
    expected = {r["path"] for r in manifest["files"]}
    actual = {p.relative_to(root).as_posix() for p in (root / "kb/data/chunks").glob("*") if p.is_file()}
    if actual != {p for p in expected if p.startswith("kb/data/chunks/")}:
        raise ValueError("条文文件与快照清单不一致")
    ids, books = set(), set()
    count = pool = 0
    for row in manifest["files"]:
        rel = Path(row["path"])
        if rel.is_absolute() or ".." in rel.parts or not row["path"].startswith("kb/data/"):
            raise ValueError("清单路径超出数据目录")
        data = (root / rel).read_bytes()
        if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError(f"文件校验失败：{row['path']}")
        if rel.suffix != ".jsonl":
            continue
        for line in data.decode("utf-8").splitlines():
            record = json.loads(line)
            if record["chunk_id"] in ids:
                raise ValueError("发现重复条文标识")
            ids.add(record["chunk_id"])
            books.add(record["std_code"])
            count += 1
            pool += not record["superseded"] and record["status"] != "废止" and record["section_type"] != "条文说明"
    with (root / "kb/data/catalog.csv").open(encoding="utf-8-sig", newline="") as stream:
        catalog_records = sum(1 for _ in csv.DictReader(stream))
    result = {"books": len(books), "raw_chunks": count, "retrieval_chunks": pool,
              "catalog_records": catalog_records}
    if any(result[k] != manifest[k] for k in result):
        raise ValueError("实际数据数量与快照摘要不一致")
    return {"verification": "PASS", **result}


if __name__ == "__main__":
    print(json.dumps(check(), ensure_ascii=False))
