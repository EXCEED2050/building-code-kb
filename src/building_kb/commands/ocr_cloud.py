"""把 OCR 交给 MinerU 官方云端跑（一档继续留在本地，这份跑二、三档）。

为什么要有这个文件
------------------
本地 10 s/页 是量到头的数字：批大小、渲染分辨率、多进程并行、换轻量识别模型
四条路都试过，只有批大小那条有用（1.2 倍，已经用上），其余三条实测更慢或更差，
过程记在 PLAN.md 3.1b。二三档还剩一万多页，按 10 s/页 是 30 小时，
而且这台 16G 机器内存一紧速度就塌——ocr_t1.log 里第 6 本已经掉到 45.2 s/页。

云端返回的 zip 里有 `**_content_list.json`，跟本地产物同一个格式，
chunker.py 一行不用改，也不用重新验证切条逻辑。这是选它而不是选别家的主要原因。

出网说明（要审出网行为，看两个文件，这是第二个）
----------------------------------------------
第一个是 src/providers.py（向量、重排、作答）。没有把这里并进去，是因为形态
不一样：那边是 embed / rerank / chat 三个协议，这边是传文件加轮询，
硬并进去会把查询链路的代码搞脏。两个文件，都写着为什么出网、出什么。

发出去的内容：二三档队列是 GB 国标、JGJ 行标、SH 行标——书店买得到的公开出版物，
另有 3 个 Q/SH 企标共 54 页。DEP 企标那 13 本有文本层，早就切完了，不在队列里。
企标出公网已获准（2026-09-21 信息中心答复），所以这里不拦，
但每次提交前会把清单按类别打出来，谁按的回车谁看得见发了什么。

用法
----
    python -m building_kb.commands.ocr_cloud --list var/queues/ocr_t2.txt var/queues/ocr_t3.txt   # 提交并跟到完成
    python -m building_kb.commands.ocr_cloud --status        # 只看进度，不提交
    python -m building_kb.commands.ocr_cloud --fetch         # 只把已完成的拉回本地
    python -m building_kb.commands.ocr_cloud --list ... --submit-only   # 只提交，回头再取

可中断可续跑：进度记在 data/processed/ocr/_cloud/manifest.json，
任何时候 Ctrl-C，再跑一次接着来，已经在云端排队的不会重复提交。
"""

from __future__ import annotations

from building_kb import paths

import argparse
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path

import requests

from building_kb.ingest.ocr import group_parts, merge, preclean  # noqa: E402  需要先把根目录放进 path

BASE = "https://mineru.net/api/v4"

# 官方限制：单文件 ≤200 页 / ≤200MB，单次申请上传链接 ≤50 个。
# 留一点余量，省得边界上因为页码从 0 还是从 1 数而被拒。
SEG_PAGES = 195
MAX_BYTES = 195 * 1024 * 1024
BATCH_FILES = 50

CLOUD = paths.OCR_DIR / "_cloud"
MANIFEST = CLOUD / "manifest.json"

DONE, FAILED = "done", "failed"
LIVE = ("waiting-file", "pending", "running", "converting")


# --------------------------------------------------------------------------
# 令牌
# --------------------------------------------------------------------------

def _token() -> str:
    """从 .env 读 MINERU_TOKEN。

    复用 providers 那套读法，理由一样：密钥待在文件里，不进代码、不进聊天记录、
    不进 shell 历史。token 在 mineru.net 的「API 管理」页面自己建。
    """
    from building_kb import providers

    providers._load_env()
    v = os.environ.get("MINERU_TOKEN", "").strip()
    if not v:
        raise SystemExit(
            "没找到 MINERU_TOKEN。\n"
            "  1. 去 https://mineru.net 登录，在「API 管理」页面建一个 token\n"
            "  2. 在 .env 里加一行：MINERU_TOKEN=eyJ...\n"
            "（别用 export——会进 shell 历史）"
        )
    return v


def _headers(token: str) -> dict:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# 进度台账
# --------------------------------------------------------------------------

def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {"batches": {}, "segs": {}}


def save_manifest(m: dict) -> None:
    CLOUD.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")


def seg_id(name: str, i: int) -> str:
    """data_id 只允许 [A-Za-z0-9_.-]，而书名全是中文，所以用哈希。

    上传的文件名也用它——中文文件名过一遍对象存储再回来，
    编码上能出岔子的环节太多，没必要赌。本地这边靠 manifest 记着哈希对应哪本书。
    """
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:16] + f"-{i}"


# --------------------------------------------------------------------------
# 备料：拼本 → 去水印 → 按页数切段
# --------------------------------------------------------------------------

def prepare(name: str, parts: list[Path], out_root: Path, work: Path) -> list[dict]:
    """把一本书变成若干个可以直接上传的 PDF 段。产物已存在就返回空。

    去水印这步在本地做，不指望云端替我们扛：出版社那个整页绿印章会把压在
    下面的字整片吞掉（「建筑设计防火规范」→「建筑设计防规范」，整条 4.5.2 消失），
    这是输入被污染，不是模型不行，换谁来识别都一样。
    """
    import fitz

    if (out_root / name / "auto" / f"{name}_content_list.json").exists():
        return []

    pdf = merge(parts, work / f"{name}.pdf")
    pdf, _ = preclean(pdf, work / f"{name}.clean.pdf")

    with fitz.open(pdf) as doc:
        total = doc.page_count

    if total <= SEG_PAGES and pdf.stat().st_size <= MAX_BYTES:
        return [{"name": name, "seg": 0, "nsegs": 1, "page_offset": 0,
                 "pages": total, "file": str(pdf)}]

    # 超限的要切开。切完页码要能拼回去，所以每段记住自己从第几页起——
    # chunker 是按 page_idx 定位条文出处的，页码错了，引用就指错地方。
    step = SEG_PAGES
    while True:
        bounds = [(s, min(s + step, total)) for s in range(0, total, step)]
        segs, ok = [], True
        CLOUD.joinpath("parts").mkdir(parents=True, exist_ok=True)
        for i, (a, b) in enumerate(bounds):
            # 文件名带上页区间，而不是只带段号：字节数超限时下面会把 step 减半重切，
            # 只按段号缓存的话，第 0 段会把上一轮那个更长的文件直接复用，
            # 页数就跟 manifest 记的对不上了。
            dst = CLOUD / "parts" / f"{seg_id(name, i)}-{a}-{b}.pdf"
            if not dst.exists():
                with fitz.open(pdf) as src, fitz.open() as out:
                    out.insert_pdf(src, from_page=a, to_page=b - 1)
                    out.save(dst)
            if dst.stat().st_size > MAX_BYTES:
                ok = False
                break
            segs.append({"name": name, "seg": i, "nsegs": len(bounds),
                         "page_offset": a, "pages": b - a, "file": str(dst)})
        if ok:
            return segs
        # 页数没超但字节数超了（扫描件分辨率高），再对半切。
        # 报错要报刚刚失败的那个 step，不是下一轮要试的——否则日志里写着
        # 「切到 3 页还超」，实际失败的是 7 页那次，照着排查会找错方向。
        if step // 2 < 8:
            raise RuntimeError(
                f"{name}: 每段切到 {step} 页仍有超 {MAX_BYTES // 1024 // 1024}MB 的，先手工处理")
        step //= 2


# --------------------------------------------------------------------------
# 提交
# --------------------------------------------------------------------------

def submit(segs: list[dict], token: str, model: str, m: dict) -> None:
    """按 50 个一批申请上传链接、上传。上传完系统自动建解析任务，不用再调一次。"""
    for i in range(0, len(segs), BATCH_FILES):
        chunk = segs[i: i + BATCH_FILES]
        body = {
            "model_version": model,
            "language": "ch",
            "enable_table": True,
            "files": [
                {"name": f"{seg_id(s['name'], s['seg'])}.pdf",
                 "data_id": seg_id(s["name"], s["seg"]),
                 # 规范基本都是扫描件；有文本层的那批早就在本地走 txt 模式切完了，
                 # 不在这个队列里，所以这里直接要 OCR。
                 "is_ocr": True}
                for s in chunk
            ],
        }
        r = requests.post(f"{BASE}/file-urls/batch", headers=_headers(token),
                          json=body, timeout=60)
        r.raise_for_status()
        res = r.json()
        if res.get("code") != 0:
            raise RuntimeError(f"申请上传链接失败：{res.get('msg')}")

        batch_id = res["data"]["batch_id"]
        urls = res["data"]["file_urls"]

        # 先把 batch_id 和这一批的段落盘，再开始传。
        # 反过来写的话，传到第 30 个断网，batch_id 就丢了——
        # 那 29 个已经传上去的既查不到状态也取不回来，重跑还会再传一遍。
        # 宁可留下「记了但没传成」的记录：那种情况下面会标成 upload-failed，看得见。
        for s in chunk:
            sid = seg_id(s["name"], s["seg"])
            m["segs"][sid] = {k: s[k] for k in
                              ("name", "seg", "nsegs", "page_offset", "pages")}
            m["segs"][sid].update(state="waiting-file", zip=None, batch=batch_id)
        m["batches"][batch_id] = {"submitted": time.strftime("%Y-%m-%d %H:%M:%S"),
                                  "segs": [seg_id(s["name"], s["seg"]) for s in chunk]}
        save_manifest(m)

        for s, url in zip(chunk, urls):
            sid = seg_id(s["name"], s["seg"])
            try:
                with open(s["file"], "rb") as f:
                    # 官方明确说上传时不要设 Content-Type，设了签名对不上
                    requests.put(url, data=f, timeout=600).raise_for_status()
            except Exception as exc:  # noqa: BLE001 - 一段传失败不该连累整批
                m["segs"][sid]["state"] = FAILED
                m["segs"][sid]["err"] = f"上传失败：{type(exc).__name__}: {exc}"[:160]
                save_manifest(m)
                print(f"  ! {s['name']} 第{s['seg'] + 1}段 上传失败：{exc}"[:160], flush=True)
                continue
            print(f"  ↑ {s['name']}"
                  + (f"  第{s['seg'] + 1}/{s['nsegs']}段" if s["nsegs"] > 1 else "")
                  + f"  {s['pages']} 页", flush=True)
        save_manifest(m)
        print(f"  批次 {batch_id} 已提交（{len(chunk)} 段）", flush=True)


# --------------------------------------------------------------------------
# 轮询
# --------------------------------------------------------------------------

def poll(token: str, m: dict) -> None:
    """刷新所有还没完成的批次状态。"""
    pending = {b for b, v in m["batches"].items()
               if any(m["segs"].get(s, {}).get("state") not in (DONE, FAILED)
                      for s in v["segs"])}
    for b in pending:
        try:
            r = requests.get(f"{BASE}/extract-results/batch/{b}",
                             headers=_headers(token), timeout=60)
            r.raise_for_status()
            data = r.json().get("data", {})
        except Exception as exc:  # noqa: BLE001 - 查不到就下轮再查，别把整批中断
            print(f"  查询 {b} 失败（下轮重试）：{exc}", flush=True)
            continue
        for item in data.get("extract_result", []):
            sid = item.get("data_id") or Path(item.get("file_name", "")).stem
            if sid not in m["segs"]:
                continue
            s = m["segs"][sid]
            # 本地上传失败的段，云端只会一直报 waiting-file（文件从没到）。
            # 让它覆盖掉 FAILED，这段就又算「在跑」，主循环永远等不完。
            # 只看 err 不看 state：旧版已经把这类段覆盖成了 waiting-file 落了盘。
            # 重传成功时 submit 会整条重写这段的记录，err 随之清掉。
            if s.get("err", "").startswith("上传失败"):
                s["state"] = FAILED
                continue
            s["state"] = item.get("state", s["state"])
            if item.get("full_zip_url"):
                s["zip"] = item["full_zip_url"]
            if item.get("err_msg"):
                s["err"] = item["err_msg"]
    save_manifest(m)


# --------------------------------------------------------------------------
# 取回
# --------------------------------------------------------------------------

def _pick(zf: zipfile.ZipFile, suffix: str) -> str | None:
    for n in zf.namelist():
        if n.endswith(suffix) and not n.startswith("__MACOSX"):
            return n
    return None


def fetch(m: dict, out_root: Path) -> int:
    """把所有段都完成了的书拉回来，拼成跟本地产物一模一样的目录。"""
    books: dict[str, list[dict]] = {}
    for sid, s in m["segs"].items():
        books.setdefault(s["name"], []).append({**s, "sid": sid})

    got = 0
    for name, segs in sorted(books.items()):
        dst = out_root / name / "auto"
        if (dst / f"{name}_content_list.json").exists():
            continue
        segs.sort(key=lambda s: s["seg"])
        if len(segs) != segs[0]["nsegs"] or any(s["state"] != DONE or not s["zip"]
                                                for s in segs):
            continue

        merged: list[dict] = []
        md: list[str] = []
        (dst / "images").mkdir(parents=True, exist_ok=True)
        try:
            for s in segs:
                blob = requests.get(s["zip"], timeout=600)
                blob.raise_for_status()
                with zipfile.ZipFile(io.BytesIO(blob.content)) as zf:
                    cl = _pick(zf, "_content_list.json") or _pick(zf, "content_list.json")
                    if not cl:
                        raise RuntimeError("zip 里没有 content_list.json")
                    for blk in json.loads(zf.read(cl).decode("utf-8")):
                        # 切段时每段的页码都从 0 重新数，这里加回偏移量。
                        # 不加的话 500 页的书会有三段都自称第 1 页。
                        if isinstance(blk.get("page_idx"), int):
                            blk["page_idx"] += s["page_offset"]
                        merged.append(blk)
                    if mdname := _pick(zf, "full.md"):
                        md.append(zf.read(mdname).decode("utf-8"))
                    for n in zf.namelist():
                        if "/images/" in n and not n.endswith("/"):
                            (dst / "images" / Path(n).name).write_bytes(zf.read(n))
        except Exception as exc:  # noqa: BLE001 - 一本取失败不该连累其余
            print(f"  ! {name} 取回失败：{type(exc).__name__}: {exc}"[:160], flush=True)
            continue

        (dst / f"{name}_content_list.json").write_text(
            json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
        (dst / f"{name}.md").write_text("\n\n".join(md), encoding="utf-8")
        got += 1
        pages = sum(s["pages"] for s in segs)
        print(f"  ✓ {name}  {pages} 页 / {len(merged)} 块"
              + (f"（{len(segs)} 段拼合）" if len(segs) > 1 else ""), flush=True)
    return got


# --------------------------------------------------------------------------
# 台面
# --------------------------------------------------------------------------

def status(m: dict) -> None:
    c = Counter(s["state"] for s in m["segs"].values())
    pages = Counter()
    for s in m["segs"].values():
        pages[s["state"]] += s["pages"]
    if not c:
        print("还没提交过任何东西。")
        return
    print(f"{'状态':<14}{'段数':>6}{'页数':>8}")
    for k in (*LIVE, DONE, FAILED):
        if c[k]:
            print(f"{k:<14}{c[k]:>6}{pages[k]:>8}")
    for sid, s in m["segs"].items():
        if s["state"] == FAILED:
            print(f"  ! {s['name']} 第{s['seg'] + 1}段：{s.get('err', '(无说明)')}")


def _preview(segs: list[dict]) -> None:
    """提交前把要发出去的东西按标准类别列一遍。

    企标出公网已经获准，所以这里不拦。但「获准」和「知道自己发了什么」是两回事，
    真发出去之前，人得先看见清单。
    """
    kind = Counter()
    for s in segs:
        n = s["name"]
        k = ("GB 国标" if n.startswith("GB") else
             "JGJ 行标" if n.startswith("JGJ") else
             "SH 行标" if n.startswith("SH") else
             "Q/SH 企标" if n.startswith(("Q/SH", "Q_SH")) else
             "DEP 企标" if n.startswith("DEP") else "其他")
        kind[k] += s["pages"]
    books = len({s["name"] for s in segs})
    total = sum(s["pages"] for s in segs)
    print(f"\n准备上传 {books} 本 / {len(segs)} 段 / {total} 页，按类别：")
    for k, v in kind.most_common():
        print(f"    {k:<12}{v:>6} 页")
    if total > 1000:
        print("  （每天 1000 页是最高优先级额度，超出的部分降优先级排队，不会被拒）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MinerU 云端批量 OCR，可中断可续跑")
    ap.add_argument("--list", nargs="*", default=[], help="清单文件，可给多个")
    ap.add_argument("--out", default=str(paths.OCR_DIR))
    ap.add_argument("--work", default=str(paths.OCR_DIR / "_merged"))
    ap.add_argument("--model", default="vlm", choices=("vlm", "pipeline"),
                    help="vlm 是他们较新的模型，扫描件上通常更稳")
    ap.add_argument("--limit", type=int, default=0, help="只提交前 N 本，先小批试水")
    ap.add_argument("--status", action="store_true", help="只看进度")
    ap.add_argument("--fetch", action="store_true", help="只取回已完成的")
    ap.add_argument("--submit-only", action="store_true", help="提交完就退出，不等")
    ap.add_argument("--interval", type=int, default=60, help="轮询间隔秒")
    ap.add_argument("--yes", action="store_true", help="不要提交前确认（无人值守时用）")
    args = ap.parse_args(argv)

    m = load_manifest()
    out_root = Path(args.out)

    if args.status:
        status(m)
        return 0

    token = _token()

    if args.fetch:
        poll(token, m)
        print(f"取回 {fetch(m, out_root)} 本")
        status(m)
        return 0

    # 1) 备料。已在云端排队的、已经有产物的，都不再提交。
    if args.list:
        paths: list[Path] = []
        for f in args.list:
            paths += [Path(l.strip()) for l in
                      Path(f).read_text(encoding="utf-8").splitlines()
                      if l.strip() and not l.startswith("#")]
        missing = [p for p in paths if not p.exists()]
        for p in missing[:5]:
            print(f"清单里这个路径不存在，跳过：{p}", file=sys.stderr)
        # 有段失败的书要重来，但只重传失败的那几段——已完成的段落在云端，
        # 合并时照样用得上；整本重传既费额度，又会让同一段在 manifest 里两处记账。
        failed = {sid for sid, s in m["segs"].items() if s["state"] == FAILED}
        retry = {m["segs"][sid]["name"] for sid in failed}
        queued = {s["name"] for s in m["segs"].values()} - retry
        todo = [(n, ps) for n, ps in group_parts([p for p in paths if p.exists()]).items()
                if n not in queued]
        if args.limit:
            todo = todo[: args.limit]

        segs: list[dict] = []
        print(f"备料中（{len(todo)} 本待查）…", flush=True)
        for name, parts in todo:
            try:
                got = prepare(name, parts, out_root, Path(args.work))
                if name in retry:
                    got = [s for s in got if seg_id(name, s["seg"]) in failed]
                    print(f"  重传 {name} 失败的 {len(got)} 段", flush=True)
                segs += got
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {name} 备料失败：{type(exc).__name__}: {exc}"[:160], flush=True)

        if segs:
            _preview(segs)
            if not args.yes and sys.stdin.isatty():
                if input("\n回车开始上传，Ctrl-C 取消 ").strip().lower() in ("n", "no"):
                    return 1
            submit(segs, token, args.model, m)
        else:
            print("没有需要新提交的——要么已有产物，要么已经在云端排队。")

    if args.submit_only:
        status(m)
        return 0

    # 2) 轮询到全部落地
    while True:
        poll(token, m)
        fetch(m, out_root)
        left = [s for s in m["segs"].values() if s["state"] in LIVE]
        if not left:
            break
        done = sum(1 for s in m["segs"].values() if s["state"] == DONE)
        print(f"  [{time.strftime('%H:%M:%S')}] 完成 {done} 段，"
              f"在跑/排队 {len(left)} 段（{sum(s['pages'] for s in left)} 页）", flush=True)
        time.sleep(args.interval)

    print()
    status(m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
