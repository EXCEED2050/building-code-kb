"""按 data/tiers.tsv 的分档，生成 building_kb.ingest.ocr 用的待办清单。

分档表只记「编号 → 档位」，文件路径一律回台账里查——文件名会改、会补新版本，
把路径抄进分档表，改一次名就有一本悄悄漏掉。

解析范围限定在 `建筑规范/标准库/`（见 --scope）。同目录之外的
`建筑规范/pdf`、`建筑规范/word`、`国内建筑规范`、`国际建筑规范` 一律不解析，
那几个目录里混着旧版本、Word 转存件和个人收集件，来源和版本都不可靠。

范围之外的书不是静悄悄消失：一本规范若只在范围外有 PDF，会进「范围外」清单
打印出来，并写进 var/queues/ocr_out_of_scope.txt。缩小范围会丢掉哪些东西，
必须看得见——尤其是那本在范围内只剩作废版本的。

用法:
    python -m building_kb.commands.queue                 # 生成 var/queues/ocr_t1.txt … t3.txt 并打印统计
    python -m building_kb.commands.queue --tier 1        # 只看一档
    python -m building_kb.commands.queue --scope ''      # 不限范围（回到收窄之前的行为）
"""

from __future__ import annotations

from building_kb import paths

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

NORM_RE = re.compile(r"[^A-Za-z0-9]")

# 只解析这个目录下的 PDF。用「目录片段」而不是绝对路径，库整体搬家也不用改。
SCOPE = "data/sources/标准库/"


def norm(code: str) -> str:
    return NORM_RE.sub("", code).upper()


def read_tiers(path: Path) -> list[tuple[str, str, str]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = (line.split("\t") + ["", ""])[:3]
        out.append((cells[0].strip(), cells[1].strip(), cells[2].strip()))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按档位生成 OCR 待办清单")
    ap.add_argument("--tiers", default=str(paths.CATALOG_DIR / "tiers.tsv"))
    ap.add_argument("--catalog", default=str(paths.CATALOG))
    ap.add_argument("--chunks", default=str(paths.CHUNKS))
    ap.add_argument("--out", default=str(paths.QUEUES))
    ap.add_argument("--tier", default="", help="只处理某一档")
    ap.add_argument("--scope", default=SCOPE,
                    help="只解析路径含此片段的 PDF；传空串表示不限")
    args = ap.parse_args(argv)

    rows = list(csv.DictReader(Path(args.catalog).open(encoding="utf-8-sig")))
    root = paths.ROOT
    files: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["std_code"]:
            files[norm(r["std_code"])].append(r)
    done = {norm(p.stem.replace("_", " ")) for p in Path(args.chunks).glob("*.jsonl")}

    buckets: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "pages": 0, "txt": 0})
    missing = []
    outside = []
    for tier, code, note in read_tiers(Path(args.tiers)):
        if args.tier and tier != args.tier:
            continue
        rs = [r for r in files.get(norm(code), []) if r["file_path"].lower().endswith(".pdf")]
        if not rs:
            missing.append((tier, code, note or "台账里没有对应的 PDF"))
            continue
        # 收窄到标准库。范围内一个 PDF 都没有的，单独记下来而不是默默跳过。
        if args.scope:
            inside = [r for r in rs if args.scope in r["file_path"]]
            if not inside:
                outside.append((tier, code, rs))
                continue
            rs = inside
        if norm(code) in done:
            continue
        s = stats[tier]
        s["n"] += 1
        s["pages"] += sum(int(r["page_count"] or 0) for r in rs)
        # 有文本层的不用跑 OCR，但仍要排进队列——building_kb.ingest.ocr 会自动走快通道
        s["txt"] += all(r["is_scanned"] != "是" for r in rs)
        for r in rs:
            buckets[tier].append((code, root / r["file_path"]))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for tier, items in sorted(buckets.items()):
        if tier == "-":
            continue
        f = out_dir / f"ocr_t{tier}.txt"
        f.write_text("".join(f"{p}\n" for _, p in items), encoding="utf-8")
        s = stats[tier]
        print(f"{f}  {s['n']} 本 / {len(items)} 个文件 / {s['pages']} 页"
              f"（其中 {s['txt']} 本有文本层，不用 OCR）"
              f"  预计 {s['pages'] * 10.5 / 3600:.1f} 小时")
    if missing:
        print("\n对不上台账的：")
        for tier, code, note in missing:
            print(f"  [{tier}] {code}  {note}")

    if outside:
        # 写成文件，便于事后比对「收窄到底丢了什么」
        rec = out_dir / "ocr_out_of_scope.txt"
        rec.write_text(
            "".join(f"{tier}\t{code}\t{r['file_path']}\n" for tier, code, rs in outside for r in rs),
            encoding="utf-8",
        )
        print(f"\n范围外（只在 {args.scope} 之外有 PDF，本次不解析）：{len(outside)} 本 → {rec}")
        for tier, code, rs in outside:
            where = ", ".join(sorted({Path(r["file_path"]).parent.name for r in rs}))
            print(f"  [{tier}] {code}  仅见于 {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
