"""量一下 OCR 慢在哪，以及换个识别模型值不值。

一页 10 秒、剩下一万两千页，就是 37 小时。动手调参之前先测。

MinerU 1.3 的 lang='ch' 默认用 ch_PP-OCRv4_rec_server_doc——识别模型里最大的那个。
规范是印刷体、字距规整、对比度高，未必需要这么重的模型。
但「未必」不能当依据：**识别错一个数字就是错一条限值**，所以这里同时打出
速度和原文，让人眼看着定，而不是我替你定。

    python -m tools.ocrbench <pdf> [页数]
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MINERU_TOOLS_CONFIG_JSON", str(ROOT / "conf" / "magic-pdf.json"))
os.environ.setdefault("LOGURU_LEVEL", "ERROR")

# 要比的几个识别模型。det 三家共用，只换 rec。
RECS = [
    ("ch_PP-OCRv4_rec_server_doc_infer.pth", "ppocrv4_doc_dict.txt", "v4 server doc（当前默认）"),
    ("ch_PP-OCRv5_rec_server_infer.pth", "ppocrv5_dict.txt", "v5 server"),
    ("ch_PP-OCRv5_rec_infer.pth", "ppocrv5_dict.txt", "v5 mobile"),
]


def render(pdf: Path, n: int) -> list:
    import fitz
    import numpy as np

    doc = fitz.open(pdf)
    out = []
    # 跳过封面目次，取中间的正文页——那才是真要 OCR 的版面
    start = min(len(doc) // 3, max(0, len(doc) - n))
    for i in range(start, min(start + n, len(doc))):
        pm = doc[i].get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72), alpha=False)
        img = np.frombuffer(pm.samples, dtype=np.uint8).reshape(pm.h, pm.w, 3)
        out.append(img[:, :, ::-1].copy())  # RGB -> BGR
    doc.close()
    return out


def build(rec: str, dic: str):
    """照 MinerU 自己的方式建一个 OCR，只把 rec 换掉。"""
    import importlib

    pp = importlib.import_module(
        "magic_pdf.model.sub_modules.ocr.paddleocr2pytorch.pytorch_paddle")
    from magic_pdf.libs.config_reader import get_local_models_dir

    det_dir = os.path.join(get_local_models_dir(), "OCR", "paddleocr_torch")
    orig = pp.get_model_params

    def patched(lang, config):
        det, _, _ = orig(lang, config)
        # 上游 models_config 里 ch 的 det 指向一个没下发的 v3 文件，拿在手的顶上
        if not os.path.exists(os.path.join(det_dir, det)):
            for alt in ("ch_PP-OCRv5_det_infer.pth", "ch_PP-OCRv4_det_infer.pth"):
                if os.path.exists(os.path.join(det_dir, alt)):
                    det = alt
                    break
        return det, rec, dic

    pp.get_model_params = patched
    return pp.PytorchPaddleOCR(det_db_box_thresh=0.3, use_dilation=True,
                               det_db_unclip_ratio=1.8)


def run(ocr, imgs: list) -> tuple[float, list[str]]:
    ocr.ocr(imgs[0])  # 预热：第一次要编 kernel，算进去就不是稳态
    t0 = time.perf_counter()
    texts: list[str] = []
    for im in imgs:
        res = ocr.ocr(im)
        for line in (res[0] or []) if res else []:
            texts.append(line[1][0])
    return (time.perf_counter() - t0) / len(imgs), texts


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    pdf = Path(argv[0])
    n = int(argv[1]) if len(argv) > 1 else 4
    if not pdf.exists():
        print(f"找不到 {pdf}", file=sys.stderr)
        return 1

    imgs = render(pdf, n)
    from magic_pdf.libs.config_reader import get_device, get_local_models_dir

    print(f"{pdf.name}\n{n} 页 · 200dpi · {imgs[0].shape[1]}×{imgs[0].shape[0]} · device={get_device()}\n")

    mdir = Path(get_local_models_dir()) / "OCR" / "paddleocr_torch"
    results = []
    for rec, dic, label in RECS:
        if not (mdir / rec).exists():
            print(f"{label:<24s} 模型不在本地，跳过")
            continue
        sec, texts = run(build(rec, dic), imgs)
        results.append((label, sec, texts))
        print(f"{label:<24s} {sec:6.2f} s/页   识别 {len(texts)} 行")

    if len(results) < 2:
        return 0

    base = results[0]
    print(f"\n相对「{base[0]}」：")
    for label, sec, _ in results[1:]:
        print(f"  {label:<22s} 快 {base[1] / sec:.2f}×")

    # 正确性要人眼看：只打不一致的行，一致的没必要占屏
    print("\n" + "=" * 72)
    print("识别结果不一致的行（左＝当前默认，右＝候选）。数字和单位要逐个核。")
    for label, _, texts in results[1:]:
        print(f"\n--- {label} ---")
        diff = 0
        for a, b in zip(base[2], texts):
            if a.replace(" ", "") != b.replace(" ", ""):
                diff += 1
                if diff <= 25:
                    print(f"  {a}\n  {b}\n")
        same = len(base[2]) - diff
        print(f"  共 {len(base[2])} 行，{same} 行一致，{diff} 行不一致"
              + ("（只列了前 25 条）" if diff > 25 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
