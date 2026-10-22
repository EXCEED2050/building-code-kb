"""生成人工校对页：左边切出来的 chunk，右边 PDF 原页图，按 page_idx 对齐。

表格里错一个数字，比答不出来危害大得多，所以默认只挑带表格的条文来核——
那是最容易出错、也最常被查的部分。

用法:
    python -m building_kb.commands.review --std "GB 50016-2014"           # 只核带表格的条文
    python -m building_kb.commands.review --std "GB 50016-2014" --all     # 全部条文
    python -m building_kb.commands.review --std "GB 50016-2014" --clause 5.5.17
"""

from __future__ import annotations

from building_kb import paths

import argparse
import csv
import html
import json
import re
from pathlib import Path

import fitz  # PyMuPDF

DPI = 130
# 校对页会被人拖着滚很久，图太大会卡；130dpi 足够看清表格里的数字


def find_pdf(catalog: Path, std_code: str) -> Path | None:
    root = paths.ROOT
    for r in csv.DictReader(catalog.open(encoding="utf-8-sig")):
        if r["std_code"] == std_code and r["file_path"].lower().endswith(".pdf"):
            p = root / r["file_path"]
            if p.exists():
                return p
    return None


def render_pages(pdf: Path, pages: set[int], out_dir: Path, tag: str) -> dict[int, str]:
    """把需要的页渲染成 PNG，返回 page_idx -> 相对路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf)
    mapping: dict[int, str] = {}
    try:
        for p in sorted(pages):
            if not 0 <= p < doc.page_count:
                continue
            name = f"{tag}_p{p:04d}.png"
            f = out_dir / name
            if not f.exists():
                doc[p].get_pixmap(dpi=DPI).save(f)
            mapping[p] = f"img/{name}"
    finally:
        doc.close()
    return mapping


# 数值格：纯数字、破折号、空。用来数每行末尾连续有几个数值格。
VALUE_CELL_RE = re.compile(r"^[\d.,\s—–\-~×]*$")

# 打勾格。这类表不是「标签列 + 数值列」，不适用下面的判据。
MARK_RE = re.compile(r"^[★☆√✓●○◎▲△■□•·]+$")


def suspect_rows(rows: list[list[str]]) -> set[int]:
    """找出列位可能对不上的行。

    PDF 转 Word 时合并单元格的 gridSpan 常常丢失：表 5.5.17 的
    「歌舞娱乐放映游艺场所」原本横跨 3 个名称列，转换器却只给了 1 列，
    又凭空补了一个「—」凑数——数值没错，但落到了错误的列下面。

    判据：数一行末尾连续几个数值格。正常行不会超过表头的数值列数，
    只会更少——「医疗建筑 | 高层 | 病房部分」用满三个标签列是合法的。
    所以只标【多于】众数的行，少的不动。不自动改数，改错比不改危害大。

    打勾矩阵（GB 50453 的「甲|乙|丙|丁」分类表：`- | ★ | - | -`）不适用：
    那里行末破折号的个数表示★打在第几列，本来就该逐行不同。
    按上面的判据数，23 行的表能标出 10 行「可疑」，全是假警报——
    而校对页的全部价值就在于标黄的行值得人去看。全库这类假警报 128 行。
    """
    if any(MARK_RE.match(c.strip()) for row in rows for c in row):
        return set()

    tails: list[int] = []
    for row in rows:
        cells = list(row)
        while cells and not cells[-1].strip():  # 行尾空格子是排版占位，不算数值
            cells.pop()
        n = 0
        for c in reversed(cells):
            if not VALUE_CELL_RE.match(c.strip()) or not c.strip():
                break
            n += 1
        tails.append(n)

    data = [t for t in tails if t > 0]
    if len(data) < 3:
        return set()
    mode = max(set(data), key=data.count)
    return {i for i, t in enumerate(tails) if t > mode}


def table_html(md: str) -> tuple[str, int]:
    """Markdown 表格转 <table>，返回 (html, 可疑行数)。"""
    rows = [
        [c.strip() for c in r.strip().strip("|").split("|")]
        for r in md.splitlines()
        if r.strip()
    ]
    if not rows:
        return "", 0
    bad = suspect_rows(rows)
    body = "".join(
        f'<tr class="{"bad" if i in bad else ""}">'
        + "".join(f"<td>{html.escape(c)}</td>" for c in row)
        + "</tr>"
        for i, row in enumerate(rows)
    )
    return f"<table class=tbl>{body}</table>", len(bad)


def render_chunk(c: dict, imgs: dict[int, str]) -> str:
    flags = []
    if c["is_mandatory"]:
        flags.append('<span class="flag mand">强条</span>')
    if c["section_type"] != "正文":
        flags.append(f'<span class="flag sec">{html.escape(c["section_type"])}</span>')
    if c["superseded"]:
        flags.append('<span class="flag sup">已被修订取代</span>')

    basis = (
        f'<div class=basis>强条依据：{html.escape(c["mandatory_basis"])}</div>'
        if c["mandatory_basis"]
        else ""
    )
    rendered = [table_html(t) for t in c["tables"]]
    tables = "".join(h for h, _ in rendered)
    n_bad = sum(n for _, n in rendered)
    if n_bad:
        flags.append(f'<span class="flag warn">{n_bad} 行列位存疑</span>')
    p = c["page_idx"]
    page = f"P{p + 1}" if p is not None else "无页码"
    # 表格常跨页（5.5.17 的条文在 P5，表在 P6），带表的条文连下一页一起给出
    want = [p] if p is None else ([p, p + 1] if c["tables"] else [p])
    shots = [imgs[n] for n in want if n in imgs]
    pane = (
        "".join(f'<img loading="lazy" src="{s}">' for s in shots)
        if shots
        else '<div class=nopage>该条未取到页码，无法比对原页</div>'
    )

    return f"""<section class=row id="c{html.escape(c['clause_no'])}">
  <div class=left>
    <h3>第 {html.escape(c['clause_no'])} 条 {''.join(flags)}
        <span class=page>{page}</span></h3>
    <div class=path>{html.escape(c['chapter_path'])}</div>
    <pre>{html.escape(c['text'])}</pre>
    {tables}
    {basis}
  </div>
  <div class=right>{pane}</div>
</section>"""


CSS = """
body{margin:0;font:15px/1.7 -apple-system,"PingFang SC",sans-serif;background:#f6f6f4;color:#1a1a1a}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid #ddd;padding:10px 18px;z-index:9}
header h1{margin:0;font-size:16px}
header .meta{color:#666;font-size:13px;margin-top:3px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:16px 18px;border-bottom:1px solid #e3e3e0;background:#fff;margin-bottom:8px}
.left h3{margin:0 0 4px;font-size:15px}
.path{color:#888;font-size:12px;margin-bottom:8px}
pre{white-space:pre-wrap;font:14px/1.75 "PingFang SC",sans-serif;margin:0 0 10px;background:#fafaf8;padding:10px;border-left:3px solid #c8c8c0;border-radius:2px}
.tbl{border-collapse:collapse;font-size:13px;margin:0 0 10px;width:100%}
.tbl td{border:1px solid #ccc;padding:3px 6px;vertical-align:middle}
.tbl tr:first-child td{background:#f0f0ec;font-weight:600}
.right img{width:100%;border:1px solid #ccc;border-radius:2px;background:#fff}
.nopage{color:#a00;font-size:13px;padding:20px;background:#fff6f6;border:1px dashed #d99}
.page{float:right;color:#666;font-size:13px;font-weight:400}
.flag{font-size:11px;padding:1px 6px;border-radius:9px;margin-left:6px;vertical-align:2px}
.mand{background:#c0392b;color:#fff}
.sec{background:#e8e4d8;color:#6b5a2b}
.sup{background:#777;color:#fff}
.tbl tr.bad td{background:#fff3cd;border-color:#e0a800}
.warn{background:#e0a800;color:#3a2c00}
.basis{color:#8a6d3b;font-size:12px;background:#fcf8e3;padding:4px 8px;border-radius:2px}
@media(max-width:1000px){.row{grid-template-columns:1fr}}
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成切条结果的人工校对页")
    ap.add_argument("--std", required=True, help='规范编号，如 "GB 50016-2014"')
    ap.add_argument("--chunks", default=str(paths.CHUNKS))
    ap.add_argument("--catalog", default=str(paths.CATALOG))
    ap.add_argument("--out", default=str(paths.REVIEWS))
    ap.add_argument("--all", action="store_true", help="核对全部条文，默认只核带表格的")
    ap.add_argument("--clause", default="", help="只核指定条号，逗号分隔")
    args = ap.parse_args(argv)

    safe = args.std.replace("/", "_").replace(" ", "_")
    src = Path(args.chunks) / f"{safe}.jsonl"
    if not src.exists():
        print(f"找不到切条结果: {src}")
        return 1

    chunks = [json.loads(l) for l in src.open(encoding="utf-8")]
    live = [c for c in chunks if not c["superseded"] and c["section_type"] != "条文说明"]

    if args.clause:
        want = {x.strip() for x in args.clause.split(",")}
        sel = [c for c in live if c["clause_no"] in want]
    elif args.all:
        sel = live
    else:
        sel = [c for c in live if c["tables"]]

    if not sel:
        print("没有符合条件的条文")
        return 1

    def sort_key(c: dict):
        parts = re.split(r"[.]", c["clause_no"])
        return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)

    sel.sort(key=sort_key)
    if not args.clause:
        # 列位存疑的排到最前面：人的时间有限，先核最可能错的
        sel.sort(key=lambda c: -sum(table_html(t)[1] for t in c["tables"]))

    out = Path(args.out)
    pdf = find_pdf(Path(args.catalog), args.std)
    imgs: dict[int, str] = {}
    if pdf:
        pages: set[int] = set()
        for c in sel:
            if c["page_idx"] is not None:
                pages.add(c["page_idx"])
                if c["tables"]:
                    pages.add(c["page_idx"] + 1)
        imgs = render_pages(pdf, pages, out / "img", safe)
        print(f"渲染 {len(imgs)} 页  ←  {pdf.name}")
    else:
        print("! 台账里没找到对应 PDF，右栏将为空")

    name = sel[0]["std_name"]
    scope = "全部条文" if args.all else ("指定条文" if args.clause else "带表格的条文")
    body = "\n".join(render_chunk(c, imgs) for c in sel)
    doc = f"""<!doctype html><meta charset=utf-8>
<title>校对 {html.escape(args.std)}</title><style>{CSS}</style>
<header><h1>{html.escape(args.std)} {html.escape(name)}</h1>
<div class=meta>校对范围：{scope} · 共 {len(sel)} 条 · 左为切条结果，右为 PDF 原页 ·
逐格比对数值，发现不一致请记下条号</div></header>
{body}"""

    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{safe}.html"
    f.write_text(doc, encoding="utf-8")
    print(f"校对页已生成：{f}  （{len(sel)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
