"""GB/T 1.1 两级条号体例的切条：一切结构判断都以**目次**为准。

为什么要单开一条路径
--------------------
工程建设标准写三级条号（5.5.17），`chunker.CLAUSE_RE` 认的就是那个。
GB/T 1.1《标准化工作导则》体例的文件只写到两级（4.1 / 4.2 / 7.13），
而国标路径**故意**拒绝两级编号——实测放行以后命中的全是目次行
（「5.4 平面布置 (67)」）和公式残片（「0.58倍;」），
会往检索池注入几百条假条文。那条拒绝不能放松。

所以两级体例只能另走一条，且这条路径必须自带一个比「长得像条号」更硬的判据。

判据：目次
----------
整个模块只有一个核心动作——**按目次的顺序，去正文里逐个找下一个编号**。
不是「扫出所有像编号的东西再过滤」，而是「我知道下一个该是谁，去找它」。
这一条反转带来三件事：

1. 交叉引用不再是问题。读到 5.1.x 时，期望集合是 {5.1.1, 5.2, 6}，
   于是 Q/SH 0841 条 5.2 里的「符合第4.1 条规定区域内的」不会被切开——
   「4.1」根本不在期望里。靠正则去排除「第」「表」「条」那套永远漏得完。

2. 页码粘连不再是问题。SH/T 3196 的目次 OCR 成
   「6 建筑设计其他特殊要求… …296.1建筑热工节能设计」——页码 29 和节号 6.1
   粘成了「296.1」。自由扫描会读出「96.1」；按期望找「6.1」则一击即中。

3. 章节标题必须**编号和标题都对得上目次**才算数（见 `_same_title`）。
   光对编号不够：正文里一句「4 抗震设防烈度为6度」和章标题长得一模一样。

对不上目次就**拒绝出货**（`diag["trusted"] is False`，调用方抛错），
不降级、不「尽力而为」。理由见 PLAN.md：库里多一本条号错乱的规范，
比缺这本书糟得多——缺书还留在待办里，错条号没人会发现。

条与款的分界也由目次定
----------------------
同样是「X.Y」，在两本书里的身份不同：
  Q/SH 0841 目次只列到章（1~8），所以 4.1、4.2 是**条**，
            而 5.1.1、5.1.2 比条更深，是 5.1 的**款**，不单独成块
            （条 5.1 的正文是「下列地区内的建筑应进行抗台风设计：」，
             把款摘走就只剩一个冒号，检索到它等于什么都没查到）；
  SH/T 3196 目次列了 6.1、6.2、6.3，所以那三个是**节**，
            条落在 6.1.1、6.1.2 这一级。
不写死层级，问目次。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import mdblocks

# kind, obj, text, page_idx
PagedBlock = tuple[str, object, str, int]

TOC_HEAD_RE = re.compile(r"^目\s*次\s*$")
# 必须整行匹配：目次的头一行本身常常就是「前言···· ···II」，
# 用前缀匹配会在目次刚开始的地方就停住，一个条目都抽不到。
TOC_STOP_RE = re.compile(r"^(前\s*言|Contents|Foreword)\s*$")
# 附录标题：正文里可能写成「附录A」独占一块，标题另起一块，故标题允许为空
APPENDIX_RE = re.compile(r"^附\s*录\s*([A-Z])\s{0,4}([^(（]{0,40})\s*$")


def load_paged(content_list: Path) -> list[PagedBlock]:
    """content_list.json → 带页码的 block 序列。

    和 `mdblocks.load` 做同样的格式适配，只多留一个 page_idx。
    多留这一个字段是为了**不走** `chunker.load_page_map` 那条反查路——
    那边拿条号当键去 OCR 原文里找页，而本路径会把并排的多条从一个块里切开，
    切出来的条在原文里没有独立的起始行，反查必然落空。
    页码就在手边，没有理由再猜一次。
    """
    items = json.loads(Path(content_list).read_text(encoding="utf-8"))
    out: list[PagedBlock] = []
    for it in items:
        page = it.get("page_idx") or 0
        kind = it.get("type")
        if kind in ("text", "equation"):
            if t := " ".join((it.get("text") or "").split()):
                out.append(("p", None, mdblocks.delatex(t), page))
        elif kind == "table":
            for cap in it.get("table_caption") or []:
                if c := " ".join(cap.split()):
                    out.append(("p", None, c, page))
            if md := mdblocks.table_markdown(it.get("table_body") or ""):
                out.append(("t", mdblocks.delatex(md), "", page))
            for fn in it.get("table_footnote") or []:
                if f := " ".join(fn.split()):
                    out.append(("p", None, f, page))
    return out


# --------------------------------------------------------------------------
# 目次
# --------------------------------------------------------------------------

# 标题起于汉字、止于下一个数字。数字在目次里既是页码也是下一条的编号，
# 两种身份都意味着本条标题到此为止，所以不必区分。
_TITLE = r"\s{0,3}([一-龥][^\d]{0,28})"
_TRIM = " .．…·、,，\t\n"


def _norm(t: str) -> str:
    """标题归一。连标点一起去掉——目次和正文的标点常常差一个顿号。"""
    return re.sub(r"[\s　、，,．.·（）()／/－—-]+", "", t).rstrip("。：:")


def _same_title(a: str, b: str) -> bool:
    """正文标题与目次标题是否为同一个。

    只要求一方是另一方的前缀，不要求全等：目次那头常被页码或点线截断
    （「建筑抗台风构造措施 2」里的 2 是页码，截断后正好少一截）。
    """
    x, y = _norm(a), _norm(b)
    return bool(x) and bool(y) and (x.startswith(y) or y.startswith(x))


def _successors(num: str) -> list[str]:
    """目次里合法的下一个编号：先下潜一级，再同级递增，再逐级回退。

    「6.1」→ ["6.1.1", "6.2", "7"]。顺序不影响结果（取正文里出现得最早的那个），
    列全是为了不漏掉「本章只有一节」这种跳法。
    """
    parts = [int(x) for x in num.split(".")]
    out = [".".join(map(str, parts + [1]))]
    for d in range(len(parts), 0, -1):
        out.append(".".join(map(str, parts[: d - 1] + [parts[d - 1] + 1])))
    return out


def _clause_candidates(base: str, last: str) -> list[str]:
    """章/节 `base` 之下，紧跟在条 `last` 后面合法的下一个条号。

    GB/T 1.1 里凡是带点的编号都是「条」，层级可深可浅，而且**同一本书里
    各章深浅不一**：SH/T 3103 第 8 章用 8.1、8.2，第 5 章却一路下到
    5.2.1.11，目次又只列到章。早先按「目次没列节 → 条落在第二级」一刀切，
    结果 5.2.1.1 那十几条全被当成 5.2 的续文，整章打成一块。

    所以条这一级不再由目次定，而是跟 `_successors` 一样按正文走：
    先下潜一级，再同级递增，再逐级回退。回退**到 base 为止**，
    不越过章/节——章和节的边界仍然只认目次，交叉验证在那一层做。

    「5.2.1」→ ["5.2.1.1", "5.2.2", "5.3"]（base = "5"）
    """
    if not last:
        return [f"{base}.1"]
    floor = base.count(".") + 1  # 条号至少比 base 深一级
    parts = [int(x) for x in last.split(".")]
    out = [".".join(map(str, parts + [1]))]
    for d in range(len(parts), floor, -1):
        out.append(".".join(map(str, parts[: d - 1] + [parts[d - 1] + 1])))
    return out


def parse_toc(texts: list[str]) -> list[tuple[str, str]]:
    """抽出目次，返回有序的 [(编号, 标题)]。

    取**第一个**「目次」：SH/T 3196 的条文说明自带第二份目次，
    拿错了会把全书结构建在条文说明上。
    """
    raw = ""
    for i, t in enumerate(texts):
        if TOC_HEAD_RE.match(t):
            buf: list[str] = []
            for t2 in texts[i + 1 : i + 8]:
                if TOC_STOP_RE.match(t2):
                    break
                buf.append(t2)
            raw = "  ".join(buf)
            break
    if not raw:
        return []

    out: list[tuple[str, str]] = []
    pos, expect = 0, ["1"]
    while expect:
        best: tuple[re.Match, str] | None = None
        for num in expect:
            m = re.compile(re.escape(num) + _TITLE).search(raw, pos)
            if m and (best is None or m.start() < best[0].start()):
                best = (m, num)
        if best is None:
            break
        m, num = best
        title = re.split(r"\s{2,}", m.group(1))[0].strip(_TRIM)
        if not title:
            break
        out.append((num, title))
        pos, expect = m.end(), _successors(num)
    return out


# --------------------------------------------------------------------------
# 正文
# --------------------------------------------------------------------------


@dataclass
class Item:
    num: str
    kind: str  # chapter | section | clause | appendix
    text: str
    page: int
    chapter: str = ""
    section: str = ""
    tables: list[str] = field(default_factory=list)
    # 章/节占位块开出来时为 True，收到任何正文或表格就转 False。
    # 见 `_drop_shell`：只有标题、什么都没收到的块不该进库。
    shell: bool = False


def _is_toc_line(text: str) -> bool:
    """像一条目次行：短，且以点线/空格 + 页码收尾。"""
    return len(text) <= 40 and bool(re.search(r"[.．…·\s]\s*\d{1,3}$", text))


def _bump(num: str) -> str:
    """同级下一条：「7.3」→「7.4」，「6.1.2」→「6.1.3」。"""
    parts = num.split(".")
    return ".".join(parts[:-1] + [str(int(parts[-1]) + 1)])


# 交叉引用的引导词与尾随词。期望集合挡不住这一类：
# SH/T 3196 第 5 章正文里「建、构筑物基本特征见表5.1。」中的「5.1」
# **恰好就是**当时期望的下一条条号，于是一句话被从中间劈开，
# 后半截连同全章 25 页大表一起挂到了一条根本不存在的「条 5.1」上。
# 期望集合管的是「哪个号」，这一对前后缀管的是「这个号是不是在被引用」。
_REF_LEAD = r"(?<![第表图式条附按见的])"
_REF_TAIL = r"(?![条款项、，,])"


def _find_clause(text: str, num: str) -> tuple[int, int] | None:
    """在正文里找编号 `num` 作为**条文起点**的出现位置。

    边界四道：
      `(?<![\\d.A-Za-z])` 挡住「GB50016」的尾巴和「0.65kN」的小数位；
      `_REF_LEAD` 挡住「见表5.1」「本文件第7条」这类引用；
      `(?![\\d.])` 挡住「4.1」回溯匹配「4.12」的前两位，
                   也挡住条文说明里的「5.1.1」被当成条 5.1；
      `_REF_TAIL` 挡住「4.1条」「4.1、4.2」。
    两级编号本来就短，少一道卡就会到处都是。
    """
    m = re.search(
        _REF_LEAD + r"(?<![\d.A-Za-z])" + re.escape(num) + r"(?![\d.])" + _REF_TAIL + r"\s{0,3}",
        text,
    )
    return (m.start(), m.end()) if m else None


def _heading_at(text: str, num: str, toc_title: str) -> int | None:
    """本块是不是目次里那个编号为 `num` 的章/节标题？是则返回标题起点。

    三重条件缺一不可：出现在块首、编号后面直接是汉字、**标题和目次对得上**。
    最后一条是这条路径的地基。只验编号的话，SH/T 3196 条 4.3 正文里那句
    「……GB50016—2014和《石油化工企业防火设计规范》GB50160一2008相关规定确定」
    并不会出事，但 GB 50011 附录里的款行「4 抗震设防烈度为6度，设计基本地震
    加速度值为 0.05g」会——它和章标题在字面上完全无法区分。
    """
    m = re.match(re.escape(num) + r"\s{0,4}(?=[一-龥])", text)
    if not m:
        return None
    return m.end() if _same_title(text[m.end() :], toc_title) else None


def build_structure(
    blocks: list[PagedBlock],
    toc: list[tuple[str, str]],
    start: int,
    end: int,
    strict: bool = True,
) -> tuple[list[Item], dict]:
    """按目次走一遍正文，切出章 / 节 / 条。

    每一步只在两类候选里挑：目次的下两个条目（章或节），
    以及当前章节下的下一条条号。挑出现得最早的那个。
    「下两个」而不是「下一个」，是给 OCR 漏掉一个标题留的余地——
    漏一个只让那一章的条文归到前一章名下，局部且有界；
    而放开成「后面任意一个」就等于自由扫描，前面说的三个好处全部失效。

    `strict=False` 才放开成「目次里剩下的任意一个」，只给条文说明区用：
    那一区从第 3 章起头（前两章没有条文说明），窗口卡死在开头就永远追不上，
    整区会一个块都切不出来。条文说明不进检索池，放宽的代价只是它自己，
    不会污染正文。
    """
    toc_nums = [n for n, _ in toc]
    # 某一章在目次里有没有节，决定了「条」落在第几级。见模块 docstring。
    has_sec = {c: any(n.startswith(c + ".") for n in toc_nums) for c in toc_nums if "." not in c}

    items: list[Item] = []
    cur: Item | None = None
    chapter = section = ""  # 「编号 标题」
    chap_no = sec_no = ""
    clause_no = ""  # 本章/本节里上一条的条号
    toc_pos = 0
    in_appendix = False
    in_toc = False
    orphan: list[str] = []
    hit_nums: list[str] = []

    def open_item(num: str, kind: str, text: str, page: int, shell: bool = False) -> None:
        nonlocal cur
        cur = Item(num, kind, text, page, chapter, section, shell=shell)
        items.append(cur)

    def absorb(text: str) -> None:
        if cur is not None:
            cur.text = f"{cur.text}\n{text}" if cur.text else text
            cur.shell = False
        elif text:
            orphan.append(text)

    for i in range(start, end):
        kind, obj, text, page = blocks[i]

        if kind == "t":
            if cur is not None and isinstance(obj, str) and obj:
                cur.tables.append(obj)
            continue
        if not text:
            continue

        # 条文说明区自带一份目次（「3术语 …36」「4基本规定. ..36」）。
        # 那些行编号和标题都跟正文目次对得上，不挡掉就会被当成章标题吃进去，
        # 而真正的章标题排在它们后面，届时已经追不上了。
        # 判据是行尾的页码——正文里的章标题不带页码。
        if TOC_HEAD_RE.match(text):
            in_toc = True
            continue
        if in_toc:
            if _is_toc_line(text):
                continue
            in_toc = False

        if m := APPENDIX_RE.match(text):
            # 附录与章平级，且之后不再有正文章节——继续拿章号去匹配的话，
            # 附录里的款行会被当成新的一章。
            in_appendix = True
            chapter = f"附录{m.group(1)} {m.group(2).strip()}".rstrip()
            section, chap_no, sec_no, clause_no = "", "", "", ""
            open_item(f"附录{m.group(1)}", "appendix", text, page)
            continue
        if in_appendix:
            absorb(text)
            continue

        # ---- 候选一：目次的下两个条目（只在块首、且标题对得上）
        head = None
        # 非严格模式下整份目次都可选：条文说明区会把章标题再走一遍
        # （3、4、5、6 各出现两次），卡着 toc_pos 只能认下第一遍。
        window = range(toc_pos, toc_pos + 2) if strict else range(len(toc))
        for j in window:
            if j >= len(toc):
                break
            num, title = toc[j]
            if (k := _heading_at(text, num, title)) is not None:
                head = (j, num, text[k:].strip(_TRIM))
                break
        if head is not None:
            j, num, title = head
            toc_pos = j + 1
            hit_nums.append(num)
            clause_no = ""
            if "." in num:
                sec_no, section = num, f"{num} {title}"
            else:
                chap_no, chapter = num, f"{num} {title}"
                sec_no, section = "", ""
            # 章/节标题自成一个占位块：SH/T 3196 第 5 章的正文和 25 页大表
            # 都挂在章下，没有占位块就整章游离。什么都没接到的会在
            # `_drop_shell` 里扔掉，不会留下一堆只有标题的空块。
            open_item(num, "section" if "." in num else "chapter", text, page, shell=True)
            continue

        # ---- 候选二：本章/本节的下一条。没有章就没有条。
        rest, off = text, page
        while rest:
            base = ""
            if chap_no:
                if sec_no:
                    base = sec_no
                elif not has_sec.get(chap_no):
                    base = chap_no
            hit = None
            if base:
                # 取正文里出现得最早的那个候选。见 `_clause_candidates`。
                for want in _clause_candidates(base, clause_no):
                    h = _find_clause(rest, want)
                    if h and (hit is None or h[0] < hit[0][0]):
                        hit = (h, want)
            if hit is None:
                absorb(rest)
                break
            (s, e), want = hit
            if s:
                absorb(rest[:s])
            clause_no = want
            open_item(want, "clause", rest[e:], off)
            # 已经开了新条，块内剩下的文字归它；继续找同一块里的下一条
            rest = cur.text  # type: ignore[union-attr]
            cur.text = ""  # type: ignore[union-attr]

    n_clause = sum(1 for it in items if it.kind == "clause")
    chaps = [n for n in toc_nums if "." not in n]
    got = [n for n in hit_nums if "." not in n]
    diag = {
        "toc": len(toc),
        "toc_chapters": len(chaps),
        "toc_hit": len(hit_nums),
        "chapters_hit": len(got),
        "clauses": n_clause,
        "orphan": orphan,
        # 交叉验证的结论。三条都要成立：目次抽得出来、正文把目次的章
        # 按原顺序走了一遍（允许漏一章）、确实切出了成规模的条文。
        "trusted": (
            len(chaps) >= 3
            and got == [n for n in chaps if n in got]
            and len(got) >= max(3, int(len(chaps) * 0.8))
            and n_clause >= 10
        ),
    }
    return items, diag


_SENTENCE = re.compile(r"[。；;：:，,]")


def _fold_title_clauses(items: list[Item]) -> list[Item]:
    """只有标题、下面还有子条的「条」，并进子条的章节路径里，本身不入库。

    GB/T 1.1 允许带标题的条再往下分：「5.2.1 房间布置」下面是 5.2.1.1～5.2.1.11。
    这种父条自己只有四个字，单独成块检索时只会命中噪音；
    而它的子条正文里又不带「房间布置」这几个字——标题得跟着子条走。

    判据：紧跟着的下一项就是它的子条，且它自己像标题（短、没有句读、没挂表）。
    有正文的父条（「5.1 …应符合下列规定：」后面跟子条）照旧留着。
    """
    out: list[Item] = []
    for i, it in enumerate(items):
        nxt = items[i + 1] if i + 1 < len(items) else None
        is_parent = it.kind == "clause" and nxt is not None and nxt.num.startswith(it.num + ".")
        title = it.text.strip()
        if is_parent and not it.tables and len(title) <= 30 and not _SENTENCE.search(title):
            head = f"{it.num} {title}".strip()
            for d in items[i + 1 :]:
                if not d.num.startswith(it.num + "."):
                    break
                d.section = f"{d.section} / {head}" if d.section else head
            continue
        out.append(it)
    return out


def _drop_shell(items: list[Item]) -> list[Item]:
    """扔掉「开了标题但一个字都没收到」的章/节占位块。

    占位块是为了接住不编号的章首正文和表格。真接到了东西就留着
    （SH/T 3196 第 5 章全靠它兜住 25 页的大表），什么都没接到
    （下一块就是本章第一条）就只剩一行标题，留着只会污染检索结果。
    国标路径的 `chunk_region.flush()` 也是这么处理的，两边保持一致。
    """
    return [it for it in items if not it.shell or it.tables]


def build(
    blocks: list[PagedBlock], regions: list[tuple[str, int, int]]
) -> tuple[list[tuple[str, Item]], dict]:
    """外部入口：按区段各走一遍，返回 ([(区段名, Item)], diag)。

    blocks 来自 `load_paged`，regions 由 `chunker.split_regions` 在**同一份**
    blocks 上算出——`load_paged` 不做块拆分，所以两边的下标是一套。
    （`mdblocks.tidy` 会拆块，拿它的下标来索引这里必然错位。）

    只有「正文」区参与可信度判定：条文说明不进检索池，
    它切得好不好都不该左右「这本书能不能入库」这个判断。
    而「修订」区在 GB/T 1.1 体例里就是封面、目次、前言（这类文件不发局部修订
    公告），走一遍是白走，所以干脆不走——但它有多少段没入库要报出来，
    见 `diag["front"]`。
    """
    toc = parse_toc([t for _, _, t, _ in blocks])

    out: list[tuple[str, Item]] = []
    diag: dict = {"toc": len(toc), "trusted": False, "orphan": [], "regions": {}, "front": 0}
    for kind, s, e in regions:
        if kind == "修订":
            diag["front"] = sum(1 for b in blocks[s:e] if b[0] == "p" and b[2])
            continue
        items, d = build_structure(blocks, toc, s, min(e, len(blocks)), strict=(kind == "正文"))
        items = _fold_title_clauses(_drop_shell(items))
        diag["regions"][kind] = len(items)
        out.extend((kind, it) for it in items)
        if kind == "正文":
            diag.update(d)
        else:
            # 非正文区的游离文字也要有人报。上一轮的教训就是「静悄悄地少了东西」
            # 比「大声地出错」难发现得多（见 PLAN.md 3.3d）。
            diag.setdefault("other_orphan", {})[kind] = sum(len(t) for t in d["orphan"])
    return out, diag
