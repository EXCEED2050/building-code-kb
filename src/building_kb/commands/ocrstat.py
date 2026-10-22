"""看 OCR 跑到哪了。

`building_kb.ingest.ocr` 的日志一行一本，但一本要跑十几分钟，光看日志不知道还剩多久。
这里拿队列和产物目录对一下账，算出剩余页数和预计完成时刻——
跨夜跑 38 小时的活，得能随时知道什么时候能收。

用法:
    python -m building_kb.commands.ocrstat                      # 看一档
    python -m building_kb.commands.ocrstat --list var/queues/ocr_t2.txt
    python -m building_kb.commands.ocrstat --watch              # 每 60 秒刷一次
"""

from __future__ import annotations

from building_kb import paths

import argparse
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from building_kb.ingest.ocr import group_parts


def snapshot(queue: Path, out: Path) -> dict:
    paths = [Path(l.strip()) for l in queue.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")]
    groups = group_parts(paths)
    import fitz

    done_books, todo_books, done_pages, todo_pages = [], [], 0, 0
    # 不排序，跟 building_kb.ingest.ocr 保持同一个顺序——否则「在跑」「接下来」报的是
    # 字母序第一本，而不是真正在跑的那本，越准的进度条越骗人。
    for name, parts in groups.items():
        n = 0
        for p in parts:
            try:
                with fitz.open(p) as d:
                    n += d.page_count
            except Exception:  # noqa: BLE001 - 打不开的文件台账里已经记过了
                pass
        if (out / name / "auto" / f"{name}_content_list.json").exists():
            done_books.append(name)
            done_pages += n
        else:
            todo_books.append((name, n))
            todo_pages += n
    return {"done": done_books, "todo": todo_books,
            "done_pages": done_pages, "todo_pages": todo_pages}


DONE_RE = re.compile(r"(\d+) 页 / (\d+(?:\.\d+)?)s")
SWAP_RE = re.compile(r"total = ([\d.]+)M\s+used = ([\d.]+)M")


def pressure() -> tuple[float, float]:
    """内存压力 (系统空闲百分比, 交换区已用 GB)。取不到就是 (-1, -1)。

    不要用「交换区已用/总量」当指标：macOS 会跟着需求伸缩交换文件，
    颠簸时是 21.3/22.5 GB（95%），缓过来是 6.0/7.2 GB（84%）——
    比例几乎没动，实际压力差了三倍多，拿它报警会一直误报。
    真正能区分的是系统空闲内存百分比（同两个时刻：39% vs 74%）。
    交换区已用的绝对值留着，是因为它直观：写 6 GB 比写 74% 更容易让人
    意识到「该关点东西了」。
    """
    import subprocess

    free, used = -1.0, -1.0
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True,
                             text=True, timeout=10).stdout
        if m := re.search(r"free percentage:\s*(\d+)%", out):
            free = float(m.group(1))
    except Exception:  # noqa: BLE001 - 看不到就不显示，不该影响进度查询
        pass
    try:
        out = subprocess.run(["sysctl", "vm.swapusage"], capture_output=True,
                             text=True, timeout=5).stdout
        if m := SWAP_RE.search(out):
            used = float(m.group(2)) / 1024
    except Exception:  # noqa: BLE001 - 同上
        pass
    return free, used


WINDOW = 5


def rate(log: Path) -> float:
    """从日志里已完成的行算实测均速，比拍脑袋的 10.5 s/页准。

    只取最近 WINDOW 本：机器状态会变——内存一紧，每页能从 13 秒掉到 58 秒，
    好转了又能回来。把全程平均进去，算出的完工时刻会一直拖着早已过去的坏时段，
    既不反映现在跑多快，也就没法拿来判断「要不要现在去腾点内存」。
    """
    if not log.exists():
        return 10.5
    hits = DONE_RE.findall(log.read_text(encoding="utf-8", errors="ignore"))[-WINDOW:]
    pages = sum(float(p) for p, _ in hits)
    secs = sum(float(s) for _, s in hits)
    return secs / pages if pages else 10.5


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="查看 OCR 进度")
    ap.add_argument("--list", default=str(paths.QUEUES / "ocr_t1.txt"))
    ap.add_argument("--out", default=str(paths.OCR_DIR))
    ap.add_argument("--log", default="")
    ap.add_argument("--watch", action="store_true", help="每 60 秒刷新一次")
    args = ap.parse_args(argv)

    queue = Path(args.list)
    log = Path(args.log) if args.log else (paths.VAR / "logs") / f"ocr_{queue.stem.split('_')[-1]}.log"

    while True:
        s = snapshot(queue, Path(args.out))
        total = len(s["done"]) + len(s["todo"])
        tp = s["done_pages"] + s["todo_pages"]
        r = rate(log)
        left = timedelta(seconds=int(s["todo_pages"] * r))
        pct = 100 * s["done_pages"] / tp if tp else 100
        print(f"\n{queue.name}  {len(s['done'])}/{total} 本 · "
              f"{s['done_pages']}/{tp} 页 · {pct:.0f}%")
        print(f"实测 {r:.1f} s/页 · 剩余 {s['todo_pages']} 页 ≈ {left} · "
              f"预计 {(datetime.now() + left):%m-%d %H:%M} 完成")
        if s["todo"]:
            # 正在跑的这本必须单独报，还要报「安静了多久」。
            # magic-pdf 的模型推理阶段一行日志都不打（tqdm 关掉了），
            # 一本 263 页的规范能静默一个多小时——不写这行，
            # 看日志的人会以为卡死了，然后把还差十分钟就跑完的活给杀了。
            name, pages = s["todo"][0]
            quiet = time.time() - log.stat().st_mtime if log.exists() else 0
            need = pages * r
            print(f"在跑：{name}（{pages}p，约 {timedelta(seconds=int(need))}）· "
                  f"日志已静默 {timedelta(seconds=int(quiet))}"
                  + ("   ← 超出该本预计用时" if quiet > need * 1.5 + 300 else "（推理阶段不打日志，正常）"))
            free, used = pressure()
            if 0 <= free < 50:
                print(f"内存吃紧：系统空闲 {free:.0f}%，交换区已用 {used:.1f} GB"
                      f"——慢下来多半是这个。先看 OCR 进程自己涨到多大"
                      f"（ps -p <pid> -o rss=），再考虑关应用")
            if len(s["todo"]) > 1:
                nxt = ", ".join(f"{n}({p}p)" for n, p in s["todo"][1:4])
                print(f"接下来：{nxt}")
        if not args.watch or not s["todo"]:
            return 0
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
