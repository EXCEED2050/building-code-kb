"""把检索到的条文交给大模型作答，并强制标注出处。

这一层的全部价值在于**可核对**。同事拿去做设计的东西，必须能一路回溯到
原书的页码。所以：

  - 每条结论后面必须跟 [规范号 第X.X.X条 P页码]；
  - 没检索到就说没检索到，不许用常识补；
  - 强条要显著标出——强条是必须执行的，与一般条文性质不同；
  - 原文照抄，不改写数值。规范里「不应小于 1.10m」改成「大约 1.1 米」就是事故。

模型只经 providers 调用，不在这里直接连任何服务——信息中心尚未就企标
（DEP-*）能否出公网给出结论，换成内网模型时这一层不需要改。
"""

from __future__ import annotations

import argparse

from . import providers
from .retrieve import Hit, Retriever

SYSTEM = """你是建筑设计规范查询助手，服务于工程设计人员。你的回答会被用于实际工程设计，错误会造成安全后果。

铁律：
1. 只依据【给定条文】作答。给定条文里没有的内容，一律回答「未检索到相关条文」，不得用常识、记忆或推理补充。
2. 每一条结论后面必须标注出处，格式：[规范号 第X.X.X条 PN]。出处只能来自给定条文的标注，不得自己编造条号或页码。
3. 引用数值、限值、比例时必须与原文逐字一致，不得换算、取整或改写单位。
4. 标有【强制性条文】的条文，回答时必须显著说明「该条为强制性条文，必须严格执行」。
5. 多本规范对同一问题有不同规定时，全部列出并注明各自出处，不要替用户判断哪条适用——适用范围由设计人员依工程条件判定。
6. 条文本身写的是「应」「宜」「不应」「不得」时，原样保留，不要改成「必须」「建议」之类。
7. 带【引用提醒】的条文，必须把提醒原样转达给用户，并说明该条不能直接照用。

回答格式：先直接回答问题，再逐条列出依据。不要写开场白和总结。"""

USER_TMPL = """【给定条文】
{context}

【问题】
{question}"""


def format_context(hits: list[Hit]) -> str:
    blocks = []
    for h in hits:
        c = h.chunk
        head = f"{h.cite()} {c['std_name']}"
        if c["is_mandatory"]:
            head += " 【强制性条文】"
        elif c["mandatory_basis"]:
            head += f" （强条判定：{c['mandatory_basis']}）"
        body = [head]
        if c["chapter_path"]:
            body.append(f"章节：{c['chapter_path']}")
        body.append(c["text"])
        body.extend(c["tables"])
        if c.get("caveat"):
            body.append(f"【引用提醒】{c['caveat']}")
        blocks.append("\n".join(body))
    return "\n\n---\n\n".join(blocks)


def answer(r: Retriever, question: str, k: int = 8):
    """产出 (检索结果, 流式回答生成器)。检索不到就不调模型。"""
    hits, _ = r.search(question, k)
    if not hits:
        def nothing():
            yield "未检索到相关条文。可以试试换个说法，或直接给出规范号和条号。"
        return hits, nothing()

    llm = providers.get_llm()
    return hits, llm.stream(
        SYSTEM, USER_TMPL.format(context=format_context(hits), question=question)
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="规范问答")
    ap.add_argument("question", nargs="*")
    ap.add_argument("-k", type=int, default=8)
    ap.add_argument("--chunks", default="data/chunks")
    ap.add_argument("--show-context", action="store_true", help="同时打印喂给模型的条文")
    args = ap.parse_args(argv)

    import jieba

    jieba.setLogLevel(60)
    r = Retriever(args.chunks)

    def run(q: str) -> None:
        try:
            hits, stream = answer(r, q, args.k)
        except providers.ProviderUnavailable as exc:
            print(f"\n模型不可用：\n{exc}\n\n仅检索结果：")
            hits, _ = r.search(q, args.k)
            for h in hits:
                print(f"  {h.cite()} {h.chunk['text'][:80]}")
            return
        if args.show_context:
            print("\n" + format_context(hits) + "\n" + "=" * 60)
        for piece in stream:
            print(piece, end="", flush=True)
        print()
        srcs = " ".join(h.cite() for h in hits)
        print(f"\n— 本次检索到 {len(hits)} 条：{srcs}")

    if args.question:
        run(" ".join(args.question))
        return 0
    print("输入问题，空行退出")
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
