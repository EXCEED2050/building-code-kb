"""按台账批量切条：为每本规范配对 Word（正文）+ MinerU（页码），然后调用切条器。

配对靠规范编号归一后的 key，因为同一本规范在 Word 文件名、MinerU 目录名、
台账里的写法都不一样（"GB+50016-2014" / "GB 50016-2014" / "GB 50016-2014"）。

用法:
    python -m building_kb.ingest.batch --catalog data/catalog/catalog.csv \
        --word-dir data/sources/word --mineru-dir data/processed/mineru --out data/chunks \
        --only "GB 50016-2014,GB 50160-2008"
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from building_kb import paths

from . import pagefind
from .chunker import build, build_from_mineru, cut_back_matter

# 归一：去掉所有非字母数字，大写。"GB/T 50378-2019" -> "GBT500782019"
NORM_RE = re.compile(r"[^A-Za-z0-9]")


def norm(code: str) -> str:
    return NORM_RE.sub("", code).upper()


def index_word_files(word_dir: Path) -> dict[str, Path]:
    """建 归一编号 -> docx 路径。同编号有多个时取文件名最长的（通常信息最全）。"""
    out: dict[str, Path] = {}
    for p in sorted(word_dir.rglob("*.docx")):
        if p.name.startswith("~$"):
            continue
        # 去掉导出时间戳再归一，否则时间戳会混进 key
        stem = re.sub(r"_\d{12,14}$", "", p.stem)
        key = norm(stem)
        prev = out.get(key)
        if prev is None or len(p.name) > len(prev.name):
            out[key] = p
    return out


def index_mineru(mineru_dir: Path) -> dict[str, Path]:
    """建 归一编号 -> content_list.json 路径。

    在线版 MinerU 把文件直接放在目录里，本地 building_kb.ingest.ocr 跑出来的放在 auto/ 下，
    所以用 rglob 一并兜住。
    """
    out: dict[str, Path] = {}
    if not mineru_dir.is_dir():
        return out
    for d in sorted(mineru_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        cl = sorted(d.rglob("*_content_list.json"))
        if not cl:
            continue
        stem = d.name.split(".pdf-")[0].split(".PDF-")[0]
        out.setdefault(norm(stem), cl[0])
    return out


def match(code: str, table: dict[str, Path]) -> Path | None:
    """先精确匹配归一编号，再退回「表中 key 以编号开头」的前缀匹配。

    前缀匹配是必要的：Word 文件名常带着规范全名
    （"GB+50016-2014+建筑设计防火规范(2018版)"），归一后比纯编号长。
    """
    k = norm(code)
    if k in table:
        return table[k]
    cands = [v for kk, v in table.items() if kk.startswith(k)]
    return cands[0] if len(cands) == 1 else None


def load_notes(path: Path) -> dict[tuple[str, str], str]:
    """读条文提醒表：(规范号, 条号) → 引用前必须知道的事。"""
    if not path.exists():
        return {}
    out: dict[tuple[str, str], str] = {}
    with path.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            code = (row.get("std_code") or "").strip()
            no = (row.get("clause_no") or "").strip()
            note = (row.get("caveat") or "").strip()
            if code and not code.startswith("#") and no and note:
                out[(norm(code), no)] = note
    return out


def apply_notes(
    chunks: list, code: str, notes: dict[tuple[str, str], str]
) -> tuple[int, list[str]]:
    """把提醒挂到条文上，并报告哪些条号没挂上。

    挂不上一般是条号写错或那本还没入库。必须报出来——
    一条「已被修订」的提醒静悄悄地失效，比从来没写过更危险。
    """
    want = {no for (c, no) in notes if c == norm(code)}
    if not want:
        return 0, []
    hit = 0
    for ch in chunks:
        if ch.clause_no in want and (note := notes.get((norm(code), ch.clause_no))):
            ch.caveat = note
            hit += 1
    got = {ch.clause_no for ch in chunks if ch.caveat}
    return hit, sorted(want - got)


def dedupe_ids(chunks: list) -> list[str]:
    """保证一本书内 chunk_id 唯一，返回撞过的 id。

    落在这里而不是各切条路径里：chunk_id 唯一是**写盘时的不变量**，
    三条路径（Word / OCR 三级 / OCR 两级）各自保证一次，就会漏掉第四条。
    """
    seen: Counter[str] = Counter()
    dups: list[str] = []
    for c in chunks:
        seen[c.chunk_id] += 1
        if seen[c.chunk_id] > 1:
            if seen[c.chunk_id] == 2:
                dups.append(c.chunk_id)
            c.chunk_id = f"{c.chunk_id}~{seen[c.chunk_id]}"
    return dups


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按台账批量切条")
    ap.add_argument("--catalog", default=str(paths.CATALOG))
    ap.add_argument("--word-dir", default=str(paths.WORD_DIR))
    ap.add_argument("--mineru-dir", default=str(paths.MINERU_DIR))
    ap.add_argument(
        "--ocr-dir",
        default=str(paths.OCR_DIR),
        help="building_kb.ingest.ocr 的产物目录；没有 Word 源的规范从这里走 OCR 路径",
    )
    ap.add_argument(
        "--notes", default=str(paths.CATALOG_DIR / "clause_notes.tsv"), help="单条条文的引用提醒"
    )
    ap.add_argument("--out", default=str(paths.CHUNKS))
    ap.add_argument("--only", default="", help="逗号分隔的规范编号，留空则处理全部有编号的")
    args = ap.parse_args(argv)

    rows = list(csv.DictReader(Path(args.catalog).open(encoding="utf-8-sig")))
    words = index_word_files(Path(args.word_dir))
    minerus = index_mineru(Path(args.mineru_dir))
    ocrs = index_mineru(Path(args.ocr_dir))
    notes = load_notes(Path(args.notes))
    root = paths.ROOT
    pdfs: dict[str, Path] = {}
    for r in rows:
        if r["file_path"].lower().endswith(".pdf") and (p := root / r["file_path"]).exists():
            pdfs.setdefault(norm(r["std_code"]), p)
    print(
        f"Word 索引 {len(words)} 项，MinerU 索引 {len(minerus)} 项，"
        f"OCR 索引 {len(ocrs)} 项，PDF {len(pdfs)} 项\n"
    )

    wanted = {norm(c) for c in args.only.split(",") if c.strip()}

    # 台账里一本规范有 PDF/Word 多行，按编号去重
    seen: set[str] = set()
    targets: list[dict] = []
    for r in rows:
        code = r["std_code"].strip()
        if not code or norm(code) in seen:
            continue
        if wanted and norm(code) not in wanted:
            continue
        seen.add(norm(code))
        targets.append(r)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ok, skipped = 0, []
    summary: list[dict] = []

    for r in targets:
        code = r["std_code"].strip()
        w = match(code, words)
        m = match(code, minerus)
        o = match(code, ocrs)
        if w is None and o is None:
            skipped.append((code, "无 Word 源，也没跑过 OCR"))
            continue

        print(f"[{code}] {r['std_name']}" + ("" if w else "   （OCR 源）"))
        if w and m is None:
            print("  ! 无 MinerU 页码源，chunk 将不带页码")

        meta = {
            "std_code": code,
            "std_name": r["std_name"],
            "version": r["version_year"],
            "status": r["status"] or "待核",
        }
        try:
            chunks = build(w, m, meta) if w else build_from_mineru(o, meta)
        except Exception as exc:  # noqa: BLE001 - 单本失败不应中断整批
            print(f"  ! 切条失败: {exc}")
            skipped.append((code, f"切条失败: {exc}"))
            continue

        if n_back := cut_back_matter(chunks):
            print(f"  截掉书末附件（用词说明 / 引用标准名录）{n_back} 处")

        if notes:
            hit, missed = apply_notes(chunks, code, notes)
            if hit:
                print(f"  挂上条文提醒 {hit} 条")
            for no in missed:
                print(f"  ! 提醒表里的 {code} 第{no}条没在切出来的块里找到")

        # MinerU 没覆盖到的（13 本企标全部如此）直接去 PDF 里按正文找页码
        if any(c.page_idx is None for c in chunks) and (pdf := pdfs.get(norm(code))):
            try:
                hit, miss = pagefind.apply_to(chunks, pdf)
                if hit:
                    print(f"  PDF 定位页码: 命中 {hit} 条，仍缺 {miss} 条")
            except Exception as exc:  # noqa: BLE001 - 页码是增益，失败不该拖垮切条
                print(f"  ! PDF 定位页码失败: {exc}")

        if not chunks:
            # 空文件会伪装成「这本已经入库了」：后续 batch、building_kb.commands.queue、
            # building_kb.commands.chunk_new 都靠 glob 认已完成，看到文件就不再排它。
            # 于是库里多一本「查得到书名、查不到条文」的规范，还没人会发现。
            # 宁可这本缺着并留在待办里。
            skipped.append((code, "切出 0 条，未写文件"))
            print("  ! 切出 0 条，不写文件（避免伪装成已入库）")
            continue

        if dups := dedupe_ids(chunks):
            # 没有条号的文件会撞 id：企标里「第5章」整章是一张术语表，
            # 每段都落在 #正文#5 上。这些段落内容各不相同，不能合并，
            # 只能让 id 唯一——Chroma 的 upsert 见到重复 id 直接抛错，
            # 建索引会停在半路；就算不抛，后一条也会把前一条顶掉，
            # 静悄悄少几十条。宁可 id 难看，不可少条。
            print(f"  条号重复 {len(dups)} 个，已加后缀区分（{'、'.join(dups[:3])}…）")

        f = out_dir / f"{code.replace('/', '_').replace(' ', '_')}.jsonl"
        with f.open("w", encoding="utf-8") as fh:
            for c in chunks:
                fh.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")

        live = [c for c in chunks if not c.superseded and c.section_type != "条文说明"]
        no_page = sum(1 for c in live if c.page_idx is None)
        summary.append(
            {
                "code": code,
                "total": len(chunks),
                "live": len(live),
                "mandatory": sum(1 for c in live if c.is_mandatory),
                "tables": sum(1 for c in live if c.tables),
                "no_page": no_page,
            }
        )
        print(f"  -> {len(chunks)} 块，检索池 {len(live)}，缺页码 {no_page}\n")
        ok += 1

    print("=" * 74)
    print(f"{'规范编号':<18}{'总块':>6}{'检索池':>7}{'强条':>6}{'带表':>6}{'缺页码':>7}")
    for s in summary:
        print(
            f"{s['code']:<18}{s['total']:>6}{s['live']:>7}"
            f"{s['mandatory']:>6}{s['tables']:>6}{s['no_page']:>7}"
        )
    print("=" * 74)
    print(f"成功 {ok} 本，跳过 {len(skipped)} 本")
    for code, why in skipped:
        print(f"  - {code}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
