"""规范问答的 H5 后端。

走普通 H5 而不是小程序，是为了绕开「深度合成—AI问答」类目：
小程序上架要算法备案 + 安全评估，H5 挂在已备案域名下即可。
详见 PLAN.md 阶段 2。

两条铁律在这一层落地：

  1. 出网调用一律经 src.providers。这个文件不认识 DeepSeek，也不认识硅基流动。
     信息中心若判定企标（DEP-*）不能出公网，换成内网模型时这里一行都不用改。

  2. 没有模型也要能用。没配 API key 时问答关掉，但检索照常——
     BM25 + 条号直达已经能回答「GB 50016 第5.5.17条写了啥」这类问题，
     而这占了同事日常查询的一大半。宁可少个功能，不可给个空壳。

用法:
    export KB_PASSCODE='……'
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from src import answer as answer_mod
from src import providers, source_pdf
from src.retrieve import Retriever

from . import auth

STATIC = Path(__file__).resolve().parent / "static"
CHUNKS = os.environ.get("KB_CHUNKS", "data/chunks")
PAGE_CACHE = Path(os.environ.get("KB_PAGE_CACHE", "data/cache/pages"))
PAGE_ZOOM = 1.6   # 72 dpi × 1.6 ≈ 115 dpi：手机上看清五号字够了，灰度 JPEG 一页 100 多 KB

app = FastAPI(title="建筑规范知识库", docs_url=None, redoc_url=None)

_r: Retriever | None = None


@app.on_event("startup")
def _passcode_now() -> None:
    # 启动时就取一次口令：本地开发时口令立刻打在日志里，不用先登录失败一次；
    # 正式部署忘设 KB_PASSCODE 时直接起不来，而不是等第一个同事扫码才报错
    auth.passcode()


def retriever() -> Retriever:
    """首次用到时才装载语料，装一次留着。

    语料装载要几秒钟，放在启动阶段会让健康检查在这段时间里全部失败；
    放在这里，进程起来就能应答，第一个提问的人多等几秒。
    """
    global _r
    if _r is None:
        import jieba

        jieba.setLogLevel(60)
        _r = Retriever(CHUNKS)
    return _r


def guard(token: str | None) -> None:
    if not auth.valid(token):
        raise HTTPException(401, "请先输入访问口令")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/api/login")
async def login(req: Request) -> JSONResponse:
    body = await req.json()
    if not auth.check(str(body.get("passcode", ""))):
        # 不区分「没输」和「输错了」，也不回显输入内容
        raise HTTPException(401, "口令不对")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        auth.COOKIE,
        auth.issue(),
        max_age=auth.TTL,
        httponly=True,          # JS 读不到，页面上有 XSS 也偷不走
        samesite="lax",         # 别的站点发起的请求不带上它
        secure=bool(os.environ.get("KB_HTTPS")),
    )
    return resp


@app.post("/api/logout")
def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/api/status")
def status(kb_session: str | None = Cookie(None)) -> dict:
    """页面用它决定显示什么——尤其是「问答是否可用」要如实告诉用户。"""
    authed = auth.valid(kb_session)
    out: dict = {"authed": authed}
    if authed:
        r = retriever()
        cap = providers.available()
        out |= {
            "pool": len(r.pool),
            "books": len({c["std_code"] for c in r.pool}),
            "llm": cap["llm"],
            "vector": bool(getattr(r, "col", None)) and cap["embed"],
            "rerank": cap["rerank"],
        }
    return out


def _brief(h) -> dict:
    """送给前端的一条命中。字段名保持和 chunk 一致，省得两边对不上。"""
    c = h.chunk
    return {
        "id": c["chunk_id"],
        "std_code": c["std_code"],
        "std_name": c["std_name"],
        "clause_no": c["clause_no"],
        "chapter_path": c["chapter_path"],
        "page": (c["page_idx"] + 1) if c["page_idx"] is not None else None,
        "text": c["text"],
        "tables": c["tables"],
        "is_mandatory": c["is_mandatory"],
        "mandatory_basis": c["mandatory_basis"],
        "caveat": c.get("caveat", ""),
        "status": c.get("status", ""),
        "section_type": c.get("section_type", ""),   # 「修订」要在卡片上标出来
        "route": h.route,
    }


_books: list[dict] | None = None


def books_payload() -> dict:
    """在库清单，按规范号聚合。语料不变清单就不变，算一次留着。"""
    global _books
    if _books is None:
        agg: dict[str, dict] = {}
        for c in retriever().pool:
            b = agg.setdefault(c["std_code"], {
                "std_code": c["std_code"], "std_name": c["std_name"], "n": 0, "mandatory": 0,
            })
            b["n"] += 1
            b["mandatory"] += int(bool(c["is_mandatory"]))
        _books = sorted(agg.values(), key=lambda b: b["std_code"])
    return {"books": _books}


@app.get("/api/books")
def books(kb_session: str | None = Cookie(None)) -> dict:
    """同事的第一个问题往往是「有没有 GB 50016」——让页面自己能答。"""
    guard(kb_session)
    return books_payload()


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/ask")
def ask(
    q: str, k: int = 8, llm: int = 1, kb_session: str | None = Cookie(None)
) -> StreamingResponse:
    """先把条文推给前端，再流式推模型的话。

    顺序是有意的：条文是这套东西的本体，模型只是帮着读。
    先出条文，同事在模型还没开口时就已经能看到出处并自己判断；
    模型挂了、超时了，页面上留下的仍是一份带页码的条文清单，不是一片空白。

    `llm=0` 只要条文不要作答：页面上「AI 解读」关掉、或点「多显示几条」时用。
    查条号的人本来就不需要模型开口，省一次调用也省几秒。
    """
    guard(kb_session)
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "问题是空的")
    k = max(1, min(k, 20))

    def gen():
        try:
            r = retriever()
            hits, parsed = r.search(q, k)
            yield _sse("hits", {"hits": [_brief(h) for h in hits], "parsed": parsed})
            if not hits or not llm:
                yield _sse("done", {"answered": False})
                return
            try:
                model = providers.get_llm()
            except providers.ProviderUnavailable as exc:
                yield _sse("nollm", {"why": str(exc)})
                yield _sse("done", {"answered": False})
                return
            for piece in model.stream(
                answer_mod.SYSTEM,
                answer_mod.USER_TMPL.format(
                    context=answer_mod.format_context(hits), question=q
                ),
            ):
                yield _sse("delta", piece)
            yield _sse("done", {"answered": True})
        except Exception as exc:  # noqa: BLE001 - 连接已经开了，只能把错误也走 SSE 送出去
            yield _sse("error", {"msg": f"{type(exc).__name__}: {exc}"[:200]})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/chunk/{chunk_id}")
def chunk(chunk_id: str, kb_session: str | None = Cookie(None)) -> dict:
    """按 id 取整条，供前端展开看全文和表格。"""
    guard(kb_session)
    for c in retriever().pool:
        if c["chunk_id"] == chunk_id:
            return c
    raise HTTPException(404, "没有这一条")


# -- 看原文 ------------------------------------------------------------------
#
# 条文卡上的页码要能一点就翻到原件那一页。手机上的 PDF 阅读器不认 #page=N，
# 微信内置浏览器更是直接把整本几十兆下下来，所以把那一页渲成图片发过去——
# 图片是哪儿都能开的。整本 PDF 另给一个链接，电脑上用浏览器自带阅读器开，#page= 能翻到。

_sources: dict[str, tuple[Path, int]] = {}


def _source(code: str) -> tuple[Path, int]:
    """(原件路径, 页数)。规范号只当 dict 的键用，不拼进任何路径。"""
    if code not in _sources:
        p = source_pdf.resolve(code)
        if p is None:
            raise HTTPException(404, "这本规范没有可用的 PDF 原件")
        import fitz

        with fitz.open(p) as d:
            _sources[code] = (p, d.page_count)
    return _sources[code]


def page_response(code: str, page: int) -> FileResponse:
    """原件第 page 页（从 1 数）的灰度 JPEG，渲一次就留在磁盘上。"""
    p, n = _source(code)
    if not 1 <= page <= n:
        raise HTTPException(404, f"没有第 {page} 页，全书 {n} 页")
    f = PAGE_CACHE / source_pdf.norm(code) / f"{page}.jpg"
    if not f.exists():
        import fitz
        from PIL import Image

        f.parent.mkdir(parents=True, exist_ok=True)
        with fitz.open(p) as d:
            pm = d[page - 1].get_pixmap(matrix=fitz.Matrix(PAGE_ZOOM, PAGE_ZOOM), colorspace=fitz.csGRAY)
            Image.frombytes("L", (pm.width, pm.height), pm.samples).save(f, "JPEG", quality=72)
    return FileResponse(
        f, media_type="image/jpeg",
        headers={
            "X-Page-Count": str(n),
            "X-Pdf-Mb": f"{p.stat().st_size / 2**20:.0f}",
            "Cache-Control": "private, max-age=86400",
        },
    )


def pdf_response(code: str) -> FileResponse:
    p, _ = _source(code)
    return FileResponse(p, media_type="application/pdf", filename=p.name,
                        content_disposition_type="inline")


@app.get("/api/page")
def page(code: str, page: int, kb_session: str | None = Cookie(None)) -> FileResponse:
    guard(kb_session)
    return page_response(code, page)


@app.get("/api/pdf")
def pdf(code: str, kb_session: str | None = Cookie(None)) -> FileResponse:
    guard(kb_session)
    return pdf_response(code)
