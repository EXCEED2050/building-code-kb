"""前端联调用的假后端：不要口令、不调模型、不出网。

    python -m tools.ui_mock            # 打开 http://127.0.0.1:8001

为什么单独写一个，而不是给 app.main 加个「演示模式」开关：正式入口里多一个
能绕过口令的开关，迟早会跟着代码一起上线。这个文件只绑回环地址，
正式代码里没有任何地方 import 它。

条文是真的——用 src.retrieve 的 BM25 检索本机语料，页面拿到的数据形状和正式
后端一模一样（直接复用 app.main 的 _brief / _sse / books_payload）。
「AI 解读」是照着命中拼出来的假话，一个字一个字慢慢吐，
专门用来看流式渲染、出处跳转和停止按钮。
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app import main as real

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
app = FastAPI(docs_url=None, redoc_url=None)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/api/login")
def login() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.post("/api/logout")
def logout() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/api/status")
def status() -> dict:
    r = real.retriever()
    return {
        "authed": True,
        "pool": len(r.pool),
        "books": len({c["std_code"] for c in r.pool}),
        "llm": True, "vector": False, "rerank": False,
    }


@app.get("/api/books")
def books() -> dict:
    return real.books_payload()


@app.get("/api/page")
def page(code: str, page: int) -> FileResponse:
    return real.page_response(code, page)


@app.get("/api/pdf")
def pdf(code: str) -> FileResponse:
    return real.pdf_response(code)


def _fake_answer(hits) -> str:
    """长得像 answer.SYSTEM 要求的格式：先答，再逐条列依据，每条带 [出处]。"""
    top = hits[0]
    c = top.chunk
    text = f"{c['text'][:90].strip()}…{top.cite()}\n\n"
    if c["is_mandatory"]:
        text += "**该条为强制性条文，必须严格执行。**\n\n"
    text += "依据：\n"
    for h in hits[:3]:
        text += f"- {h.chunk['text'][:40].strip()}… {h.cite()}\n"
    if any(h.chunk.get("caveat") for h in hits[:3]):
        text += "\n【引用提醒】上述条文中带提醒的不能直接照用，请核对原件。\n"
    text += "\n（这段是 tools/ui_mock.py 拼的假回答，只用来看页面。）"
    return text


@app.get("/api/ask")
def ask(q: str, k: int = 8, llm: int = 1) -> StreamingResponse:
    def gen():
        hits, parsed = real.retriever().search(q.strip(), max(1, min(k, 20)), rerank=False)
        yield real._sse("hits", {"hits": [real._brief(h) for h in hits], "parsed": parsed})
        if not hits or not llm:
            yield real._sse("done", {"answered": False})
            return
        text = _fake_answer(hits)
        for i in range(0, len(text), 3):
            yield real._sse("delta", text[i:i + 3])
            time.sleep(0.02)
        yield real._sse("done", {"answered": True})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    import uvicorn

    # 只听回环地址：这东西没有口令，不能让同网段的人摸到
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
