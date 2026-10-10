"""把「OCR 已经跑完、但还没进 data/chunks」的书补切进来。

每跑完一批 OCR 就该切一次，而不是等全部跑完——一档头 6 本切出来就暴露了
三个切条 bug（见 PLAN.md 3.3b）。越早切，错得越便宜。

    python -m tools.chunk_new              # 看一眼要切哪些，不动手
    python -m tools.chunk_new --run        # 真切
    python -m tools.chunk_new --run --wait-pid 12413   # 等 OCR 跑完再切

**只切 OCR 源。** 范围已经收窄到 `建筑规范/标准库/`（PLAN.md 3.1e），
而 `src.batch` 在一本书同时有 Word 源时会优先走 Word 路径——那些 Word 文件在
`建筑规范/word` 里，属于范围外。所以这里先剔掉有 Word 源的书再交给 batch，
否则范围收窄等于白做，而且是悄悄失效的那种白做。
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import subprocess
import sys
import time
from pathlib import Path

from src.batch import index_mineru, index_word_files, match, norm

KB = Path(__file__).resolve().parents[1]
ROOT = KB.parent


def pending(catalog: Path, ocr_dir: Path, word_dir: Path, chunks: Path) -> tuple[list, list]:
    """返回 (该切的, 因为有 Word 源而跳过的)。两个都要报出来，不能只报好消息。"""
    rows = list(csv.DictReader(catalog.open(encoding="utf-8-sig")))
    ocrs = index_mineru(ocr_dir)
    words = index_word_files(word_dir) if word_dir.exists() else {}
    done = {norm(Path(f).stem.replace("_", " ")) for f in glob.glob(str(chunks / "*.jsonl"))}

    todo, skipped, seen = [], [], set()
    for r in rows:
        code = (r["std_code"] or "").strip()
        if not code or norm(code) in seen:
            continue
        seen.add(norm(code))
        if match(code, ocrs) is None or norm(code) in done:
            continue
        if match(code, words) is not None:
            skipped.append((code, r["std_name"]))   # 有 Word 源，交给 batch 会走范围外的路
        else:
            todo.append((code, r["std_name"]))
    return todo, skipped


def wait_for(pid: int, limit_s: int) -> bool:
    """等某个进程退出。返回是否等到了（False = 超时，OCR 可能又卡住了）。"""
    t0 = time.time()
    while time.time() - t0 < limit_s:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return True
        time.sleep(20)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="补切已 OCR 完成但未入库的规范")
    ap.add_argument("--catalog", default=str(KB / "data" / "catalog.csv"))
    ap.add_argument("--ocr-dir", default=str(KB / "data" / "ocr"))
    ap.add_argument("--word-dir", default=str(ROOT / "建筑规范" / "word"))
    ap.add_argument("--mineru-dir", default=str(ROOT / "MinerU"))
    ap.add_argument("--chunks", default=str(KB / "data" / "chunks"))
    ap.add_argument("--run", action="store_true", help="真的切；不加只打印")
    ap.add_argument("--wait-pid", type=int, default=0, help="先等这个进程退出")
    ap.add_argument("--wait-limit", type=int, default=4 * 3600, help="最多等多少秒")
    args = ap.parse_args(argv)

    if args.wait_pid:
        print(f"等 pid {args.wait_pid} 退出（最多 {args.wait_limit / 3600:.1f} 小时）…", flush=True)
        if not wait_for(args.wait_pid, args.wait_limit):
            print(f"! 等到超时，pid {args.wait_pid} 还活着。"
                  f"OCR 可能又卡在换页上了，先去看它，别急着切。")
            return 2
        print("OCR 进程已退出。\n", flush=True)

    todo, skipped = pending(Path(args.catalog), Path(args.ocr_dir),
                            Path(args.word_dir), Path(args.chunks))
    if skipped:
        print(f"跳过 {len(skipped)} 本（有 Word 源，走 batch 会用到范围外文件）：")
        for c, n in skipped:
            print(f"   {c:<20} {n[:40]}")
        print()
    if not todo:
        print("没有要补切的书。")
        return 0

    print(f"待补切 {len(todo)} 本：")
    for c, n in todo:
        print(f"   {c:<20} {n[:40]}")
    if not args.run:
        print("\n（没加 --run，只是看看）")
        return 0

    cmd = [sys.executable, "-m", "src.batch",
           "--catalog", args.catalog, "--word-dir", args.word_dir,
           "--mineru-dir", args.mineru_dir, "--ocr-dir", args.ocr_dir,
           "--out", args.chunks, "--only", ",".join(c for c, _ in todo)]
    print("\n" + " ".join(cmd) + "\n", flush=True)
    return subprocess.call(cmd, cwd=KB)


if __name__ == "__main__":
    raise SystemExit(main())
