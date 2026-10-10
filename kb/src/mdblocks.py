"""把 MinerU 的 content_list.json 转成切条器认识的 block 序列。

扫描件没有 Word 源，正文只能来自 OCR。但**不为此另写一套切条逻辑**：
chunk_region / CHAPTER_RE / confirms() 那些规则是被真实排版逼出来的，
换个来源重写一遍等于把踩过的坑再踩一遍。这里只做格式适配——
content_list.json 本来就是「一段文字 / 一张表 + 页码」的扁平列表，
和 iter_block_items 产出的 (kind, obj, text) 结构几乎一一对应。

页码不用再猜：content_list 的每一项自带 page_idx，比 pagefind 反查可靠。
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

Block = tuple[str, object, str]


class _TableParser(HTMLParser):
    """HTML 表格转 Markdown，列位规则与 Word 那条路径保持一致。

    colspan=N → 文本出现一次，后面补 N-1 个空格子；
    rowspan=N → 后续 N-1 行的同一列继承本格的值。

    两条规则都不能省：合并格按占用列数重复文本会把「名称」变成
    「名称 | 名称 | 名称」；而纵向合并格留空会让行标签丢失
    （「| | 高层 | 病房部分 |」看不出说的是医疗建筑）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] = []
        self._buf: list[str] = []
        self._span = 1
        self._in_cell = False
        self._carry: dict[int, tuple[str, int]] = {}  # 列 → (值, 剩余行数)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._in_cell, self._buf = True, []
            self._span = max(1, int(a.get("colspan") or 1))
            self._rowspan = max(1, int(a.get("rowspan") or 1))

    def handle_data(self, data):
        if self._in_cell:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            txt = " ".join("".join(self._buf).split())
            # 先把上面传下来的纵向合并值填掉，再放本格——否则列位会错开
            self._fill_carry()
            col = len(self._row)
            self._row.append(txt)
            self._row.extend([""] * (self._span - 1))
            if self._rowspan > 1:
                self._carry[col] = (txt, self._rowspan - 1)
        elif tag == "tr":
            self._fill_carry()
            if any(self._row):
                self.rows.append(self._row)
            self._decay_carry()
            self._row = []

    def _fill_carry(self) -> None:
        while (c := len(self._row)) in self._carry:
            self._row.append(self._carry[c][0])

    def _decay_carry(self) -> None:
        self._carry = {c: (v, n - 1) for c, (v, n) in self._carry.items() if n > 1}

    def markdown(self) -> str:
        return "\n".join("| " + " | ".join(r) + " |" for r in self.rows)


def table_markdown(html: str) -> str:
    p = _TableParser()
    p.feed(html or "")
    p.close()
    if p._row and any(p._row):          # 最后一行没闭合 </tr> 的情况
        p.rows.append(p._row)
    return p.markdown()


def load(content_list: Path) -> list[Block]:
    """content_list.json → blocks。

    表格前面补一条标题段（「表5.1.1 民用建筑的分类」）。OCR 把表题单独放进
    table_caption，不放回正文流的话，BM25 就搜不到表名，而同事查表恰恰是报表号的。
    """
    items = json.loads(Path(content_list).read_text(encoding="utf-8"))
    out: list[Block] = []
    for it in items:
        kind = it.get("type")
        if kind == "text":
            if t := " ".join((it.get("text") or "").split()):
                out.append(("p", None, t))
        elif kind == "table":
            for cap in it.get("table_caption") or []:
                if c := " ".join(cap.split()):
                    out.append(("p", None, c))
            if md := table_markdown(it.get("table_body") or ""):
                out.append(("t", md, ""))
            for fn in it.get("table_footnote") or []:
                if f := " ".join(fn.split()):
                    out.append(("p", None, f))
        elif kind == "equation":
            # 行间公式在规范里多半是计算式，正文会用「按式(5.2.1)计算」引到它。
            # 留个占位，免得上下两段被粘成一段。
            if t := " ".join((it.get("text") or "").split()):
                out.append(("p", None, t))
    return out


# --------------------------------------------------------------------------
# 去 LaTeX
#
# MinerU 把带单位的数值当行内公式输出：「建筑高度大于 $100\mathrm{m}$」。
# 数字本身没错，但这样存进库有两个后果：同事看到的是 `$100\mathrm{m}$`，
# 而 BM25 分词后「100m」根本对不上。实测全库 12.7% 的段落含 $...$。
#
# 命令表按实际出现频次取（\mathrm 11258 次、\circ 740、\sim 459、\frac 377…），
# 不做通用 LaTeX 解析——规范里的行内公式就这么几种写法，
# 认不出的原样保留，总好过解析错把数值改了。
# --------------------------------------------------------------------------

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "Delta": "Δ",
    "epsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "lambda": "λ",
    "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ",
    "tau": "τ", "phi": "φ", "Phi": "Φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Omega": "Ω", "Sigma": "Σ", "varphi": "φ", "varepsilon": "ε",
}
_SYMBOL = {
    "times": "×", "cdot": "·", "sim": "~", "approx": "≈", "pm": "±",
    "leqslant": "≤", "leq": "≤", "le": "≤", "geqslant": "≥", "geq": "≥", "ge": "≥",
    "neq": "≠", "ll": "≪", "gg": "≫", "infty": "∞", "prime": "′",
    "sum": "Σ", "prod": "Π", "int": "∫", "partial": "∂", "%": "%", "&": " ",
}
_SUP = {"0": "⁰", "1": "¹", "2": "²", "3": "³"}

_BRACED = re.compile(r"\\(mathrm|mathbf|mathit|text|rm|bf|operatorname)\s*\{([^{}]*)\}")
_DEGREE = re.compile(r"\^\s*\{?\s*\\circ\s*\}?")
_FRAC = re.compile(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SQRT = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_TAG = re.compile(r"\\tag\s*\{([^{}]*)\}")
_SUB = re.compile(r"_\s*\{([^{}]*)\}")
_SUPER = re.compile(r"\^\s*\{([^{}]*)\}")
_CMD = re.compile(r"\\([a-zA-Z]+)")
_SPACES = re.compile(r"\\[,;:!> ]|\\quad|\\qquad|\\left|\\right|\\displaystyle|\\limits")


def _delatex(s: str) -> str:
    s = _TAG.sub(r" (\1)", s)
    # 裸的 ~ 在 LaTeX 里是不换行空格（`100~\mathrm{m}`），不是「约等于」。
    # 必须赶在 \sim → ~ 之前替掉，否则分不清哪个 ~ 是数值区间的意思。
    s = s.replace("~", " ")
    s = _SPACES.sub(" ", s)
    for _ in range(3):                       # \mathrm{\mathbf{x}} 这类嵌套
        s2 = _BRACED.sub(r"\2", s)
        if s2 == s:
            break
        s = s2
    s = _DEGREE.sub("°", s)                  # 130^{\circ}C → 130°C
    s = _FRAC.sub(r"\1/\2", s)
    s = _SQRT.sub(r"√(\1)", s)
    s = _SUB.sub(r"\1", s)                   # B_{2} → B2
    s = _SUPER.sub(lambda m: _SUP.get(m.group(1), "^" + m.group(1)), s)   # m^{2} → m²
    s = re.sub(r"\^(\d)", lambda m: _SUP.get(m.group(1), "^" + m.group(1)), s)
    s = re.sub(r"_(\w)", r"\1", s)
    s = _CMD.sub(lambda m: _GREEK.get(m.group(1), _SYMBOL.get(m.group(1), m.group(0))), s)
    return s.replace("{", "").replace("}", "")


_MATH = re.compile(r"\$+([^$]*?)\$+", re.S)


def delatex(text: str) -> str:
    """只动 $...$ 里面的内容，外面的中文一个字不碰。"""
    if "$" not in text:
        return text
    return " ".join(_MATH.sub(lambda m: _delatex(m.group(1)), text).split())


# OCR 常把「5.5.17」识别成「5. 5. 17」。
# 只修这一类有把握的，不做通用纠错——猜错条号比漏掉条号危害大。
_SPACED_NO = re.compile(r"^(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,3})([A-Z]?)(?=[^\d.]|$)")


# 一个文本块里挤了好几条。MinerU 按版面切块，一栏文字连排下来常常并成一块，
# 实测 GB 50067 的 p21 把「条 5.1.3 的第 4 款 + 条 5.1.4 + 条 5.1.5」并成了一个块。
# 切条器只认行首条号，后两条就被吞进上一条——文字还在，但条号、页码、强条标记全丢了，
# 而这两条恰恰都是强制性条文。
#
# 判据：句号之后紧跟三级条文号。必须排除交叉引用——
# 「应按本规范第5.1.4条执行」里的条号前面有「第」「表」「图」「式」，
# 且后面跟着「条」，这两种都不能切。
_INLINE_NO = re.compile(r"(?<=[。；])(?=\d{1,2}(?:\.\d{1,3}){2}[A-Z]?(?![.\d条、，,]))")


def split_inline(text: str) -> list[str]:
    """把并成一块的多条文本拆开。拆不出就原样返回单元素列表。"""
    parts = [p.strip() for p in _INLINE_NO.split(text)]
    return [p for p in parts if p] or [text]


def tidy(blocks: list[Block]) -> list[Block]:
    """去掉行内公式的 LaTeX 包装，收掉条文号里被 OCR 插进去的空格，拆开并排的条文。"""
    out: list[Block] = []
    for kind, obj, text in blocks:
        if kind == "p":
            text = delatex(text)
            if m := _SPACED_NO.match(text):
                text = f"{m.group(1)}.{m.group(2)}.{m.group(3)}{m.group(4)}" + text[m.end():]
            for piece in split_inline(text):
                out.append((kind, obj, piece))
            continue
        if kind == "t" and isinstance(obj, str):
            obj = delatex(obj)
        out.append((kind, obj, text))
    return out
