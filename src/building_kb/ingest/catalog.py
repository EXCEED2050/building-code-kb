"""扫描规范文件，生成台账 catalog.csv。

台账是整个知识库的地基：status（现行/废止/被代替）错一行，
就可能让人按废止的规范做设计。脚本只负责能自动判定的字段，
status 与 superseded_by 留空，必须人工核对后填写。

用法:
    python -m building_kb.ingest.catalog --roots data/sources --out data/catalog/catalog.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import fitz  # PyMuPDF

from building_kb import paths

DOC_SUFFIXES = {".pdf", ".doc", ".docx"}

# 抽样页中可提取字符数低于此值即判为扫描件（需 OCR）
SCANNED_CHAR_THRESHOLD = 300

# 规范编号识别。顺序有意义：先匹配更具体的模式。
# 分隔符统一用 [\s_+-]，因为同一本规范在不同文件名里写法不一
# （"GB 50016-2014" / "GB+50453-2008" / "GBT 50378-2019" / "GB_T 50934-2013"）
CODE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 中石化企业标准：DEP-T-AR2101-2015
    ("企标", re.compile(r"\b(DEP[-_][A-Z][-_]?[A-Z]{0,2}\d{4}[-_]\d{4})", re.I)),
    # 中石化企业标准（另一套编号）：Q/SH 0841-2022，文件名里常写成 Q_SH
    ("QSH", re.compile(r"\bQ[\s_/+-]*SH[\s_+-]*(\d{3,5})(?:[\s_+-]*(\d{4}))?", re.I)),
    # 国标/行标带年号：GB 50016-2014、GB/T 50934-2013、JGJ 25-2010、GB 15763.1-2009
    # 体系名的排列顺序有意义：长的在前，否则 GBZ 会先被 GB 吃掉一半。
    (
        "标准",
        re.compile(
            r"\b(GBZ|GB|JGJ|JG|SH|CJJ|CJ|CECS|TCECS|HG|SY|AQ)"  # 标准体系
            r"(?:[\s_+-]*[/_]?[\s_+-]*T)?"  # 可选的 /T 推荐性标记
            # 序号短到一位的真实存在（GBZ 1、JGJ 25）；「.1」是分部标准
            # （GB 15763.1/.2/.3 是防火玻璃/钢化玻璃/夹层玻璃三本不同的标准，
            #   丢掉这一位会把三本并成一本）
            r"[\s_+-]*(\d{1,5}(?:\.\d{1,2})?)"  # 标准序号
            r"(?:[\s_+-]*(\d{4}))?",  # 可选年号
            re.I,
        ),
    ),
    # 标准设计图集：22HM001-1、05BG213-2024
    ("图集", re.compile(r"\b(\d{2}[A-Z]{2,3}\d{3}(?:[-‑]\d{1,4})?)\b")),
]

CATEGORY_BY_SYSTEM = {
    "GB": "国标",
    "GBZ": "国标",
    "JGJ": "行标",
    "JG": "行标",
    "SH": "行标",
    "CJJ": "行标",
    "CJ": "行标",
    "CECS": "协会标准",
    "TCECS": "协会标准",
    "HG": "行标",
    "SY": "行标",
    "AQ": "行标",
}

INTERNATIONAL_HINTS = ("NFPA", "IBC", "ASCE", "API ", "ASTM", "EN 1", "ISO ")


@dataclass
class Record:
    file_path: str
    std_code: str = ""
    std_name: str = ""
    version_year: str = ""
    status: str = ""  # 人工填：现行 / 废止 / 被代替
    superseded_by: str = ""  # 人工填
    category: str = ""
    has_clause_notes: str = ""
    is_scanned: str = ""
    page_count: str = ""
    sha256: str = ""
    dup_group: str = ""  # 同一 sha256 的文件共用一个组号
    issue: str = ""  # 自动发现的问题：DRM 加密、无法打开等
    note: str = ""


def sha256_of(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk_size):
            h.update(block)
    return h.hexdigest()


def normalize_code(system: str, serial: str, year: str, recommended: bool) -> str:
    """把各种写法归一成 'GB/T 50934-2013' 这种规范写法。"""
    system = system.upper()
    code = f"{system}/T" if recommended else system
    out = f"{code} {serial}"
    if year:
        out += f"-{year}"
    return out


def parse_code(filename: str) -> tuple[str, str, str]:
    """从文件名解析 (std_code, version_year, category)。解析不出就返回空串。"""
    for kind, pattern in CODE_PATTERNS:
        m = pattern.search(filename)
        if not m:
            continue
        if kind == "企标":
            raw = m.group(1).replace("_", "-").upper()
            year = raw.rsplit("-", 1)[-1]
            return raw, year, "企标"
        if kind == "QSH":
            serial, year = m.group(1), m.group(2) or ""
            return f"Q/SH {serial}" + (f"-{year}" if year else ""), year, "企标"
        if kind == "图集":
            return m.group(1).upper(), "", "图集"
        system, serial, year = m.group(1), m.group(2), m.group(3) or ""
        # 判断是否推荐性标准：编号段里出现独立的 T
        head = filename[m.start() : m.end()]
        recommended = bool(re.search(r"[/_\s+-]T\b|[A-Z]T[\s_+-]*\d", head, re.I))
        code = normalize_code(system, serial, year, recommended)
        return code, year, CATEGORY_BY_SYSTEM.get(system.upper(), "其他")
    return "", "", ""


def parse_name(filename: str, std_code: str) -> str:
    """去掉编号、扩展名、导出时间戳后剩下的部分当作规范名称。"""
    name = Path(filename).stem
    # 去掉形如 _20250630105023 的导出时间戳
    name = re.sub(r"_\d{12,14}$", "", name)
    # 大文件被拆成「_部分1…_部分19」上传，这是切割痕迹不是规范名的一部分
    name = re.sub(r"[_\s-]*部分\d+$", "", name)
    # 去掉编号本身（用宽松模式，因为文件名里的写法和归一后的不一定一致）
    for pat in (r"^[A-Z/_+\s-]*\d{1,5}(?:\.\d{1,2})?(?:[\s_+-]*\d{4})?", r"^DEP[-_][^\s]*"):
        name = re.sub(pat, "", name, count=1, flags=re.I)
    # 有的 Word 是从网页导出的，文件名用 + 当分隔符
    name = name.replace("+", " ").strip(" -_：:")
    # 括号不能连着分隔符一起剥：「建筑设计防火规范(2018版)」剥完成了
    # 「建筑设计防火规范(2018版」，而规范名会出现在每一条引用里。
    # 只去掉首尾那种落了单的括号。
    if name.startswith(("(", "（")) and name.count(")") + name.count("）") == 0:
        name = name[1:]
    if name.endswith((")", "）")) and name.count("(") + name.count("（") == 0:
        name = name[:-1]
    return " ".join(name.split()).strip(" -_：:")


def probe_pdf(path: Path) -> tuple[str, str, str]:
    """返回 (page_count, is_scanned, issue)。

    扫描件判定：取文档 1/4、1/2、3/4 三处的页面，累计可提取字符数。
    只看首页不可靠——规范的封面往往是图片，正文却是文本层。
    """
    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001 - 需要把原因写进台账
        msg = str(exc).strip()
        if "no objects found" in msg or "cannot recognize" in msg.lower():
            return "", "", "无法打开：疑似 DRM 加密或非 PDF 格式"
        return "", "", f"无法打开：{msg[:80]}"

    try:
        if doc.needs_pass:
            return str(doc.page_count), "", "加密 PDF，需要口令"
        n = doc.page_count
        if n == 0:
            return "0", "", "空文档"
        sample_idx = sorted({n // 4, n // 2, (3 * n) // 4})
        chars = sum(len(doc[i].get_text().strip()) for i in sample_idx if i < n)
        return str(n), "是" if chars < SCANNED_CHAR_THRESHOLD else "否", ""
    finally:
        doc.close()


def build_record(path: Path, root: Path) -> Record:
    filename = path.name
    std_code, year, category = parse_code(filename)

    if not category and any(h.lower() in filename.lower() for h in INTERNATIONAL_HINTS):
        category = "国际"

    rec = Record(
        file_path=path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path),
        std_code=std_code,
        std_name=parse_name(filename, std_code),
        version_year=year,
        category=category,
        has_clause_notes="是" if "条文说明" in filename else "",
        sha256=sha256_of(path),
    )

    if path.suffix.lower() == ".pdf":
        rec.page_count, rec.is_scanned, rec.issue = probe_pdf(path)
    else:
        rec.note = "Word 文档，走 python-docx 直接抽取"

    return rec


def load_overrides(path: Path) -> dict[str, dict[str, str]]:
    """读人工订正表：文件名 → 要覆盖的字段。

    台账的编号和名称全靠文件名推断，而文件名是会错的——`GB 50033-2013` 那份
    封面写的是《建筑采光设计标准》，文件名却叫「建筑地面设计规范」。
    这类错不能靠改脚本躲过去（下一份错法又不一样），也不该去改用户的原始文件；
    把订正单独记一张表，重扫台账时自动套用，错在哪、谁改的、为什么，一目了然。
    """
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            key = (row.get("file") or "").strip()
            if not key or key.startswith("#"):
                continue
            out[key] = {
                k: v.strip() for k, v in row.items() if k and k != "file" and (v or "").strip()
            }
    return out


def collect(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            if p.suffix.lower() in DOC_SUFFIXES:
                files.append(p)
    return files


def mark_duplicates(records: list[Record]) -> None:
    by_hash: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        by_hash[r.sha256].append(r)
    group = 0
    for recs in by_hash.values():
        if len(recs) > 1:
            group += 1
            for r in recs:
                r.dup_group = f"D{group:02d}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成规范台账 catalog.csv")
    ap.add_argument("--roots", nargs="+", required=True, help="待扫描的目录，可多个")
    ap.add_argument("--out", default=str(paths.CATALOG))
    ap.add_argument(
        "--overrides", default=str(paths.CATALOG_DIR / "overrides.tsv"), help="人工订正表"
    )
    args = ap.parse_args(argv)

    roots = [Path(r).expanduser().resolve() for r in args.roots]
    for r in roots:
        if not r.is_dir():
            print(f"目录不存在: {r}", file=sys.stderr)
            return 1

    files = collect(roots)
    print(f"发现 {len(files)} 个文档，开始解析…")

    overrides = load_overrides(Path(args.overrides))
    used: set[str] = set()

    records: list[Record] = []
    for i, p in enumerate(files, 1):
        rec = build_record(p, paths.ROOT)
        if fix := overrides.get(p.name):
            used.add(p.name)
            for k, v in fix.items():
                if hasattr(rec, k):
                    setattr(rec, k, v)
        records.append(rec)
        if i % 10 == 0 or i == len(files):
            print(f"  {i}/{len(files)}")

    # 订正表里指着不存在的文件，多半是文件改名或挪走了，必须喊出来，
    # 否则那条订正会悄无声息地失效，错误的编号又回到台账里。
    for name in sorted(set(overrides) - used):
        print(f"  ! 订正表里的 {name} 没在扫描结果里找到", file=sys.stderr)

    mark_duplicates(records)
    records.sort(key=lambda r: (r.category or "zz", r.std_code or "zz", r.file_path))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(asdict(records[0]).keys()))
        w.writeheader()
        for r in records:
            w.writerow(asdict(r))

    # 汇总，方便立刻看出要人工处理哪些
    scanned = sum(1 for r in records if r.is_scanned == "是")
    issues = [r for r in records if r.issue]
    no_code = [r for r in records if not r.std_code]
    dups = sorted({r.dup_group for r in records if r.dup_group})

    print(f"\n台账已写入 {out}")
    print(f"  文档总数      {len(records)}")
    print(f"  人工订正      {len(used)}")
    print(f"  扫描件(需OCR) {scanned}")
    print(f"  内容重复组    {len(dups)}  {' '.join(dups)}")
    print(f"  编号未识别    {len(no_code)}")
    print(f"  有问题的文件  {len(issues)}")
    for r in issues:
        print(f"      ! {Path(r.file_path).name}  —  {r.issue}")
    print("\n下一步：人工填写 status / superseded_by 两列（对照住建部标准公告）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
