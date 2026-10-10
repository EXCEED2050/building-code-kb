"""中石化企业标准（DEP-*）的结构解析。

国标走工程建设标准写法，条文号「5.5.17」明明白白印在正文里，正则一扫就有。
企标走 GB/T 1.1 标准编写规则，章节号是 Word 自动编号——正文里**一个数字都没有**，
只有「范围」「引用文件」这样的光杆标题。python-docx 的 .text 看不到自动编号，
于是整本书在切条器眼里没有一条条文，全部沦为游离文本。

编号藏在段落的 w:numPr 里。但实测 13 本的 ilvl 基准各不相同（AR2001 的章在
ilvl=1，AR2108 在 ilvl=0），且 Word 每次重启编号就新建一个 numId，各自从 0 起，
于是次级列表的 ilvl=0 会被误当成章——按 numPr 定层级行不通。

改由**目次**定层级：目次里「4 建筑防火」是白纸黑字的标准答案，
标题在正文中出现即开一章。numPr 只用来认「这一段是不是一条条文」
以及区分条与款。目次对不上的书降级为不给条号，引用里只写章节名——
引用里写错条号比不写条号危害大得多。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

# 目次行：「4 建筑防火……… 3」「1.\t范围\t3」「2引用文件…… 3」
# 标题与编号间可能无空格，故分隔符允许为空，但那时标题须以汉字开头。
TOC_RE = re.compile(
    r"^(\d+(?:\.\d+)*)\s*[.、]?\s*[\t　 ]*([一-龥][^\t]*?)[\s.…·]*(\d{1,3})\s*$"
)
TOC_END_RE = re.compile(r"^前\s*言\s*$")
SKIP_TITLE_RE = re.compile(r"^(目\s*次|前\s*言|Contents)\s*$")
# 正文起点：前言之后的第一个目次标题。前言里也会复述章节名
#（「本标准共分6章，主要内容有：建筑防火、建筑防爆……」），要跳过去。
PREFACE_RE = re.compile(r"^前\s*言\s*$")


@dataclass
class Node:
    """结构树上的一个节点。"""

    kind: str  # chapter | section | clause
    number: str  # "4" / "4.1" / "4.1.2"；不可信时为空
    text: str
    idx: int  # 在 blocks 里的下标
    chapter: str = ""  # 所属章的「编号 标题」
    section: str = ""  # 所属节的「编号 标题」
    body: list[str] = field(default_factory=list)  # 归入本节点的后续段落


def _numpr(p: Paragraph | None) -> tuple[str, int] | None:
    """取段落的 (numId, ilvl)。段落上没有就去样式定义里找。

    自动编号是 Word 的排版元数据，OCR 产物里根本不存在——那边 block 的
    obj 是 None。之前这里直接 `p._p` 会抛 AttributeError，
    把「这本书的条号恢复不了」这件事伪装成了一个崩溃。返回 None 才是实话：
    没有编号信息，不是出了故障。
    """
    if p is None:
        return None
    for el in (p._p.find(qn("w:pPr")), p.style._element.find(qn("w:pPr"))):
        if el is None:
            continue
        n = el.find(qn("w:numPr"))
        if n is None:
            continue
        ni = n.find(qn("w:numId"))
        il = n.find(qn("w:ilvl"))
        if ni is None:
            continue
        return ni.get(qn("w:val")), int(il.get(qn("w:val")) if il is not None else 0)
    return None


def _norm(t: str) -> str:
    """标题归一，用于和目次比对。

    连标点一起去掉：目次写「变配电类建筑物功能概述」，正文写
    「变、配电类建筑物功能概述」，就差一个顿号。
    """
    return re.sub(r"[\s　、，,．.·（）()／/－—-]+", "", t).rstrip("。：:")


def parse_toc(texts: list[str]) -> tuple[dict[str, str], int]:
    """从目次区抽出 归一标题 -> 编号，并返回目次结束的位置。

    只扫到「前言」为止：正文里也有「表 4.1 食堂建筑的规模 12」这样的行，
    扫过头会把它们当章节。
    """
    out: dict[str, str] = {}
    end = 0
    for i, t in enumerate(texts):
        if TOC_END_RE.match(t) and out:
            end = i
            break
        if m := TOC_RE.match(t):
            title = m.group(2).strip().rstrip(".…· 　")
            if title and not SKIP_TITLE_RE.match(title):
                out.setdefault(_norm(title), m.group(1))
    return out, end


def body_start(texts: list[str], toc_end: int) -> int:
    """正文起点：前言段之后的第一段。"""
    for i in range(toc_end, len(texts)):
        if PREFACE_RE.match(texts[i]):
            # 前言正文一路到下一个目次标题为止，交给调用方按目次命中去定位
            return i + 1
    return toc_end


# 企标自带编号时的条文号：允许两级（「3.1」）。
# 国标路径刻意拒绝两级——那边的两级全是目次行和公式残片。企标不同，
# 两级就是正经条文号（AR2113 全书最深只到 4.1），所以单开一个式子。
INLINE_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){1,3})\s{0,3}(?=[^\d.]|$)")
# 章标题是单级的（「1 范围」），条文号至少两级。单级号只在与目次比对时剥离——
# 光看「1 」无法判断是章号还是句首的数量词（「1 个房间……」），目次命中才是证据。
HEAD_NO_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2})*)\s{0,3}(?=[^\d.]|$)")


def has_inline_numbers(texts: list[str], clause_re: re.Pattern) -> bool:
    """正文自带三级条文号就该走国标路径，不必来这里。"""
    return sum(1 for t in texts if clause_re.match(t)) >= 10


def build_structure(
    blocks: list[tuple[str, object, str]], clause_re: re.Pattern
) -> tuple[list[Node], dict]:
    """按目次切章节，条文号优先取正文自带的，没有才按 numPr 顺序重建。"""
    texts = [t for _, _, t in blocks]
    toc, toc_end = parse_toc(texts)
    start = body_start(texts, toc_end)

    # 正文自带编号就用自带的——那是原文印着的，比任何重建都可靠
    inline = sum(1 for t in texts[start:] if INLINE_RE.match(t)) >= 10

    nodes: list[Node] = []
    chapter = section = ""
    chap_no = sec_no = ""
    counter = 0
    seen: list[str] = []  # 按出现顺序记已开的章节
    cur: Node | None = None

    for i in range(start, len(blocks)):
        kind, obj, text = blocks[i]
        if kind != "p" or not text:
            continue

        # 自带编号时先把编号剥下来再跟目次比对：正文标题写作「3 架空防静电活动
        # 地板组成」，目次里只有「架空防静电活动地板组成」
        stripped = text
        inline_no = ""
        if inline and (m := INLINE_RE.match(text)):
            inline_no, stripped = m.group(1), text[m.end():]

        key = _norm(stripped)
        num = toc.get(key)
        if num is None and inline and not inline_no and (m := HEAD_NO_RE.match(text)):
            # 单级章号「1 范围」：剥掉号再问一次目次
            if (k2 := _norm(text[m.end():])) in toc:
                inline_no, stripped, key, num = m.group(1), text[m.end():], k2, toc[k2]
        # 目次命中且没开过 —— 开新章或新节。限长是因为条文正文偶尔整句等于
        # 某个章节名（「本章规定了建筑防火」），但那种句子都更长。
        if num and key not in seen and len(stripped) <= 30:
            seen.append(key)
            num = inline_no or num  # 正文印的号优先
            if "." in num:
                sec_no, section = num, f"{num} {stripped.strip()}"
            else:
                chap_no, chapter = num, f"{num} {stripped.strip()}"
                sec_no, section = "", ""
            counter = 0
            cur = None
            continue

        numbered = bool(inline_no) if inline else _numpr(obj) is not None  # type: ignore[arg-type]
        if numbered and chap_no:
            counter += 1
            no = inline_no or f"{sec_no or chap_no}.{counter}"
            cur = Node("clause", no, stripped if inline_no else text, i, chapter, section)
            nodes.append(cur)
        elif cur is not None:
            cur.body.append(text)
        elif chap_no:
            # 章下、首条之前的引言段（如「引用文件」章的规范清单）自成一块
            cur = Node("clause", chap_no, text, i, chapter, section)
            nodes.append(cur)

    # 章号是从目次【取】的而不是数出来的，所以漏掉一章不会让其余章号错位，
    # 只会让那一章的内容被算到前一章名下——局部且有界。真正会让全书编号作废的
    # 是顺序错乱（说明匹配到的根本不是章标题）。故以「顺序单调」为可信判据。
    order = [list(toc).index(k) for k in seen]
    monotonic = all(a < b for a, b in zip(order, order[1:]))
    return nodes, {
        "toc": len(toc),
        "toc_hit": len(seen),
        "nodes": len(nodes),
        "inline": inline,
        "trusted": bool(toc) and monotonic and len(seen) >= max(1, len(toc) * 0.8),
        "missing": [k for k in toc if k not in seen],
    }
