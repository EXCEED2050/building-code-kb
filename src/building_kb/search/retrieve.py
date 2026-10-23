"""混合检索：条号直达 + BM25 + 向量，RRF 融合后重排。

设计上的一条硬规矩：**条号查询不走语义检索**。
「GB 50016 第 5.5.17 条怎么规定的」这类问题占实际提问的一大半，答案是确定的，
不该交给相似度去猜。正则识别出规范号/条号就直接按 metadata 取，
取不到就明说没有，绝不用一条「看起来像」的条文顶替。

没有 API key 时自动降级为纯 BM25——语料只有 58 万字，关键词检索本身就不弱，
可以先用起来，等密钥到位再补向量层。

用法:
    python -m building_kb.search.retrieve "厂房内疏散楼梯的最小净宽度"
    python -m building_kb.search.retrieve "GB 50016 5.5.17"
    python -m building_kb.search.retrieve --repl
"""

from __future__ import annotations

import argparse
import glob
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi

from building_kb import paths

from .. import providers
from ..verification import apply_review, load_reviews

CHROMA_DIR = str(paths.INDEX)
COLLECTION = "clauses"

# --------------------------------------------------------------------------
# 查询解析
# --------------------------------------------------------------------------

# 规范编号：GB 50016 / GB/T 50378 / JGJ 46 / SH/T 3006 / Q/SH 0841 / GB50016（无空格）
#
# 「Q/SH」必须排在「SH」前面。中石化企标写作 Q/SH 0841，而 SH 这一支在后面，
# 正则的分支是从左到右取第一个成功的：反过来的话「Q/SH 0841」会被读成「SH 0841」，
# 前缀对不上台账里的「Q/SH 0841-2022」，整本企标的条号直达全部失效
# （实测 Q/SH 0841、Q/SH 0705 两本都查不到）。
STD_RE = re.compile(
    r"\b((?:Q\s*/\s*SH|GB|JGJ|CJJ|SH|HG|SY|TSG|DL|NB|CECS)(?:\s*/\s*T)?)\s*-?\s*(\d{3,5})(?!\d)(?:\s*[-—－]\s*(\d{4})(?!\d))?",
    re.I,
)
# 条文号：5.5.17 / 5.5.17A / A.0.1。两级（5.5）在这里不认——三级条号本身就够独特，
# 裸写也不会跟别的数字混。
CLAUSE_RE = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d{1,3}){2}[A-Z]?|[A-Z](?:\.\d{1,2}){2})(?![\d.])")
# 两级条号（GB/T 1.1 体例，如 Q/SH 0841 的 7.10、SH/T 3196 的 4.3）必须**带称谓**
# 才认：写成「第7.10条」「7.10条」算，裸写的「7.10」不算。
#
# 不是小心过头。裸的两级数字在提问里太常见了——「坡度 6.5%」「不应小于 4.2m」——
# 而 Q/SH 0841 恰好就有第 6.5 条。条号直达一旦命中就**立即返回、不再走相似度**
# （见 search()），所以一次误判不是多给一条噪音，而是把正确答案整个挡掉。
# 三级条号没这个问题，故两条规则分开写。
CLAUSE2_RE = re.compile(
    r"第\s*(\d{1,2}(?:\.\d{1,3})+[A-Z]?)(?![\d.])"
    r"|(?<![\d.])(\d{1,2}(?:\.\d{1,3})+[A-Z]?)(?![\d.])\s*条"
)
# 用户问「强条」「强制性条文」时只给强条
MANDATORY_RE = re.compile(r"强制性条文|强条|必须执行")
# 表格类提问，命中则让带表的条文加权
TABLE_RE = re.compile(
    r"表|多少|几米|限值|数值|不应(?:小于|大于|少于|超过)|最大|最小|间距|宽度|高度|面积"
)

STOP = set(
    "的了是在和及与或对其为以按应该本条等这那要可将由于从到上下中之所对于如果并且但是一个进行"
)

# 比对规范名时用：把空格、顿号、书名号、括号一类的差异抹平
SQUASH_RE = re.compile(r"[\s　、，,．.·（）()／/《》〈〉【】\[\]\-—－_]+")


def squash(s: str) -> str:
    return SQUASH_RE.sub("", s)


@dataclass
class Hit:
    chunk: dict
    score: float
    route: str  # 命中路径，调试和给用户看依据用

    def cite(self) -> str:
        p = self.chunk["page_idx"]
        page = f" P{p + 1}" if p is not None else ""
        return f"[{self.chunk['std_code']} 第{self.chunk['clause_no']}条{page}]"


def parse_query(q: str) -> dict:
    """从自然语言里抠出可以精确过滤的东西。"""
    stds = [
        "{} {}".format(re.sub(r"\s+", "", a.upper()), b) + (f"-{year}" if year else "")
        for a, b, year in STD_RE.findall(q)
    ]
    # 归一成台账里的写法："GB/T" + 空格 + 数字
    stds = [re.sub(r"\s+", " ", s).strip() for s in stds]
    # 两级放在后面：同一句里三级条号更明确，让它先命中
    nos = CLAUSE_RE.findall(q)
    nos += [a or b for a, b in CLAUSE2_RE.findall(q) if (a or b) not in nos]
    return {
        "std_codes": stds,
        "clause_nos": nos,
        "mandatory_only": bool(MANDATORY_RE.search(q)),
        "wants_table": bool(TABLE_RE.search(q)),
    }


def code_matches(actual: str, requested: str) -> bool:
    """不带年号时展开版本；带年号时只能匹配该版，编号不能按数字前缀混用。"""
    actual, requested = (re.sub(r"\s+", "", s.upper()) for s in (actual, requested))
    return actual == requested if "-" in requested else actual.split("-", 1)[0] == requested


def tokenize(text: str) -> list[str]:
    """jieba 搜索模式切词。

    搜索模式会把长词再拆一层（「安全疏散距离」→ 安全/疏散/距离/安全疏散/疏散距离），
    这正是需要的：用户写「疏散宽度」而规范写「疏散总净宽度」时还能对上。
    """
    return [
        t
        for t in jieba.cut_for_search(text)
        if t.strip() and t not in STOP and not re.fullmatch(r"[\W_]+", t)
    ]


# --------------------------------------------------------------------------
# 检索器
# --------------------------------------------------------------------------


class Retriever:
    def __init__(self, chunks_dir: str = str(paths.CHUNKS), chroma_dir: str = CHROMA_DIR):
        self.pool = self._load(chunks_dir)
        self.by_key: dict[tuple[str, str], dict] = {
            (c["std_code"], c["clause_no"]): c for c in self.pool
        }
        self._seed_dict()
        self.names = self._index_names()
        self.idx_by_code: dict[str, list[int]] = defaultdict(list)
        for i, c in enumerate(self.pool):
            self.idx_by_code[c["std_code"]].append(i)
        self.corpus = [tokenize(self._doc_text(c)) for c in self.pool]
        self.bm25 = BM25Okapi(self.corpus)
        self.chroma_dir = chroma_dir
        self._coll = None
        self._embedder = None
        self._reranker = None

    # -- 装载 --------------------------------------------------------------

    @staticmethod
    def _load(d: str) -> list[dict]:
        out = []
        reviews = load_reviews(d)
        for f in sorted(glob.glob(str(Path(d) / "*.jsonl"))):
            with open(f, encoding="utf-8") as source:
                for line in source:
                    c = json.loads(line)
                    # 说明是解释不是规定，不能混进检索池当条文引用。
                    if c["superseded"] or c["status"] == "废止" or c["section_type"] == "条文说明":
                        continue
                    out.append(apply_review(c, reviews))
        return out

    @staticmethod
    def _doc_text(c: dict) -> str:
        """索引文本 = 章节路径 + 正文 + 表格。

        章节路径要进去：用户问「厂房防火间距」，条文正文里常只写
        「不应小于表 3.4.1 的规定」，「防火间距」三个字只出现在节标题上。
        """
        return "\n".join([c["chapter_path"], c["text"], *c["tables"]])

    def _seed_dict(self) -> None:
        """把语料里的专业词喂给 jieba，避免「耐火极限」被切成「耐火/极限」。"""
        for c in self.pool:
            for seg in re.split(r"[\s/]+", c["chapter_path"]):
                w = re.sub(r"^[\d.A-Z附录]+", "", seg).strip()
                if 2 <= len(w) <= 8:
                    jieba.add_word(w, freq=1000)

    def _index_names(self) -> dict[str, str]:
        """规范名 → 编号。同事张口就是名字，很少报编号。

        「建筑抗震设计规范 5.1.4」如果只认编号，条号直达会把所有规范里的
        5.1.4 一起端上来，排第一的还未必是他要的那本。
        名字带的后缀（「(附2016年局部修订)」）要允许省略，故正反两种写法都建索引。
        """
        out: dict[str, str] = {}
        for c in self.pool:
            code, name = c["std_code"], c["std_name"]
            for form in {name, re.sub(r"[（(].*", "", name)}:
                key = squash(form)
                if len(key) < 6:
                    continue
                # 两本规范重名就谁都不认——引错规范比认不出规范危害大
                out[key] = code if out.get(key, code) == code else ""
        return out

    def resolve_names(self, q: str) -> list[str]:
        """用户在问句里写了规范全名时，把它当成编号过滤条件。"""
        qn = squash(q)
        matched = [k for k, v in self.names.items() if v and k in qn]
        # 「石油化工控制室设计规范」里含着别的短名时，只认最长的那个
        codes: list[str] = []
        for k in matched:
            if any(k != o and k in o for o in matched):
                continue
            if self.names[k] not in codes:
                codes.append(self.names[k])
        return codes

    # -- 各路检索 ----------------------------------------------------------

    def exact(self, p: dict) -> list[Hit]:
        """条号直达。规范号+条号都有时唯一命中；只有条号时列出各规范的同号条文。"""
        hits: list[Hit] = []
        for no in p["clause_nos"]:
            if p["std_codes"]:
                for code in p["std_codes"]:
                    for (sc, cn), c in self.by_key.items():
                        if cn == no and code_matches(sc, code):
                            hits.append(Hit(c, 1.0, "条号直达"))
            else:
                hits.extend(
                    Hit(c, 1.0, "条号直达（未指定规范）")
                    for (_, cn), c in self.by_key.items()
                    if cn == no
                )
        return hits

    def allowed(self, p: dict) -> set[str] | None:
        """把（可能不带年号的）编号前缀展开成语料里的具体编号。None 表示不限。"""
        if not p["std_codes"]:
            return None
        return {sc for sc in self.idx_by_code if any(code_matches(sc, c) for c in p["std_codes"])}

    def keyword(self, q: str, k: int, allow: set[str] | None = None) -> list[Hit]:
        scores = self.bm25.get_scores(tokenize(q))
        # 限定了规范就只在那本里排名次。若只是照常取全库前 k 再过滤，
        # 「建筑设计技术规定里卫生间的做法」会因为那本一条都没挤进前 30 而颗粒无收。
        idx = (
            range(len(scores)) if allow is None else [i for c in allow for i in self.idx_by_code[c]]
        )
        order = sorted(idx, key=lambda i: -scores[i])[:k]
        return [Hit(self.pool[i], float(scores[i]), "BM25") for i in order if scores[i] > 0]

    def vector(self, q: str, k: int, allow: set[str] | None = None) -> list[Hit]:
        try:
            coll = self._collection()
            if coll is None:
                return []
            vec = self._embed().embed([q])[0]
            r = coll.query(
                query_embeddings=[vec],
                n_results=k,
                include=["metadatas", "distances"],
                **({"where": {"std_code": {"$in": sorted(allow)}}} if allow else {}),
            )
        except Exception as exc:  # noqa: BLE001 - 向量层挂了要能退回 BM25
            print(f"  ! 向量检索不可用，已退回关键词：{exc}")
            return []
        out = []
        # 回原始 chunk 而不是用 Chroma 里的副本：切条器改了但索引还没重建时，
        # 至少正文、表格、页码是最新的。对不上的说明索引过期，直接跳过。
        for meta, dist in zip(r["metadatas"][0], r["distances"][0]):
            if c := self.by_key.get((meta["std_code"], meta["clause_no"])):
                out.append(Hit(c, 1.0 - dist, "向量"))
        return out

    # -- 融合 --------------------------------------------------------------

    def search(self, q: str, k: int = 8, rerank: bool = True) -> tuple[list[Hit], dict]:
        p = parse_query(q)
        if not p["std_codes"]:
            p["std_codes"] = self.resolve_names(q)

        if exact := self.exact(p):
            # 条号命中就到此为止：答案是确定的，再掺语义结果只会引入噪音
            return self._post(exact, p, k), p
        if p["clause_nos"]:
            # 指定条号不存在时不可用近似条文顶替，尤其是追问中的新条号。
            return [], p

        pool_n = max(k * 5, 30)
        allow = self.allowed(p)
        if allow == set():
            return [], p
        lists = [self.keyword(q, pool_n, allow)]
        if v := self.vector(q, pool_n, allow):
            lists.append(v)

        fused = rrf(lists)
        if rerank and len(fused) > k:
            fused = self._rerank(q, fused, k * 2)
        return self._post(fused, p, k), p

    def _post(self, hits: list[Hit], p: dict, k: int) -> list[Hit]:
        seen, out = set(), []
        for h in hits:
            key = (h.chunk["std_code"], h.chunk["clause_no"])
            if key in seen:
                continue
            if p["std_codes"] and not any(
                code_matches(h.chunk["std_code"], c) for c in p["std_codes"]
            ):
                continue
            if p["mandatory_only"] and not h.chunk["is_mandatory"]:
                continue
            seen.add(key)
            out.append(h)
        if p["wants_table"]:
            # 稳定排序：只把带表的往前提，不打乱同类之间的相对次序
            out.sort(key=lambda h: 0 if h.chunk["tables"] else 1)
        return out[:k]

    def _rerank(self, q: str, hits: list[Hit], k: int) -> list[Hit]:
        try:
            rr = self._rr()
            docs = [self._doc_text(h.chunk)[:1500] for h in hits]
            ranked = rr.rerank(q, docs, k)
        except (providers.ProviderUnavailable, Exception) as exc:  # noqa: BLE001
            if not isinstance(exc, providers.ProviderUnavailable):
                print(f"  ! 重排不可用，用融合排序：{exc}")
            return hits
        return [Hit(hits[i].chunk, s, hits[i].route + "+重排") for i, s in ranked]

    # -- 惰性初始化 --------------------------------------------------------

    def _collection(self):
        if self._coll is None:
            if not Path(self.chroma_dir).exists():
                return None
            import chromadb

            client = chromadb.PersistentClient(path=self.chroma_dir)
            try:
                self._coll = client.get_collection(COLLECTION)
            except Exception:  # noqa: BLE001 - 没建过索引，正常情况
                return None
        return self._coll

    def _embed(self):
        if self._embedder is None:
            self._embedder = providers.get_embedder()
        return self._embedder

    def _rr(self):
        if self._reranker is None:
            self._reranker = providers.get_reranker()
        return self._reranker


def rrf(lists: list[list[Hit]], k: int = 60) -> list[Hit]:
    """Reciprocal Rank Fusion：只看名次不看分数。

    BM25 的分数和向量余弦不在一个量纲上，直接加权求和需要调系数且不稳。
    RRF 回避了这个问题，代价是丢掉「某一路极其确信」的信息——对查规范可以接受。
    """
    score: dict[tuple[str, str], float] = {}
    keep: dict[tuple[str, str], Hit] = {}
    for lst in lists:
        for rank, h in enumerate(lst):
            key = (h.chunk["std_code"], h.chunk["clause_no"])
            score[key] = score.get(key, 0.0) + 1.0 / (k + rank + 1)
            if key not in keep or h.route == "向量":
                keep[key] = h
    ordered = sorted(score.items(), key=lambda kv: -kv[1])
    out = []
    for key, s in ordered:
        h = keep[key]
        routes = {
            hit.route
            for results in lists
            for hit in results
            if (hit.chunk["std_code"], hit.chunk["clause_no"]) == key
        }
        out.append(Hit(h.chunk, s, "+".join(sorted(routes))))
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def show(hits: list[Hit], p: dict) -> None:
    if not hits:
        print("  未检索到相关条文")
        return
    tags = []
    if p["std_codes"]:
        tags.append("限定规范 " + "/".join(p["std_codes"]))
    if p["clause_nos"]:
        tags.append("条号 " + "/".join(p["clause_nos"]))
    if p["mandatory_only"]:
        tags.append("仅强条")
    if tags:
        print(f"  〔{' · '.join(tags)}〕")
    for i, h in enumerate(hits, 1):
        c = h.chunk
        mark = " 【强条】" if c["is_mandatory"] else ""
        print(f"\n{i}. {h.cite()}{mark}  {c['std_name']}")
        print(f"   {c['chapter_path']}   ({h.route} {h.score:.4f})")
        body = c["text"].replace("\n", " ")
        print(f"   {body[:200]}{'…' if len(body) > 200 else ''}")
        if c["tables"]:
            print(f"   ＋附表 {len(c['tables'])} 个")
        # 提醒排在最后一行、顶格标注——它是「别照着这条直接做」的意思，
        # 混在正文里会被一眼扫过去。
        if c.get("caveat"):
            print(f"   ⚠ {c['caveat']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="规范条文检索")
    ap.add_argument("query", nargs="*", help="查询语句")
    ap.add_argument("-k", type=int, default=8)
    ap.add_argument("--chunks", default=str(paths.CHUNKS))
    ap.add_argument("--no-rerank", action="store_true")
    ap.add_argument("--repl", action="store_true", help="连续提问")
    args = ap.parse_args(argv)

    jieba.setLogLevel(60)
    print("装载语料…")
    r = Retriever(args.chunks)
    caps = providers.available()
    vec = "已建" if Path(CHROMA_DIR).exists() else "未建"
    print(
        f"检索池 {len(r.pool)} 条 · 向量索引{vec} · "
        f"embedding {'✓' if caps['embed'] else '✗'} · "
        f"rerank {'✓' if caps['rerank'] else '✗'}"
    )
    if not caps["embed"]:
        print("（无 API key，当前为纯 BM25 关键词检索）")

    def run(q: str) -> None:
        hits, p = r.search(q, args.k, rerank=not args.no_rerank)
        show(hits, p)

    if args.query:
        run(" ".join(args.query))
    if args.repl or not args.query:
        print("\n输入问题，空行退出")
        while True:
            try:
                q = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q:
                break
            run(q)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
