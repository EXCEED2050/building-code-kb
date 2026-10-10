"""把一本规范切成「条」级 chunk。

数据源策略（经实测确定，见 PLAN.md「数据源决策」）：
  - 正文文本取自 Word（.docx）——条号无 OCR 错误、表格结构正确、
    条文说明里明文写有「本条为强制性条文」
  - 页码取自 MinerU 的 content_list.json，用条号做连接键

核心逻辑是三件事，缺一不可：

1. 区段识别。一本规范的 Word 里同一个条号最多出现三次：
   局部修订条文（现行）、正文（可能是被修订前的旧版）、条文说明（不具强制力）。
   不分区就切，检索会把三者混作一谈。

2. 修订优先。GB 50016-2014(2018版) 实测：条 10.2.7 的修订版比原版多出
   「老年人照料设施的非消防用电负荷应设置电气火灾监控系统」一句强制要求。
   命中原版就会漏掉它。故同条号存在修订版时，原版标 superseded 并排除出检索池。

3. 表格随条走。表 5.5.17 脱离条 5.5.17 没有意义，必须在同一个 chunk 里。

用法:
    python -m src.chunker --docx <path> [--mineru <content_list.json>] --out data/chunks/
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import docx
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from . import depdoc, mdblocks, twolevel

# 条文号：3.1.2 / 5.1.3A / 10.0.4
#
# 条文号恒为三级（章.节.条，无节时为 章.0.条），实测 10 本规范中
# 所有二级编号都是垃圾：目次行「5.4 平面布置 (67)」、公式残片「0.58倍;」。
# 故要求 {2,} 个小数点段。
#
# 负向先行断言排除三类误命中：
#   「条」——交叉引用的断行残片「6.2.5条的规定;设置回廊……」；
#   「.」「数字」——阻止回溯，否则「12.2.4条」会退一步匹配成「12.2」；
#   「、」「，」——公告里作废条文的清单「4.2.3、4.2.5、4.2.8……」。
#
# 附录条文号形如 A.0.1 / B.1.2，与正文同为三级，单列一个分支而不是并进上面的
# 字符类——否则 "A.0" 之类两级写法会被放行。
CLAUSE_RE = re.compile(
    r"^(?:(\d{1,2}(?:\.\d{1,3}){2,}[A-Z]?)|([A-Z](?:\.\d{1,2}){2}))(?![.\d条、，,])\s*"
)
# 附录标题：附录A 建筑高度和建筑层数的计算方法
APPENDIX_RE = re.compile(r"^附\s*录\s*([A-Z])\s{0,4}([^(（]{0,40})\s*$")
# 悬空引用：PDF 转 Word 把「……应按本规范第5.1.4、第」/「……应按表」断在这里，
# 下一段「5.1.5 条确定,……」看着像新条文，其实是上一条的续行。
# 实测 GB 50011 的 7 处条号重复全部出自这一模式，判据无一例外。
DANGLING_REF_RE = re.compile(r"(?:第|表|图|式|附\s*录|规范|标准)\s*$")
# 章标题：3 厂房和仓库。行标不带空格（「3术语和定义」），故 \s{0,4}。
# 结尾不得是句读——标题不带标点，而短款行常以「。」收尾。
#
# 标题不设长度上限，也不排除括号：GB 50016 第 4 章就叫「甲、乙、丙类液体、
# 气体储罐（区）与可燃材料堆场」，22 字还带括号。光靠字面分不开真假标题——
# 表注「5 当数个储罐区布置在同一库区内时,储罐区之间的防火间距不应小于本表」
# 长得一模一样，且恰好紧跟第 4 章，连章号递增都骗得过。
# 判据只能是结构：见 confirms_chapter()——真章标题后面跟的是本章的条文。
CHAPTER_RE = re.compile(r"^(\d{1,2})\s{0,4}([一-龥][^。；\n]{0,30}[^。；，、：;,:])\s*$")
# 节标题：3.1 火灾危险性分类
SECTION_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\s{0,4}([一-龥][^。；\n]{0,30}[^。；，、：;,:])\s*$")
# 目次行的页码会跟着标题一起被捉进来：「6 多层和高层钢筋混凝土房屋 335」
TOC_PAGE_RE = re.compile(r"\s+\d{1,3}$")
# 条文说明里的强条声明。各规范措辞不统一，实测：
#   GB 50016 「本条为强制性条文。」（句首）
#   GB 50011 「本条是强制性条文」
#   GB 51251 「本条是强制性条文，必须严格执行。」（句尾）/「本条第2、3款是强制性条文」
#   GB 50223 「本条是新增的，作为强制性条文」
# 必须锚定「本条」：否则会误命中引用他标准的句子，例如 GB/T 50378 里的
# 「《建筑抗震设计规范》GB 50011 第3.4.1条（强制性条文）」——那是别人的强条。
MANDATORY_RE = re.compile(r"本条[^。；\n]{0,12}?(?:为|是|作为|属于)\s*强制性条文")

# 发布公告里的强条清单：「其中，第3.2.1、3.2.2、3.8.5、3.8.7条为强制性条文，必须严格执行。」
# 这是住建部公告的原话，比从条文说明里一条条推断权威得多，也全得多
# （GB 50009 公告列 13 条，条文说明一条都没自述；GB 50067 公告 25 条 vs 条文说明 20 条）。
# 只在正文起点之前的前置区里找——条文说明里也会出现「第X条为强制性条文」，
# 但那往往是在引用**别的规范**（「《建规》GB 50016 第5.5.8条为强制性条文」），
# 照抄进来就会把别人的强条安到自己头上。
# 清单里常带款号：「12.1.5（1）」「3.1.1(1)(2)(3)」「4.0.6(1、2、3)」。字符集里不放括号，
# 碰到第一个括号整张清单就匹配不上、静悄悄返回空集——GB 50074 / 50183 / 50737
# 三本就是这样一条强条都没标上。款号不单独落地：整条标强条，多标不漏标。
ANNOUNCE_RE = re.compile(r"第([\d.\s、，,()（）]{3,600}?)条[^。]{0,6}为强制性条文")
CLAUSE_NUM_RE = re.compile(r"\d+(?:\.\d+){1,3}")


def announced_mandatory(blocks: list[tuple[str, object, str]], body_start: int) -> set[str]:
    """从发布公告里抄强条条号清单。找不到就返回空集，不影响原有判据。"""
    out: set[str] = set()
    for _, _, t in blocks[:body_start]:
        for m in ANNOUNCE_RE.finditer(t or ""):
            out.update(CLAUSE_NUM_RE.findall(m.group(1)))
    return out

# GB 55xxx 系列为工程建设通用规范，全文强制，且文件内不含条文说明
UNIVERSAL_CODE_RE = re.compile(r"^GB\s*55\d{3}")

FRONT_MATTER_RE = re.compile(r"^(中华人民共和国.*公告|住房城乡建设部.*公告|前\s*言|目\s*次|Contents)\s*$")
# 条文说明标题的写法不一：GB 50011 是「条文说明」，GB 55015 是「四、条文说明」
# 「条文说明」大标题。必须整行匹配，否则目次里的「条文说明……38」也会算数。
# 但 OCR 源要多兜一层：MinerU 常把页眉的规范号跟标题并进同一个块，
# 实测出来是 'GB 50067 - 2014条文说明'、'GB 50037-2013条文说明'。
# 漏判的后果不是少一个区，而是**正文和条文说明整个对调**——
# 条文说明把每条条号都复述一遍，密度比正文还高，
# 挑「条文最密集的一段」当正文时它必胜，于是真正文被降级成「修订区」，
# 再被修订优先逻辑拿条文说明的文字顶掉（GB 50067 实测顶掉 75 条）。
# 条文说明扉页整页往往只有三样东西：规范名、规范号、「条文说明」。
# MinerU 把它们并成一个块，mdblocks.tidy 再把换行压成空格，最终长这样：
#   '无障碍设计规范 GB 50763 - 2012 条文说明'    （GB 50763 实测）
# 所以规范名这一段也得放进来。漏判的代价不是少一个区，而是整本书的
# 条文说明被当成正文——GB 50763 实测 89 条解释性文字顶着条号混进了检索池，
# 「本条规定了制定本规范的目的」这种话会被当成条文原文答出去。
#
# 仍然要求整行收尾于「条文说明」且不带页码：目次里的「附：条文说明 49」、
# 正文里的「……编制了本规范的条文说明，对条文规定的目的……」都不算数。
NOTES_HEAD_RE = re.compile(
    r"^(?:[一-龥]{2,20}(?:规范|标准|规程|规定|导则|手册)\s*)?"
    r"(?:[A-Z]{1,4}(?:/[A-Z])?\s*\d+(?:\.\d+)*\s*[-–—]\s*\d{4}\s*)?"
    r"(?:[一二三四五六七八九十]+\s*[、．.]\s*)?(?:条文|起\s*草)\s*[说説]\s*明\s*$"
)
# 「起草说明」：通用规范（GB 55xxx）正文之后不直接是条文说明，而是
# 「起草说明 → 一、基本情况 → 二、起草人员 → 三、术语 → 四、条文说明」。
# 只认「四、条文说明」时，前三节全落进正文：术语表按「1 砌体结构」
# 「2 无筋砌体结构」切成几十个假条，且因通用规范全文强制，被标成了强条。
# 这几节都是说明性质，和条文说明一样不进检索池。


def is_notes_head(text: str) -> bool:
    """这一块是不是「条文说明」大标题。按行判，块内换行没被压掉时也认得。"""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines or not NOTES_HEAD_RE.match(lines[-1]):
        return False
    return all(len(ln) <= 40 and not re.search(r"\d+\.\d", ln) for ln in lines[:-1])


# 说明区前的重复封面。国标、行标的条文说明（通用规范的起草说明）都另起
# 一张封面「中华人民共和国××标准 / 规范名 / 条文说明」，正文里不会有这一行。
# SH/T 3206 的 OCR 丢了「条文说明」四个字，只剩这张封面——不认它，整份
# 条文说明（「11.1.1 条文所列出的设计文件仅为…」）就全混进了正文。
COVER_RE = re.compile(r"^中华人民共和国[一-龥]{0,12}标准$")


def draft_start(heads: list[tuple[int, str]], body_start: int, notes_start: int) -> int:
    """正文之后第一个「起草说明」或重复封面，把说明区的起点往前提。

    heads 是 (位置, 文本) 形式的说明区标题和封面行。条文说明仍取最后一个
    （目次里也会出现「条文说明」），再在正文和它之间找更早的起点。
    """
    for pos, text in heads:
        last = text.split("\n")[-1].strip()
        if body_start < pos < notes_start and ("草" in last or COVER_RE.match(last)):
            return pos
    return notes_start


# 全书正文的起点锚：第 1 章标题，或第 1 章的首条。
# 国标第 1 章叫「总则」，行标（SH/T 等）叫「范围」，且常不带空格（「1范围」）。
BODY_START_RE = re.compile(r"^1\s{0,4}(?:总\s{0,2}则|范\s{0,2}围)\s*$|^1\.[01]\.1(?!\d)")

SectionType = str  # "修订" | "正文" | "条文说明"


@dataclass
class Chunk:
    chunk_id: str
    std_code: str
    std_name: str
    version: str
    clause_no: str
    chapter_path: str
    section_type: SectionType
    text: str
    tables: list[str] = field(default_factory=list)
    page_idx: int | None = None
    is_mandatory: bool = False
    mandatory_basis: str = ""
    superseded: bool = False
    status: str = ""
    # 「这一条本身抄得没错，但引用之前必须知道的事」。
    # 典型场景是局部修订公告：住建部单发一份《局部修订条文》，
    # 正文照旧印着修订前的文字。这种条文既不能删（删了查不到）、
    # 也不能当现行文本直接用，只能带着话一起给出去。
    caveat: str = ""

    def search_text(self) -> str:
        """送进 embedding / BM25 的文本。

        前缀带上规范号与章节路径，让「GB 50016 疏散距离」这类查询
        在语义和关键词两条通路上都能命中。
        """
        head = f"{self.std_code} {self.std_name} {self.chapter_path} 第{self.clause_no}条"
        body = "\n".join([self.text, *self.tables])
        return f"{head}\n{body}"


# --------------------------------------------------------------------------
# Word 读取：python-docx 默认把段落和表格分成两个列表，会丢失它们的相对位置。
# 表格必须挂到它所跟随的条文上，所以这里按 XML body 顺序遍历。
# --------------------------------------------------------------------------

def iter_block_items(doc: DocxDocument):
    """按文档实际顺序产出 ('p', Paragraph) / ('t', Table)。"""
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            yield "p", Paragraph(child, doc)
        elif tag == "tbl":
            yield "t", Table(child, doc)


# 转换工具把带单位的数值写成了 OMML 公式（<m:t>32</m:t><m:t>m</m:t>），
# python-docx 的 .text 只读 <w:t>，会把这些数值整个吞掉——实测 GB 50016
# 有 46% 的条文因此丢失关键数值（「建筑高度不宜大于　，不应大于　」）。
# 规范里数字就是内容，必须连 <m:t> 一起取。
_TEXT_TAGS = {"t", "delText"}

# XML 解析器本来就会把实体还原成字符，所以正文里**还留着** &lt; &gt; 字面量，
# 只能是转换工具转义了两次。而这两个符号在规范里是限值的比较号
# （「塔高H&lt;80m」「FP&gt;50m²」），原样留着既影响阅读也影响送进模型的语义。
# 全库实测只出现这两个，不做通用 html.unescape——那会把合法的 & 一起吃掉。
_ENTITIES = (("&lt;", "<"), ("&gt;", ">"))


def rich_text(el) -> str:
    """按文档顺序提取元素下的全部文字，含 OMML 公式内的数值。"""
    parts: list[str] = []
    for node in el.iter():
        tag = node.tag.split("}")[-1]
        if tag in _TEXT_TAGS and node.text:
            parts.append(node.text)
        elif tag in ("tab",):
            parts.append(" ")
        elif tag in ("br", "cr"):
            parts.append("\n")
    out = "".join(parts)
    for src, dst in _ENTITIES:
        out = out.replace(src, dst)
    return out


def para_text(p: Paragraph) -> str:
    return " ".join(rich_text(p._p).split())


def cell_text(cell) -> str:
    return " ".join(rich_text(cell._tc).split())


def table_to_markdown(t: Table | str) -> str:
    """表格转 Markdown。

    OCR 那条路径（mdblocks）送进来的已经是转好的 Markdown，原样返回。

    不能用 row.cells：它会把一个横向合并的单元格按占用的列数重复产出，
    「名 称」会变成「名 称 | 名 称 | 名 称」。也不能靠「相邻文本相同就去重」
    来消这种重复——那会连合法的重复值一起删掉（表 5.5.17 里「9 | — | —」
    会被压成「9 | —」，直接改变了规范数值）。

    正确做法是读 XML 里真实的 <w:tc>，用 gridSpan 补占位，
    文本只出现一次，列位不偏。

    纵向合并（vMerge=continue）的格子在 XML 里是空的，行标签会丢：
    「| | 高层 | 病房部分 | 24 |」看不出说的是医疗建筑。
    故把上一行同列的值继承下来。
    """
    if isinstance(t, str):
        return t

    lines: list[str] = []
    carry: dict[int, str] = {}

    for row in t.rows:
        cells: list[str] = []
        col = 0
        for tc in row._tr.tc_lst:
            txt = cell_text(_TcWrapper(tc))
            span = tc.grid_span or 1
            if not txt and _is_vmerge_continue(tc):
                txt = carry.get(col, "")
            else:
                carry[col] = txt
            cells.append(txt)
            cells.extend([""] * (span - 1))
            col += span
        if any(cells):
            lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _is_vmerge_continue(tc) -> bool:
    """该单元格是否为纵向合并的延续格（非起始格）。"""
    for node in tc.iter():
        if node.tag.split("}")[-1] == "vMerge":
            val = node.get(qn("w:val"))
            return val in (None, "continue")
    return False


class _TcWrapper:
    """让 cell_text 能直接吃裸的 <w:tc> 元素。"""

    __slots__ = ("_tc",)

    def __init__(self, tc) -> None:
        self._tc = tc


# --------------------------------------------------------------------------
# 区段划分
# --------------------------------------------------------------------------

@dataclass
class Region:
    kind: SectionType
    start: int
    end: int


def split_regions(blocks: list[tuple[str, object, str]]) -> list[Region]:
    """把文档块序列切成 修订 / 正文 / 条文说明 三个区（前置内容丢弃）。

    blocks: [(kind, obj, text), ...]，text 为段落文本，表格为 ""。

    各家规范的排版差异很大，不能按「文档以什么开头」来判：
      GB 50016 以「局部修订条文」起头，只有一个「条文说明」标题；
      GB 50011 以「公告」起头，且有【两个】「条文说明」——
        第一个属于局部修订部分，第二个才是全书的。
    取第一个会把全书正文整个吞进条文说明区（实测只剩 10 条进检索池）。

    notes_start 取【最后一个】「条文说明」标题。

    body_start 不能简单取「最后一个第 1 章锚点」：GB 55015 的第 1 章锚点是
    [73, 97, 98, 825, 826]，而它的「条文说明」写作「四、条文说明」（para 823），
    取最后一个会落到 825/826——那已经在条文说明里了。

    改用与排版无关的判据：正文区一定是条文最密集的一段。把相邻两个第 1 章锚点
    之间视作一个候选区间，选条文行数最多的那个的起点。
    """
    n = len(blocks)
    texts = [t for _, _, t in blocks]

    notes_idx = [i for i, t in enumerate(texts) if is_notes_head(t)]
    notes_start = notes_idx[-1] if notes_idx else n

    body_hits = [i for i in range(notes_start) if BODY_START_RE.match(texts[i])]
    body_start = 0
    if body_hits:
        bounds = [*body_hits, notes_start]
        density = [
            (sum(1 for t in texts[a:b] if CLAUSE_RE.match(t)), a)
            for a, b in zip(bounds, bounds[1:])
        ]
        body_start = max(density)[1]
    heads = [(i, t) for i, t in enumerate(texts) if i in set(notes_idx) or COVER_RE.match(t.strip())]
    notes_start = draft_start(heads, body_start, notes_start)

    regions: list[Region] = []

    # 前置区（公告/前言/目次）里没有正经条文，切条器会自然跳过；
    # 修订区需要单独识别，因为它的条文要优先于正文。
    # 修订区止于其自身的「条文说明」标题（若有），否则止于正文起点。
    if body_start > 0:
        pre_notes = [i for i in notes_idx if i < body_start]
        rev_end = pre_notes[0] if pre_notes else body_start
        if rev_end > 0:
            regions.append(Region("修订", 0, rev_end))

    regions.append(Region("正文", body_start, notes_start))
    if notes_start < n:
        regions.append(Region("条文说明", notes_start, n))
    return regions


# --------------------------------------------------------------------------
# 切条
# --------------------------------------------------------------------------

def confirms(
    blocks: list[tuple[str, object, str]], start: int, end: int, num: list[int], window: int = 60
) -> bool:
    """标题编号 num 是否与后文的条文号自洽（不得倒退）。

    真章标题「4 甲、乙、丙类液体、气体储罐（区）与可燃材料堆场」后面跟 4.1.1；
    混在第 4 章里的表注「5 当数个储罐区布置在同一库区内时，储罐区之间的防火间距
    不应小于本表」后面跟的仍是 4.x.x。两者字面上无从分辨——长度、括号、标点全
    一样，连「章号必递增」都骗得过（5 正好是 4+1）——只有这个结构判据管用。

    判「不倒退」而不是「必须等于」：SH/T 3006 的第 1、2 章（范围、规范性引用
    文件）通篇没有编号条文，往后第一个条文号是 3.1.1。要求相等会把这两章连同
    整章正文一起否掉（实测游离率从 0% 涨到 12%）。
    """
    for i in range(start, min(end, start + window)):
        kind, _, text = blocks[i]
        if kind != "p" or not text:
            continue
        if m := CLAUSE_RE.match(text):
            if not m.group(1):
                return True  # 附录条号（A.0.1），与正文章号无可比性
            got = [int(x) for x in m.group(1).split(".")[: len(num)] if x.isdigit()]
            return len(got) == len(num) and got >= num
        if m2 := SECTION_RE.match(text):
            got = [int(m2.group(1)), int(m2.group(2))][: len(num)]
            return got >= num
    # 窗口内找不到任何条文号 —— 无反证即通过。SH/T 3186 的第 1、2 章
    # （范围、规范性引用文件）下面全是散文和标准清单，往后 75 块才有第一个节号；
    # 判 False 会把这两章连同第 3 章一起否掉（章号递增校验随即断链），
    # 实测整本书 17% 的正文因此游离。表注那类假标题则必被后文的 4.x.x 反证，
    # 不受这条影响。
    return True


def chunk_region(
    blocks: list[tuple[str, object, str]],
    region: Region,
    meta: dict,
    orphan: list[str] | None = None,
) -> list[Chunk]:
    """在一个区段内按条文切块，表格挂到所属条文。

    orphan: 传入一个 list 则把「不属于任何条文的段落」收集进去，供质量核查。
    """
    out: list[Chunk] = []
    chapter = ""
    section = ""
    chap_no = 0  # 当前章号，由条文号 X.Y.Z 的 X 持续校准
    cur: Chunk | None = None
    shell = False  # cur 是附录占位块（只有标题、还没收到内容）
    orphan = orphan if orphan is not None else []

    def new_chunk(clause: str, text: str) -> Chunk:
        return Chunk(
            chunk_id=f"{meta['std_code']}#{region.kind}#{clause}".replace(" ", "_"),
            std_code=meta["std_code"],
            std_name=meta["std_name"],
            version=meta["version"],
            clause_no=clause,
            chapter_path=" / ".join(x for x in (chapter, section) if x),
            section_type=region.kind,
            text=text,
            status=meta.get("status", ""),
        )

    def flush() -> None:
        nonlocal cur, shell
        # 附录占位块没收到任何表格或正文就丢弃：GB 50016 的附录紧跟 A.0.1，
        # 占位块留着就是一条只有标题的空块。
        if cur and cur.text.strip() and not (shell and not cur.tables):
            out.append(cur)
        cur = None
        shell = False

    for i in range(region.start, region.end):
        kind, obj, text = blocks[i]
        if kind == "t":
            if cur:
                md = table_to_markdown(obj)  # type: ignore[arg-type]
                if md:
                    cur.tables.append(md)
            continue

        if not text:
            continue

        # 悬空引用续行判断要让位给「已被后文条文号证实的章节标题」。
        # SH/T 3006 的第 2 章是一串引用标准名，末行就以「……设计规范」收尾，
        # 于是下一段「3术语和定义」被当成续行吞掉，第 3、4 章连同整章条文
        # 全挂到了第 2 章名下。章节标题有 confirms() 背书，比悬空引用更可信。
        heading = not chapter.startswith("附录") and (
            ((m := CHAPTER_RE.match(text)) and int(m.group(1)) > chap_no
             and confirms(blocks, i + 1, region.end, [int(m.group(1))]))
            or ((m := SECTION_RE.match(text)) and int(m.group(1)) == chap_no
                and confirms(blocks, i + 1, region.end,
                             [int(m.group(1)), int(m.group(2))]))
        )

        # 上一段以悬空引用收尾，本段必是续行，不能当作新条文/新附录开头
        if cur and not heading and DANGLING_REF_RE.search(cur.text):
            cur.text += "\n" + text
            shell = False
            continue

        # 章 / 节标题：更新路径，不产生 chunk。
        # 附录之后不再有正文章节，必须停止匹配——否则附录里的款行
        # 「4 抗震设防烈度为6度，设计基本地震加速度值为 0.05g」会被当成第 4 章标题。
        # 款行与章标题长得一样（「1 同一结构单元的基础不宜设置在性质截然不同的地基上。」
        # 只有 25 字），靠编号序列区分：章号必递增，款号每条重新从 1 数起。
        # 实测不加这条判据时 GB 50011 正文有 23% 的文字被甩出条文之外。
        if not chapter.startswith("附录"):
            # 章/节标题同样开占位块：SH/T 3006 的「3术语和定义 / 3.1控制室」这类
            # 段落下面是不编条号的定义正文，没有占位块就会整章游离。
            # 占位块若立刻被条文顶掉，会在 flush 里丢弃。
            if (m := CHAPTER_RE.match(text)) and int(m.group(1)) > chap_no \
                    and confirms(blocks, i + 1, region.end, [int(m.group(1))]):
                flush()
                chap_no = int(m.group(1))
                chapter = f"{chap_no} {TOC_PAGE_RE.sub('', m.group(2).strip())}"
                section = ""
                cur = new_chunk(str(chap_no), text)
                shell = True
                continue
            if (m := SECTION_RE.match(text)) and int(m.group(1)) == chap_no and confirms(
                blocks, i + 1, region.end, [int(m.group(1)), int(m.group(2))]
            ):
                flush()
                section = f"{m.group(1)}.{m.group(2)} {TOC_PAGE_RE.sub('', m.group(3).strip())}"
                cur = new_chunk(f"{m.group(1)}.{m.group(2)}", text)
                shell = True
                continue
        # 附录与章平级：附录A 的条文号是 A.0.1，与正文章节号不冲突。
        # 有的附录根本不编条号，整篇就是一张表（GB 51251 附录A「表A 不同火灾规模下的
        # 机械排烟量」），所以这里先开一个以「附录A」为条号的占位块接住表格；
        # 若紧接着出现 A.0.1，占位块会在 flush 时被丢弃。
        if m := APPENDIX_RE.match(text):
            flush()
            chapter = f"附录{m.group(1)} {m.group(2).strip()}".rstrip()
            section = ""
            cur = new_chunk(f"附录{m.group(1)}", text)
            shell = True
            continue

        if m := CLAUSE_RE.match(text):
            flush()
            if m.group(1):
                # 条文号里的章号是权威值：即使某个章标题漏识别，也能自动校准
                chap_no = int(m.group(1).split(".")[0])
            cur = new_chunk(m.group(1) or m.group(2), text)
            continue

        # 款、项、注：并入当前条文
        if cur:
            cur.text += "\n" + text
            shell = False
        else:
            # 不属于任何条文的正文——切条规则漏了什么，这个数必须盯着
            orphan.append(text)

    flush()
    return out


# --------------------------------------------------------------------------
# 页码回填 & 修订优先
# --------------------------------------------------------------------------

def load_page_map(path: Path) -> tuple[dict[str, list[int]], int, int]:
    """从 MinerU content_list.json 建 条号 -> [出现过的页码]，并定位区段页界。

    返回 (pages, rev_end_page, notes_start_page)。

    同一条号在 PDF 里最多出现三次（修订区、正文、条文说明），
    不能靠「第几次出现」来挑——目次里也会出现条号。
    改为先确定每个区段的页码范围，再按范围选。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    pages: dict[str, list[int]] = {}
    notes_pages: list[int] = []
    notes_heads: list[tuple[int, str]] = []
    body_pages: list[int] = []
    clause_pages: list[int] = []

    for b in data:
        if b.get("type") != "text":
            continue
        raw = b.get("text", "").strip().lstrip("# ").strip()
        page = b["page_idx"]

        # 必须和 mdblocks.tidy 用同一套拆分：那边把并排的条文拆成了独立块，
        # 这边不拆的话，拆出来的条就查不到页码——页码是这个库「可核对」的根基，
        # 一条查得到原文却指不出在哪一页的条文，等于要同事自己翻一遍书。
        for text in mdblocks.split_inline(raw):
            if is_notes_head(text):
                notes_pages.append(page)
                notes_heads.append((page, text))
            elif COVER_RE.match(text):
                notes_heads.append((page, text))
            if BODY_START_RE.match(text):
                body_pages.append(page)

            if m := CLAUSE_RE.match(text):
                pages.setdefault(m.group(1) or m.group(2), []).append(page)
                clause_pages.append(page)
            elif m := APPENDIX_RE.match(text):
                # 无编号附录的块以「附录A」为条号，页码也得按同一个键回填
                pages.setdefault(f"附录{m.group(1)}", []).append(page)
            elif m := SECTION_RE.match(text):
                # 章/节占位块的条号就是章号/节号，同样要能查到页
                pages.setdefault(f"{m.group(1)}.{m.group(2)}", []).append(page)
            elif m := CHAPTER_RE.match(text):
                pages.setdefault(m.group(1), []).append(page)

    notes_start_page = notes_pages[-1] if notes_pages else 10**9

    # 与 split_regions 同一判据：正文是条文最密集的一段
    cands = sorted({p for p in body_pages if p < notes_start_page})
    rev_end_page = 0
    if cands:
        bounds = [*cands, notes_start_page]
        rev_end_page = max(
            (sum(1 for p in clause_pages if a <= p < b), a)
            for a, b in zip(bounds, bounds[1:])
        )[1]
    notes_start_page = draft_start(notes_heads, rev_end_page, notes_start_page)

    return pages, rev_end_page, notes_start_page


def apply_pages(
    chunks: list[Chunk], pages: dict[str, list[int]], rev_end: int, notes_start: int
) -> None:
    def pick(hits: list[int], lo: int, hi: int) -> int | None:
        """取落在 [lo, hi) 内的最小页码；没有就退回最接近的一个。"""
        inside = [p for p in hits if lo <= p < hi]
        if inside:
            return min(inside)
        return min(hits, key=lambda p: min(abs(p - lo), abs(p - hi))) if hits else None

    for c in chunks:
        hits = pages.get(c.clause_no)
        if not hits:
            continue
        if c.section_type == "修订":
            c.page_idx = pick(hits, 0, rev_end or notes_start)
        elif c.section_type == "条文说明":
            c.page_idx = pick(hits, notes_start, 10**9)
        else:
            c.page_idx = pick(hits, rev_end, notes_start)


def apply_mandatory(
    chunks: list[Chunk], std_code: str, announced: set[str] | None = None
) -> tuple[int, str]:
    """标注强制性条文，并记录判定依据。

    规范正文里强条是用黑体字标示的，而 OCR 与 PDF→Word 转换都会丢掉字重，
    这个信息不可能从文本里恢复。所以这里按可靠性从高到低分四条路：

      1. GB 55xxx 通用规范 —— 全文强制，这是该系列的定义，最可靠
      2. GB/T、JGJ/T 等推荐性标准 —— 按定义不设强制性条文
      3. 发布公告列明「第X、Y、Z条为强制性条文」—— 住建部原话，权威且成建制
      4. 条文说明里本条自述「本条为/是强制性条文」—— 可靠但覆盖不全
         （实测 GB 50160、SH/T 3006 的条文说明根本不标；
          GB 50009 的条文说明一条都没自述，公告里却列了 13 条）

    3 和 4 取并集，依据字段记清楚是谁说的。并集是有意的：漏标一条强条，
    同事就照普通条文处置了；多标一条，最坏是按更严的要求做。两种错不对称。

    判不出来的一律留 False 并把依据写成「未标注」，界面上必须提示用户
    按页码核对原文黑体。不做关键词猜测——猜错比不猜危害大。
    """
    if UNIVERSAL_CODE_RE.match(std_code.strip()):
        n = 0
        for c in chunks:
            if c.section_type != "条文说明":
                c.is_mandatory = True
                c.mandatory_basis = "通用规范全文强制"
                n += 1
        return n, "通用规范全文强制"

    if "/T" in std_code:
        for c in chunks:
            c.mandatory_basis = "推荐性标准，无强制性条文"
        return 0, "推荐性标准，无强制性条文"

    declared = {
        c.clause_no
        for c in chunks
        if c.section_type == "条文说明" and MANDATORY_RE.search(c.text)
    }
    listed = announced or set()
    n = 0
    for c in chunks:
        if c.section_type == "条文说明":
            continue
        src = [s for s, ok in (("发布公告列明", c.clause_no in listed),
                               ("条文说明声明", c.clause_no in declared)) if ok]
        if src:
            c.is_mandatory = True
            c.mandatory_basis = " + ".join(src)
            n += 1
        else:
            c.mandatory_basis = "未标注，请按页码核对原文黑体"
    basis = "发布公告列明 + 条文说明声明" if listed else "条文说明声明"
    return n, basis


def repair_chapter_paths(chunks: list[Chunk]) -> int:
    """用条文号校验并修复章节路径。

    条文号 X.Y.Z 里的 X、Y 本身就是权威的章节归属，比一路继承下来的标题可靠。
    继承会出两种错：
      - 表注里的「5 当数个储罐区布置在同一库区内时……」被 CHAPTER_RE 当成第 5 章，
        之后整段条文都挂到这个假章下；
      - 修订公告新增的条（GB 50160 的 5.7.1A）在正文里没有同号孪生条可继承，
        只能沿用公告里上一处提到的节名「4.5 厂际管道规划」——差了一整章。

    做法：先从「路径与条号自洽」的 chunk 里统计出每个章号/节号的真实标题，
    再拿它去改写不自洽的。标题只从自洽样本里来，所以假标题不会自我强化。
    """
    chap_title: dict[str, Counter] = defaultdict(Counter)
    sect_title: dict[str, Counter] = defaultdict(Counter)
    for c in chunks:
        parts = c.clause_no.split(".")
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        segs = [s for s in c.chapter_path.split(" / ") if s]
        if segs and segs[0].split(" ")[0] == parts[0]:
            chap_title[parts[0]][segs[0]] += 1
            if len(segs) > 1 and segs[1].split(" ")[0] == f"{parts[0]}.{parts[1]}":
                sect_title[f"{parts[0]}.{parts[1]}"][segs[1]] += 1

    n = 0
    for c in chunks:
        parts = c.clause_no.split(".")
        if len(parts) < 2 or not parts[0].isdigit():
            continue  # 附录条（A.0.1）和企标条号不走这套校验
        segs = [s for s in c.chapter_path.split(" / ") if s]
        if segs and segs[0].split(" ")[0] == parts[0]:
            continue  # 自洽，不动
        if not (ch := chap_title.get(parts[0])):
            continue  # 全书都没有这一章的可信标题，宁可留空也不猜
        new = [ch.most_common(1)[0][0]]
        if se := sect_title.get(f"{parts[0]}.{parts[1]}"):
            new.append(se.most_common(1)[0][0])
        c.chapter_path = " / ".join(new)
        n += 1
    return n


def apply_revision_priority(chunks: list[Chunk]) -> int:
    """同条号存在修订版时，把正文版标记为已被取代，并把章节路径传给修订版。

    传章节路径是必须的：修订公告只按条号罗列改动（「5.5.17 公共建筑的安全疏散
    距离……」），没有章节标题，切出来的 chapter_path 是空的。而检索文本要靠章节
    路径补语境——条文正文常只写「不应小于表 5.5.17 的规定」，「安全疏散」四个字
    只出现在节标题上。被取代的那条正好带着正确的路径，原样继承即可。
    """
    body = {c.clause_no: c for c in chunks if c.section_type == "正文"}
    n = 0
    for c in chunks:
        if c.section_type != "修订":
            continue
        if old := body.get(c.clause_no):
            old.superseded = True
            n += 1
            if not c.chapter_path:
                c.chapter_path = old.chapter_path
    return n


# --------------------------------------------------------------------------

def build_dep(
    blocks: list[tuple[str, object, str]], meta: dict, quiet: bool = False
) -> tuple[list[Chunk], dict]:
    """企标（DEP-*）的切条：结构交给 depdoc，这里只负责组装 Chunk 和挂表格。

    企标没有条文说明、没有局部修订公告，也没有强制性条文（企业标准不设强条），
    所以不做区段划分，全书按「正文」一种类型处理。

    `quiet` 只管要不要打印诊断，**不改变返回形态**——永远返回 (chunks, diag)。
    原先 quiet 时返回元组、非 quiet 时返回裸 list，调用方得记住自己传了什么，
    记错就会把整本书削成第一块。那种省事不值得。
    """
    nodes, diag = depdoc.build_structure(blocks, CLAUSE_RE)
    by_idx = {n.idx: n for n in nodes}

    chunks: list[Chunk] = []
    cur: Chunk | None = None
    for i, (kind, obj, _) in enumerate(blocks):
        if n := by_idx.get(i):
            cur = Chunk(
                chunk_id=f"{meta['std_code']}#正文#{n.number}".replace(" ", "_"),
                std_code=meta["std_code"], std_name=meta["std_name"],
                version=meta["version"],
                # 编号不可信时留空：引用里写错条号，比不写条号危害大得多
                clause_no=n.number if diag["trusted"] else "",
                chapter_path=" / ".join(x for x in (n.chapter, n.section) if x),
                section_type="正文",
                text="\n".join([n.text, *n.body]),
                status=meta.get("status", ""),
            )
            chunks.append(cur)
        elif kind == "t" and cur is not None:
            if md := table_to_markdown(obj):  # type: ignore[arg-type]
                cur.tables.append(md)

    if not quiet:
        src = "正文自带" if diag["inline"] else "按自动编号重建"
        print(f"  企标结构: 目次 {diag['toc']} 章节，命中 {diag['toc_hit']}，条文号{src}"
              f"（{'可信' if diag['trusted'] else '存疑，已置空条号'}）")
        print(f"  -> {len(chunks)} 条")
    return chunks, diag


def build_twolevel(content_list: Path, meta: dict) -> list[Chunk]:
    """GB/T 1.1 两级条号体例的切条。结构判断全在 `twolevel`，这里只组装 Chunk。

    页码不走 `load_page_map` 的条号反查：本路径会把并排的多条从同一个块里切开，
    切出来的条在 OCR 原文里没有独立起始行，反查必然落空。
    `twolevel.load_paged` 把 page_idx 一路带了过来，直接用。

    不可信就抛。两级编号短、长得像的东西多，一旦目次对不上，
    切出来的条号错在哪、错了几条，事后没有任何办法查证——
    那种产物不该进库，见 `twolevel` 模块 docstring。

    注意 `trusted` 的含义：它回答的是「这份目次有没有在正文里被走通」，
    **不是**「这本书是不是两级体例」。三级体例的书拿到这里同样能走通
    （它们的目次列了节，于是条自然落到第三级）——实测 24 本都能通过。
    分流的责任在 `build_from_mineru`：只有正文找不到三级条号、
    企标路径也切不出东西的书才会走到这里。这里只负责「既然来了，
    就必须和目次对得上」。
    """
    blocks = twolevel.load_paged(content_list)
    regions = split_regions([(k, o, t) for k, o, t, _ in blocks])
    items, diag = twolevel.build(blocks, [(r.kind, r.start, r.end) for r in regions])

    print(f"  两级体例: 目次 {diag['toc']} 条目（{diag.get('toc_chapters', 0)} 章），"
          f"正文命中 {diag.get('chapters_hit', 0)} 章，切出 {diag.get('clauses', 0)} 条"
          f"（{'与目次相符' if diag['trusted'] else '与目次不符'}）")
    if not diag["trusted"]:
        raise ValueError(
            f"{meta['std_code']} 两级体例切条未通过目次交叉验证："
            f"目次 {diag['toc']} 条目 / {diag.get('toc_chapters', 0)} 章，"
            f"正文按序命中 {diag.get('chapters_hit', 0)} 章，"
            f"切出条文 {diag.get('clauses', 0)} 条。需人工确认体例。"
        )

    # 同一个编号可能出现两次（条文说明区把章标题又走了一遍），chunk_id 必须唯一，
    # 否则前端按 id 取详情会拿到不确定的那一条。
    seen: Counter[str] = Counter()
    chunks: list[Chunk] = []
    for kind, it in items:
        key = f"{meta['std_code']}#{kind}#{it.num}".replace(" ", "_")
        seen[key] += 1
        chunks.append(Chunk(
            chunk_id=key if seen[key] == 1 else f"{key}~{seen[key]}",
            std_code=meta["std_code"], std_name=meta["std_name"],
            version=meta["version"], clause_no=it.num,
            chapter_path=" / ".join(x for x in (it.chapter, it.section) if x),
            section_type=kind,
            text=it.text.strip(),
            tables=it.tables,
            page_idx=it.page,
            status=meta.get("status", ""),
        ))

    n_mand, basis = apply_mandatory(chunks, meta["std_code"])
    n_fix = repair_chapter_paths(chunks)
    lost = sum(len(t) for t in diag["orphan"])
    kept = sum(len(c.text) for c in chunks if c.section_type == "正文")
    print(f"  区段: {diag['regions']}（前置 {diag['front']} 段未入库）")
    print(f"  正文游离 {len(diag['orphan'])} 段 / {lost} 字"
          f"（{lost / max(kept + lost, 1):.1%}）")
    for k, n in (diag.get("other_orphan") or {}).items():
        if n:
            print(f"  {k}区游离 {n} 字")
    print(f"  强条 {n_mand} 条（依据：{basis}），修正章节路径 {n_fix} 条")
    return chunks


def build(docx_path: Path, mineru_path: Path | None, meta: dict) -> list[Chunk]:
    doc = docx.Document(str(docx_path))
    blocks: list[tuple[str, object, str]] = []
    for kind, obj in iter_block_items(doc):
        text = para_text(obj) if kind == "p" else ""  # type: ignore[arg-type]
        blocks.append((kind, obj, text))

    texts = [t for _, _, t in blocks]
    if not depdoc.has_inline_numbers(texts, CLAUSE_RE):
        # 正文里一个条文号都没有：只可能是 GB/T 1.1 写法（企标），直接走那条路
        return build_dep(blocks, meta)[0]

    regions = split_regions(blocks)
    chunks: list[Chunk] = []
    orphan: list[str] = []
    for r in regions:
        chunks.extend(chunk_region(blocks, r, meta, orphan if r.kind == "正文" else None))

    # 有条文号不等于该走国标路径：DEP-T-AR2104 两种写法混用，走国标路径丢 16% 正文，
    # 走企标路径却能对齐目次。与其按编号前缀硬编码，不如两条都跑、取丢字少的那条。
    kept0 = sum(len(c.text) for c in chunks if c.section_type == "正文")
    lost0 = sum(len(t) for t in orphan)
    # 除了丢字，章号错位同样是「路径选错了」的征兆：DEP-T-AR2106 只丢 3.9%，
    # 但 34 条条文挂在了错误的章下——它的章标题是 Word 自动编号，正文里看不见，
    # 于是条文都归到了上一个看得见的标题底下。
    body = [c for c in chunks if c.section_type == "正文" and c.clause_no[:1].isdigit()]
    askew = sum(
        1 for c in body
        if (s := c.chapter_path.split(" / ")[0]) and s.split(" ")[0] != c.clause_no.split(".")[0]
    )
    if lost0 / max(kept0 + lost0, 1) > 0.05 or askew > max(5, 0.1 * len(body)):
        alt, diag = build_dep(blocks, meta, quiet=True)
        # 不要求企标路径的字数更多，只要求不显著更少：两条路径对前言、引用文件
        # 清单的取舍本就不同（AR2106 企标 6232 字 vs 国标 6759 字），
        # 但企标那边 45 条章节全对，国标那边 37 条里有 34 条挂错章。
        if diag["trusted"] and sum(len(c.text) for c in alt) >= 0.9 * kept0:
            print(f"  国标路径游离 {lost0 / max(kept0 + lost0, 1):.1%}、"
                  f"章号错位 {askew} 条，改用企标路径")
            return alt

    body = next((r.start for r in regions if r.kind == "正文"), 0)
    return _finish(chunks, orphan, regions, mineru_path, meta,
                   announced_mandatory(blocks, body))


# 书末的两样附件：「本规范用词说明」「引用标准名录」。
# 它们不编条号，切条器看不出它们已经不是正文，于是整段挂在最后一条下面——
# 09-24 实测 35 本书、每本正好一块中招。那一条的向量被一大段套话冲淡，
# 查「应」「宜」「可」这类用词时还会被当成相关条文顶上来。
# 必须整行匹配：条文里「按本规范用词说明执行」之类的句子不能误截。
BACK_MATTER_RE = re.compile(r"(?:^|\n)\s*(?:附[:：]?\s*)?(?:本(?:规范|标准|规程|规定)用词说明|引用标准名录)\s*(?=\n|$)")


def cut_back_matter(chunks: list[Chunk]) -> int:
    """把书末附件从吞了它的那一条里截掉，返回截了几块。条文说明不管——不进检索池。"""
    n = 0
    for c in chunks:
        if c.section_type == "条文说明":
            continue
        if m := BACK_MATTER_RE.search(c.text):
            c.text = c.text[: m.start()].rstrip()
            n += 1
    return n


def _finish(
    chunks: list[Chunk],
    orphan: list[str],
    regions: list[Region],
    mineru_path: Path | None,
    meta: dict,
    announced: set[str] | None = None,
) -> list[Chunk]:
    """回填页码、判强条、修订去重、修章节路径，并打印本本书的质量数字。"""
    if mineru_path and mineru_path.exists():
        pages, rev_end, notes_start = load_page_map(mineru_path)
        apply_pages(chunks, pages, rev_end, notes_start)
        print(f"  页码分区: 修订<{rev_end}  正文{rev_end}~{notes_start}  条文说明>={notes_start}")

    n_mand, basis = apply_mandatory(chunks, meta["std_code"], announced)

    # 公告点了名、正文里却找不到的条 —— 这是一条免费的完整性校验，
    # 而且校验的正好是最不能丢的那部分内容。实测 GB 50067 靠它抓出
    # 5.1.4、5.1.5 被 OCR 并进了上一条（两条都是强条）。
    if announced:
        body_nos = {c.clause_no for c in chunks if c.section_type == "正文"}
        if lost := sorted(announced - body_nos):
            print(f"  ! 公告列为强条、但正文里没切出来：{'、'.join(lost)}"
                  f"（{len(lost)}/{len(announced)} 条）")
    n_sup = apply_revision_priority(chunks)
    n_fix = repair_chapter_paths(chunks)

    by_region = {r.kind: sum(1 for c in chunks if c.section_type == r.kind) for r in regions}
    kept = sum(len(c.text) for c in chunks if c.section_type == "正文")
    lost = sum(len(t) for t in orphan)
    print(f"  区段: {by_region}")
    print(f"  正文游离 {len(orphan)} 段 / {lost} 字（{lost / max(kept + lost, 1):.1%}）")
    print(f"  强条 {n_mand} 条（依据：{basis}），修订取代 {n_sup} 条，修正章节路径 {n_fix} 条")
    return chunks


def build_from_mineru(content_list: Path, meta: dict) -> list[Chunk]:
    """没有 Word 源时走这条：正文和页码都取自 OCR 产物。

    切条规则一条都不改——只是把 content_list.json 适配成同样的 block 序列。
    """
    blocks = mdblocks.tidy(mdblocks.load(content_list))
    texts = [t for _, _, t in blocks]
    if not depdoc.has_inline_numbers(texts, CLAUSE_RE):
        chunks = build_dep(blocks, meta)[0]
        if chunks:
            return chunks
        # 企标路径靠 Word 的自动编号补条号，而 OCR 产物里没有那份元数据，
        # 于是一条都切不出来。正文里也找不到三级条号，那多半是 GB/T 1.1
        # 的两级写法（4.1 / 4.2）——改走两级路径，由目次做交叉验证。
        # 那条路径自己会在对不上目次时抛错，不会静悄悄返回空：
        # 写一个 0 行的 jsonl，库里就多一本「有这本书但一条都查不到」的规范，
        # 比缺这本书更坏（见 3.3d，已经发生过一次）。
        return build_twolevel(content_list, meta)

    regions = split_regions(blocks)
    chunks: list[Chunk] = []
    orphan: list[str] = []
    for r in regions:
        chunks.extend(chunk_region(blocks, r, meta, orphan if r.kind == "正文" else None))
    body = next((r.start for r in regions if r.kind == "正文"), 0)
    chunks = _finish(chunks, orphan, regions, content_list, meta,
                     announced_mandatory(blocks, body))

    # 混合体例：有的章用三级条号，有的章用两级（SH/T 3103 第 5、11 章是
    # 5.2.1.1 / 11.1.1，第 4、6～10 章却是 4.1、8.1）。三级路径在前一种章里
    # 切得到条，就认定这本「能切」，后一种章被整章打成一块——内容不丢，
    # 但「第8.1条」查不到，第 9 章一块 1300 多字。
    # 这种书改走两级路径：它按正文实际层级一路切到最细，章边界由目次核对。
    # 目次对不上会抛错，此时退回三级路径的结果——整章打包总比丢书强。
    #
    # 换之前还要过一道更严的闸：三级路径原本能单独查到的条，两级路径
    # 一条都不许丢。两级路径的「可信」允许漏两成的章，那是给「只有这条路」
    # 的书留的余地；这里是拿它去**替换**一个能用的结果，标准不能一样。
    # 09-24 实测 GBZ 1：OCR 把「6 工作场所基本卫生要求」粘在上一段句尾，
    # 两级路径只认块首的章标题，整个第 6 章并进了 5.3.6（一块 7504 字），
    # 6.2.1.1～6.2.1.15 原本是一条条切开的。GB 8624 则是附录 A、B 共 14 条
    # 并进了第 7 章。两本都过了「可信」，都被这道闸拦下。
    if packed := packed_chapters(chunks):
        print(f"  混合体例：第 {'、'.join(packed)} 章被整章打包，改试两级路径")
        try:
            alt = build_twolevel(content_list, meta)
        except ValueError as exc:
            print(f"  ! 两级路径不可信，保留三级路径结果：{exc}")
            return chunks
        if lost := uncovered(chunks, alt):
            print(f"  ! 两级路径会丢掉三级路径切得出的 {len(lost)} 条"
                  f"（{'、'.join(lost[:6])}{'…' if len(lost) > 6 else ''}），保留三级路径结果")
            return chunks
        return alt
    return chunks


def uncovered(old: list[Chunk], new: list[Chunk]) -> list[str]:
    """old 里单独成块的正文条号，new 里既没有它、也没有它的子条的。

    子条也算覆盖：只有标题的父条（「5.2.1 房间布置」）在两级路径里
    会并进子条的章节路径，本身不再成块，但它下面的每一条都还查得到。
    """
    have = [c.clause_no for c in new if c.section_type == "正文"]
    got = set(have)
    return [
        n for n in dict.fromkeys(c.clause_no for c in old if c.section_type == "正文")
        if n not in got and not any(h.startswith(n + ".") for h in have)
    ]


def packed_chapters(chunks: list[Chunk]) -> list[str]:
    """正文里整章打成一块、块内却有两条以上 N.k 两级条号行首的章。"""
    out = []
    for c in chunks:
        n = c.clause_no
        if c.section_type != "正文" or not re.fullmatch(r"\d{1,2}", n):
            continue
        heads = re.findall(rf"(?:^|\n)\s*{n}\.(\d{{1,2}})(?![\d.])\s*[一-龥]", c.text)
        if len(heads) >= 2:
            out.append(n)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把一本规范切成条级 chunk")
    ap.add_argument("--docx", required=True)
    ap.add_argument("--mineru", default=None, help="MinerU content_list.json，用于回填页码")
    ap.add_argument("--std-code", default="")
    ap.add_argument("--std-name", default="")
    ap.add_argument("--version", default="")
    ap.add_argument("--status", default="现行")
    ap.add_argument("--out", default="data/chunks")
    args = ap.parse_args(argv)

    docx_path = Path(args.docx)
    if not docx_path.exists():
        print(f"文件不存在: {docx_path}", file=sys.stderr)
        return 1

    meta = {
        "std_code": args.std_code or docx_path.stem,
        "std_name": args.std_name,
        "version": args.version,
        "status": args.status,
    }

    print(f"切条: {docx_path.name}")
    chunks = build(docx_path, Path(args.mineru) if args.mineru else None, meta)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{meta['std_code'].replace('/', '_').replace(' ', '_')}.jsonl"
    with out_file.open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")

    live = [c for c in chunks if not c.superseded and c.section_type != "条文说明"]
    print(f"  共 {len(chunks)} 块，进入检索池 {len(live)} 块 -> {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
