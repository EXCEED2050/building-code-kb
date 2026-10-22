"""直接在 PDF 里按正文定位页码，不依赖 MinerU。

MinerU 只跑过国标，13 本企标一页都没有；而引用里没有页码，人就没法回原文核对，
「可核对」这条就断了。企标的 PDF 都在，条文文字也都有，那就直接拿文字去 PDF 里找。

比 MinerU 的条文号匹配更稳的一点：这里匹配的是**正文文字**而不是编号。
企标的条文号本来就是重建出来的，PDF 里未必印着同样的号；文字则是一一对应的。

单调约束是关键：条文在书里必然顺序出现，所以从上一条命中的页往后找。
没有这个约束，「本标准适用于……」这种套话会命中前言里的同款句子。
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz  # PyMuPDF

WS_RE = re.compile(r"[\s　]+")
# 页眉页脚、图表编号里也会出现条文开头的字，取够长的探针避开
PROBE_MIN, PROBE_MAX = 10, 28


def _flat(s: str) -> str:
    return WS_RE.sub("", s)


def page_texts(pdf: Path) -> list[str]:
    doc = fitz.open(pdf)
    try:
        return [_flat(doc[i].get_text()) for i in range(doc.page_count)]
    finally:
        doc.close()


def locate(pages: list[str], probes: list[str]) -> list[int | None]:
    """按顺序在页文本里找每个探针，返回页下标（0 起）。

    找不到就顺延用上一条的页码——同一页上有好几条是常态，
    漏掉一条不该让它整个没页码；但要如实记为「继承」而非精确命中。
    """
    out: list[int | None] = []
    cur = 0
    for p in probes:
        probe = _flat(p)[:PROBE_MAX]
        hit = None
        if len(probe) >= PROBE_MIN:
            for i in range(cur, len(pages)):
                if probe in pages[i]:
                    hit = i
                    break
            if hit is None:
                # 往回找一页：跨页断行会把条文开头切碎，偶有错位
                for i in range(max(0, cur - 1), min(cur + 1, len(pages))):
                    if probe[:PROBE_MIN] in pages[i]:
                        hit = i
                        break
        if hit is not None:
            cur = hit
        out.append(hit if hit is not None else (cur if out else None))
    return out


def apply_to(chunks: list, pdf: Path) -> tuple[int, int]:
    """给没有页码的 chunk 补页码，返回 (精确命中数, 仍无页码数)。"""
    if not pdf.exists():
        return 0, sum(1 for c in chunks if c.page_idx is None)

    pages = page_texts(pdf)
    if not any(pages):
        return 0, sum(1 for c in chunks if c.page_idx is None)  # 扫描件，没有文本层

    targets = [c for c in chunks if c.page_idx is None]
    found = locate(pages, [c.text for c in targets])
    exact = 0
    for c, p in zip(targets, found):
        if p is not None:
            c.page_idx = p
            exact += 1
    return exact, sum(1 for c in chunks if c.page_idx is None)
