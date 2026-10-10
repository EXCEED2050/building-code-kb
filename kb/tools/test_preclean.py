"""preclean 流式改写的对照测试：旧写法 vs 新写法，比页数、页面尺寸、图像流字节、峰值内存。

用法: python tools/test_preclean.py            # 跑全部，约半分钟
      python tools/test_preclean.py old|new    # 子进程模式，单独测一种（main 自己会调）

临时文件放 data/ocr/_tmp_test/，跑完自动删。
峰值内存那一栏在这台 Mac 上看不出差别：假书全是白底，攒在内存里的整本图
被 macOS 的内存压缩器压没了，ru_maxrss 只量常驻。真扫描件有噪点压不掉，
差别就是 PLAN.md 3.1d 里算的那 2.3 GB。
"""
import io
import json
import resource
import subprocess
import sys
from pathlib import Path

import fitz
import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HERE = ROOT / "data/ocr/_tmp_test"
DPI = 400          # 故意用 400，验证「页面尺寸取原件 rect」这一点
NPAGES = 12
W_IN, H_IN = 8.27, 11.69


def make_scan(path: Path, stamped_pages: set[int]) -> None:
    """造一本 400dpi 的假扫描书：黑色文字条 + 红色印章圆。"""
    if path.exists():
        return
    w, h = round(W_IN * DPI), round(H_IN * DPI)
    doc = fitz.open()
    for i in range(NPAGES):
        im = Image.new("RGB", (w, h), (255, 255, 255))
        d = ImageDraw.Draw(im)
        for y in range(400, h - 400, 90):          # 文字行
            d.rectangle([300, y, w - 300, y + 30], fill=(10, 10, 10))
        if i in stamped_pages:                     # 红章，压住几行字
            cx, cy, r = w // 2, h // 3, 500
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(200, 20, 20), width=40)
            d.ellipse([cx - r + 150, cy - r + 150, cx + r - 150, cy + r - 150],
                      fill=(220, 40, 40))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        page = doc.new_page(width=W_IN * 72, height=H_IN * 72)
        page.insert_image(page.rect, stream=buf.getvalue())
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()


def preclean_old(src: Path, dst: Path):
    """改写前的 preclean，原样搬过来做对照（去掉了 .nostamp 部分，测试用不着）。"""
    from src.ocr import COLOR_SAT, KEEP_DARK, STAMP_RATIO, _page_dpi

    doc = fitz.open(src)
    pages, hit = [], 0
    try:
        for page in doc:
            pm = page.get_pixmap(dpi=_page_dpi(page))
            a = (np.frombuffer(pm.samples, np.uint8)
                 .reshape(pm.height, pm.width, pm.n)[:, :, :3].astype(np.int16))
            sat = a.max(2) - a.min(2)
            mask = (sat > COLOR_SAT) & (a.mean(2) > KEEP_DARK)
            if mask.mean() > STAMP_RATIO:
                hit += 1
            g = a.mean(2)
            g[mask] = 255
            pages.append(Image.fromarray(g.astype("uint8"), "L"))
    finally:
        doc.close()
    if hit / max(1, len(pages)) < 0.1:
        return src, 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(dst, "PDF", save_all=True, append_images=pages[1:], resolution=300)
    return dst, hit


def describe(pdf: Path) -> dict:
    doc = fitz.open(pdf)
    rects, streams = [], []
    for page in doc:
        rects.append((round(page.rect.width, 2), round(page.rect.height, 2)))
        xrefs = [im[0] for im in page.get_images(full=True)]
        assert len(xrefs) == 1, (pdf, page.number, xrefs)
        streams.append(doc.extract_image(xrefs[0])["image"])
    doc.close()
    return {"n": len(rects), "rects": rects, "streams": streams}


def run_child(mode: str) -> None:
    src = HERE / "scan.pdf"
    dst = HERE / f"clean_{mode}.pdf"
    dst.unlink(missing_ok=True)
    if mode == "old":
        out, hit = preclean_old(src, dst)
    else:
        from src.ocr import preclean
        out, hit = preclean(src, dst)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # macOS: bytes
    print(json.dumps({"out": str(out), "hit": hit, "peak_mb": round(peak / 2**20)}))


def main() -> None:
    make_scan(HERE / "scan.pdf", set(range(NPAGES)))      # 全部盖章
    res = {}
    for mode in ("old", "new"):
        p = subprocess.run([sys.executable, __file__, mode], capture_output=True, text=True)
        if p.returncode:
            print(p.stderr)
            sys.exit(f"{mode} 子进程失败")
        res[mode] = json.loads(p.stdout.strip().splitlines()[-1])
        print(f"{mode}: hit={res[mode]['hit']}  峰值内存 {res[mode]['peak_mb']} MB")

    orig = describe(HERE / "scan.pdf")
    old = describe(HERE / "clean_old.pdf")
    new = describe(HERE / "clean_new.pdf")
    print(f"页数        原件 {orig['n']}  旧 {old['n']}  新 {new['n']}")
    print(f"页面尺寸(pt) 原件 {orig['rects'][0]}  旧 {old['rects'][0]}  新 {new['rects'][0]}")
    same = sum(a == b for a, b in zip(old["streams"], new["streams"]))
    print(f"图像流逐字节相同的页数: {same}/{NPAGES}")
    if same != NPAGES:
        a = np.asarray(Image.open(io.BytesIO(old["streams"][0])))
        b = np.asarray(Image.open(io.BytesIO(new["streams"][0])))
        print(f"  第 1 页像素: 形状 {a.shape} vs {b.shape}, 最大差 {np.abs(a.astype(int)-b).max()}")
    assert new["rects"] == orig["rects"], "新产物页面尺寸应等于原件"
    assert new["n"] == old["n"] == NPAGES
    assert res["old"]["hit"] == res["new"]["hit"] == NPAGES

    # 印章不足 10% 的书：新代码应返回原件、留 .nostamp、不写 clean.pdf
    from src.ocr import preclean
    make_scan(HERE / "scan_few.pdf", {0})
    dst = HERE / "clean_few.pdf"
    flag = dst.with_suffix(".nostamp")
    for f in (dst, flag):
        f.unlink(missing_ok=True)
    out, hit = preclean(HERE / "scan_few.pdf", dst)
    assert out == HERE / "scan_few.pdf" and hit == 0 and flag.exists() and not dst.exists(), (out, hit)
    out2, hit2 = preclean(HERE / "scan_few.pdf", dst)
    assert (out2, hit2) == (out, hit)
    print("少量盖章 → 返回原件 + .nostamp 标记：通过")
    print("全部通过")

    for f in HERE.iterdir():
        f.unlink()
    HERE.rmdir()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_child(sys.argv[1])
    else:
        main()
