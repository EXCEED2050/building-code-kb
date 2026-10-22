"""每本规范的 chunk.page_idx 指向哪个 PDF 文件——页面上「看原文」要拿准这一本。

切条时页码来自三条路，各自对应的 PDF 不同：

  Word + MinerU 页码    data/processed/mineru/<文件名>.pdf-<hash>/ 里那本，即台账里同名的 PDF
  本地 OCR              data/processed/ocr/<书名>/，PDF 是 data/processed/ocr/_merged/<书名>.pdf（分卷合并过的），
                        没合并过的就是台账里同名的那个文件
  企标 Word + 正文定位   台账里该编号的第一个 PDF（batch.main() 里 pdfs 那个 dict）

判定顺序照抄 batch.main()：有 Word 走前两种，没 Word 走 OCR。索引函数也直接
从 batch 里 import，两边不可能算出不一样的结果。

指错一本，「第 38 页」就是另一本的第 38 页——比没有页码更糟。所以 check()
把每本的最大 page_idx 跟 PDF 页数对一遍，超了就报：

    python -m building_kb.source_pdf
"""

from __future__ import annotations

from building_kb import paths

import csv
import glob
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

from .ingest.batch import index_mineru, index_word_files, match, norm

ROOT = paths.ROOT
CATALOG = paths.CATALOG
WORD_DIR = paths.WORD_DIR
MINERU_DIR = paths.MINERU_DIR
OCR_DIR = paths.OCR_DIR
DOCUMENTS = paths.CATALOG_DIR / "source_documents.json"

__all__ = ["resolve", "pdf_document", "check", "norm"]


@lru_cache(maxsize=32)
def pdf_document(std_code: str) -> tuple[Path, int]:
    """完整阅读本与原转录页序偏移；逐页预览仍使用原定位副本。"""
    source = resolve(std_code)
    if source is None:
        raise FileNotFoundError("这本规范没有可用的PDF原件")
    manifest = DOCUMENTS
    records = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    record = records.get(std_code)
    if not record:
        return source, 0
    complete = ROOT / record["full_pdf_path"]
    if not complete.is_file():
        raise FileNotFoundError("完整PDF尚未恢复，请运行 building_kb.commands.build_source_documents；逐页预览仍可用")
    for path, digest in [(source, record["source_pdf_sha256"]), (complete, record["sha256"])]:
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("PDF指纹变化，完整阅读本的页码映射须重新核对")
    return complete, record["page_offset"]


@lru_cache(maxsize=1)
def _tables():
    by_stem: dict[str, Path] = {}     # 文件名（不含扩展名）-> 路径
    first_pdf: dict[str, Path] = {}   # 归一编号 -> 台账里第一个 PDF
    with CATALOG.open(encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            fp = r["file_path"]
            if not fp.lower().endswith(".pdf"):
                continue
            p = ROOT / fp
            by_stem.setdefault(Path(fp).stem, p)
            if r["std_code"].strip() and p.exists():
                first_pdf.setdefault(norm(r["std_code"]), p)
    return by_stem, first_pdf, index_word_files(WORD_DIR), index_mineru(MINERU_DIR), index_mineru(OCR_DIR)


def _book_dir_name(content_list: Path, base: Path) -> str:
    """content_list.json 所在的那个书目录名，去掉在线 MinerU 追加的 .pdf-<hash>。"""
    for d in content_list.parents:
        if d.parent == base:
            return d.name.split(".pdf-")[0].split(".PDF-")[0]
    return content_list.parent.name


def resolve(std_code: str) -> Path | None:
    by_stem, first_pdf, words, minerus, ocrs = _tables()
    w = match(std_code, words)
    m = match(std_code, minerus)
    o = match(std_code, ocrs)
    p: Path | None = None
    if w is not None:
        p = by_stem.get(_book_dir_name(m, MINERU_DIR)) if m is not None else first_pdf.get(norm(std_code))
    elif o is not None:
        name = _book_dir_name(o, OCR_DIR)
        p = OCR_DIR / "_merged" / f"{name}.pdf"
        if not p.exists():
            p = by_stem.get(name)
    return p if p is not None and p.exists() else None


def check(chunks_dir: Path = paths.CHUNKS) -> list[tuple[str, str, int, int]]:
    """逐本核对：返回 (编号, 文件, 最大页码, PDF 页数)；PDF 页数为 0 表示没解析到文件。"""
    import fitz

    out = []
    for f in sorted(glob.glob(str(chunks_dir / "*.jsonl"))):
        code, mx = "", -1
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                code = r["std_code"]
                if r["page_idx"] is not None:
                    mx = max(mx, r["page_idx"])
        p = resolve(code)
        n = 0
        if p is not None:
            with fitz.open(p) as d:
                n = d.page_count
        out.append((code, str(p.relative_to(ROOT)) if p else "—", mx + 1, n))
    return out


def main() -> int:
    bad = 0
    for code, path, mx, n in check():
        flag = "" if (n and mx <= n) else "  <-- 对不上"
        bad += bool(flag)
        print(f"{code:<20} 最大页 {mx:>4} / 全书 {n:>4}  {path}{flag}")
    print(f"\n{'全部对得上' if not bad else f'{bad} 本对不上'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
