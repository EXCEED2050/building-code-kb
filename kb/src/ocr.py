"""把扫描件规范跑成 MinerU 格式的解析产物（本地跑，不出网、不花钱）。

为什么不直接用 `magic-pdf` 命令行：
  - PyTorch 2.6 起 torch.load 默认 weights_only=True，doclayout_yolo 的权重
    加载会直接抛 UnpicklingError。这里按类名放行，比全局 weights_only=False 窄。
  - 大文件被拆成「_部分1…_部分19」上传过，得先拼回一本再解析，
    否则章节跨文件断开，切条器看到的是 19 份残篇。
  - 140 本、1.4 万页不可能一口气跑完，必须可中断可续跑。

产物目录结构与 MinerU 在线版一致（`<规范名>/auto/<规范名>_content_list.json`），
这样 batch.py 那边不用区分来源。

用法:
    python -m src.ocr --list data/ocr_queue.txt          # 按清单跑
    python -m src.ocr --list data/ocr_queue.txt --limit 3
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

# 一本 28 页的规范能刷出上千行进度条，跑 140 本时日志就没法看了。
# 这几行必须赶在 import magic_pdf 之前生效（CONFIG_FILE_NAME 是导入时读的），
# 所以放在模块顶层。
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("LOGURU_LEVEL", "WARNING")
# 配置放进仓库而不是 ~/magic-pdf.json：解析参数（device-mode、要不要跑公式模型）
# 直接决定入库文本长什么样，属于工程的一部分，不该藏在某台机器的 home 目录里。
os.environ.setdefault(
    "MINERU_TOOLS_CONFIG_JSON", str(Path(__file__).resolve().parent.parent / "conf" / "magic-pdf.json"))

PART_RE = re.compile(r"[_\s-]*部分(\d+)$")


def _allow_globals() -> None:
    """权重来自 ModelScope 上 OpenDataLab 的官方仓库，按类名放行。"""
    import torch
    from doclayout_yolo.nn.tasks import YOLOv10DetectionModel

    torch.serialization.add_safe_globals([YOLOv10DetectionModel])


REC_BATCH = 32


def _patch_ocr_batch() -> None:
    """把文字识别的批大小从 6 提到 32。

    MinerU 给识别模型的默认批大小是 6，那是照着 CUDA 定的。
    在 MPS 上每次前向都要过一遍 GPU 调度，批越小，调度开销占比越高——
    一页规范有四五十行字，按 6 一批就是八九次调度，几乎全耗在来回上。

    实测（GB 50763 无障碍设计规范，6 页）：
        batch=6    识别 4.85 s/页，整页 12.12 s
        batch=32   识别 2.18 s/页，整页 10.11 s
    识别这一段快了一倍多，整体快 1.2 倍。

    改批大小会改变同批图片的补白宽度，理论上可能影响识别结果，所以逐字比对过：
    4616 字里两处不同，一处是分号全角半角，另一处 batch=32 反而是对的
    （「无障碍出入口」，batch=6 认成了「出人口」）。没有变差。
    """
    import magic_pdf.model.sub_modules.ocr.paddleocr2pytorch.pytorch_paddle as pp

    orig = pp.PytorchPaddleOCR.__init__

    def init(self, *a, **kw):
        kw.setdefault("rec_batch_num", REC_BATCH)
        orig(self, *a, **kw)

    pp.PytorchPaddleOCR.__init__ = init


def _patch_ocr_det() -> str:
    """magic-pdf 1.3.12 的 models_config.yml 还指着 ch_PP-OCRv3_det_infer.pth，
    但模型仓库早已把检测模型换成 v5，v3 那份下不下来了。

    检测网络的结构是从权重文件里读出来的（get_arch_config），
    PPLCNetV3 + RSEFPN 这套 1.3.12 的代码本来就支持，换文件就能跑。
    只在配置指定的文件确实不存在时才替换——哪天上游修好了，这里自动失效。
    """
    import importlib
    import os

    pp = importlib.import_module(
        "magic_pdf.model.sub_modules.ocr.paddleocr2pytorch.pytorch_paddle")
    from magic_pdf.libs.config_reader import get_local_models_dir

    det_dir = os.path.join(get_local_models_dir(), "OCR", "paddleocr_torch")
    orig = pp.get_model_params

    def patched(lang, config):
        det, rec, dict_file = orig(lang, config)
        if not os.path.exists(os.path.join(det_dir, det)):
            for alt in ("ch_PP-OCRv5_det_infer.pth", "ch_PP-OCRv4_det_infer.pth"):
                if os.path.exists(os.path.join(det_dir, alt)):
                    return alt, rec, dict_file
        return det, rec, dict_file

    pp.get_model_params = patched
    return det_dir


def _reclaim() -> None:
    """开下一本之前，把上一本的缓存还回去。

    模型权重要留着复用——重载一次一分多钟，几十本下来就是一小时。
    但中间产物不该留：torch 的分配器习惯把申请到的内存缓存起来不还给系统，
    PyMuPDF 的页缓存也一样。实测一个进程连跑 6 本，RSS 从 1.8G 爬到 3.2G；
    16G 的机器上这点增长足以把交换区顶满，然后每页从 13 秒掉到 58 秒——
    看起来像「卡住了」，其实是机器在颠簸。
    """
    import gc

    gc.collect()
    try:
        import torch

        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:  # noqa: BLE001 - 回收不成功也只是慢点，不该中断整批
        pass


def group_parts(paths: list[Path]) -> dict[str, list[Path]]:
    """把 `X_部分1.pdf … X_部分19.pdf` 归到一组，按部分号排序。"""
    g: dict[str, list[Path]] = {}
    for p in paths:
        g.setdefault(PART_RE.sub("", p.stem), []).append(p)
    for k, v in g.items():
        v.sort(key=lambda p: int(m.group(1)) if (m := PART_RE.search(p.stem)) else 0)
        # 整本和它的分册同时在库里时只要整本。不这样的话三份拼成一本，
        # 全书出现两遍：GB 50074 就被 OCR 成 384 页，后一遍被切条器当成
        # 「局部修订」，把正文 420 条全部判为已被取代。
        whole = [p for p in v if not PART_RE.search(p.stem)]
        if whole and len(whole) < len(v):
            g[k] = whole[:1]
    return g


def merge(parts: list[Path], dst: Path) -> Path:
    """多份合成一份。只有一份就原样用，不做无谓的复制。"""
    if len(parts) == 1:
        return parts[0]
    if dst.exists():
        return dst
    import fitz

    out = fitz.open()
    for p in parts:
        with fitz.open(p) as d:
            out.insert_pdf(d)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst)
    out.close()
    return dst


# --------------------------------------------------------------------------
# 去水印
#
# 出版社扫描件多半盖着一整页的绿色印章（「中国石化出版社 / CHINA PETROCHEMICAL
# PRESS」）。压在印章上的字会被 OCR 整片吞掉：同一本书里，没印章的那页
# 识别得一字不差，有印章的那页「建筑设计防火规范」变成「建筑设计防规范」、
# 「4.3」变成「1.3」、整条 4.5.2 直接消失。这不是模型不行，是输入被污染了。
#
# 印章是彩色的，正文是中性灰黑，于是按「饱和度高且不够暗」的像素涂白即可，
# 压着字的那部分笔画因为足够暗会保留下来。实测 17% 的页面像素被判为彩色，
# 处理后整页文字可读。
# --------------------------------------------------------------------------

COLOR_SAT = 30      # max(R,G,B)-min(R,G,B) 超过它就算「有颜色」
KEEP_DARK = 80      # 亮度低于它的不动——那是压在印章下面的字
STAMP_RATIO = 0.02  # 整页彩色像素超过 2% 才认为盖了章


def _page_dpi(page, default: int = 300) -> int:
    """按页面里那张扫描图的像素数反推扫描分辨率，别把 300dpi 的原件降采样。"""
    try:
        w = max(im[2] for im in page.get_images(full=True))
    except ValueError:
        return default
    pt = page.rect.width or 1
    return max(150, min(400, round(w / (pt / 72) / 50) * 50))


def preclean(src: Path, dst: Path) -> tuple[Path, int]:
    """有印章就重排一份干净的 PDF，没有就原样返回。返回 (要解析的文件, 处理页数)。

    逐页处理、逐页压成 JPEG 塞进新文档，任何时候内存里只有当前这一页的像素。
    原先的写法是把整本的 PIL 图像攒在一个 list 里最后一次性存盘：
    GB 50984 那本 161 页按 400dpi 渲，list 里就是 2.3 GB 未压缩灰度图，
    16G 的机器直接被顶进交换区——盖章的石化书之所以慢到 45 s/页，根子在这。
    JPEG 之后一页两三百 KB，整本几十 MB。

    输出的页面尺寸取原件的 page.rect，不再是「像素数 ÷ 300」。
    两者只在扫描分辨率不是 300 时才有区别（原写法会把 400dpi 的书放大三分之一），
    已处理过的两本都是 300dpi 原扫，产物一模一样。
    编码沿用 Pillow 的默认 JPEG 参数——它的 PDF 插件内部就是这么存的，
    逐字节比对过，同一页的图像流完全一致。
    """
    import io

    import fitz
    import numpy as np
    from PIL import Image

    if dst.exists():
        return dst, -1  # -1: 上次已处理过，续跑时不重复统计

    # 「这本没盖章」也要记下来，否则每次续跑都得重新论证一遍。
    # 判断结果只能靠把整本按扫描原分辨率渲一遍得出，上面那本要 34 分钟，
    # 渲完发现不用清理就直接扔掉，下次重启再来一遍。留个空标记文件省掉这半小时。
    flag = dst.with_suffix(".nostamp")
    if flag.exists():
        return src, 0

    doc = fitz.open(src)
    out = fitz.open()
    n = hit = 0
    try:
        for page in doc:
            pm = page.get_pixmap(dpi=_page_dpi(page))
            a = (np.frombuffer(pm.samples, np.uint8)
                 .reshape(pm.height, pm.width, pm.n)[:, :, :3].astype(np.int16))
            sat = a.max(2) - a.min(2)
            g = a.mean(2)          # 亮度，float64；400dpi 一页 124 MB，只算一次
            mask = (sat > COLOR_SAT) & (g > KEEP_DARK)
            if mask.mean() > STAMP_RATIO:
                hit += 1
            g[mask] = 255
            buf = io.BytesIO()
            Image.fromarray(g.astype("uint8"), "L").save(buf, "JPEG")
            new = out.new_page(width=page.rect.width, height=page.rect.height)
            new.insert_image(new.rect, stream=buf.getvalue())
            n += 1

        if hit / max(1, n) < 0.1:   # 零星几页有色块，多半是插图，不值当重排
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
            return src, 0
        dst.parent.mkdir(parents=True, exist_ok=True)
        out.save(dst)
        return dst, hit
    finally:
        out.close()
        doc.close()


def run_one(pdf: Path, out_root: Path, name: str, force: bool = False) -> tuple[int, float]:
    """解析一本，返回 (页数, 耗时秒)。产物已存在就跳过。"""
    from magic_pdf.config.enums import SupportedPdfParseMethod
    from magic_pdf.data.data_reader_writer import FileBasedDataWriter
    from magic_pdf.data.dataset import PymuDocDataset
    from magic_pdf.model.doc_analyze_by_custom_model import doc_analyze

    d = out_root / name / "auto"
    if (d / f"{name}_content_list.json").exists() and not force:
        return 0, 0.0

    d.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    img_w, md_w = FileBasedDataWriter(str(d / "images")), FileBasedDataWriter(str(d))
    ds = PymuDocDataset(pdf.read_bytes())
    # classify() 判断有没有文本层。扫描件走 OCR，有文本层的直接取，快十几倍。
    if ds.classify() == SupportedPdfParseMethod.OCR:
        pipe = ds.apply(doc_analyze, ocr=True).pipe_ocr_mode(img_w)
    else:
        pipe = ds.apply(doc_analyze, ocr=False).pipe_txt_mode(img_w)

    pipe.dump_md(md_w, f"{name}.md", "images")
    pipe.dump_content_list(md_w, f"{name}_content_list.json", "images")
    return len(ds), time.time() - t0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="本地跑 OCR（MinerU），可中断可续跑")
    ap.add_argument("--list", required=True, help="每行一个 PDF 路径的清单")
    ap.add_argument("--out", default="data/ocr")
    ap.add_argument("--work", default="data/ocr/_merged", help="拼接后的整本 PDF 放这儿")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 本，用来先探速度")
    ap.add_argument("--force", action="store_true", help="已有产物也重跑（调参数时用）")
    args = ap.parse_args(argv)

    paths = [Path(l.strip()) for l in Path(args.list).read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")]
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"清单里有 {len(missing)} 个路径不存在，先看一眼：", file=sys.stderr)
        for p in missing[:5]:
            print("   ", p, file=sys.stderr)
        return 1

    groups = group_parts(paths)
    # 按清单里的顺序跑，不排序。清单是 tools.queue 按档位表生成的，
    # 而档位表是按「翻开它的频率」排的——排序等于把这个排法扔掉。
    # 原先用 sorted() 排字母序，结果 GB 55xxx 那五本全文强制的通用规范
    # 排到了队尾，玻璃标准反而先跑。跑 38 小时的活，顺序就是优先级：
    # 中途任何时候停下来，先跑完的那些得是最该先有的。
    todo = list(groups.items())
    if args.limit:
        todo = todo[: args.limit]

    _allow_globals()
    _patch_ocr_det()
    _patch_ocr_batch()
    out_root = Path(args.out)
    done = skipped = 0
    pages = secs = 0.0
    t_all = time.time()
    for i, (name, parts) in enumerate(todo, 1):
        _reclaim()
        tag = f"[{i}/{len(todo)}] {name}"
        # 跳过判断必须在 merge/preclean 之前。
        # run_one() 里面也有一份，但那太晚了：preclean 对没盖章的书不会留下
        # .clean.pdf，于是每次续跑都要把整本按 300dpi 重渲一遍，才发现这本早跑完了。
        # 14 本已完成的书 ≈ 一千页的无用渲染，重启一次就白等十几分钟。
        if (out_root / name / "auto" / f"{name}_content_list.json").exists() and not args.force:
            skipped += 1
            print(f"{tag}  已有产物，跳过", flush=True)
            continue
        try:
            pdf = merge(parts, Path(args.work) / f"{name}.pdf")
            pdf, stamped = preclean(pdf, Path(args.work) / f"{name}.clean.pdf")
            if stamped > 0:
                print(f"{tag}  去水印 {stamped} 页", flush=True)
            n, t = run_one(pdf, out_root, name, args.force)
        except Exception as exc:  # noqa: BLE001 - 一本崩了不能连累其余
            print(f"{tag}  ! 失败：{type(exc).__name__}: {exc}"[:200], flush=True)
            continue
        if n == 0:
            skipped += 1
            print(f"{tag}  已有产物，跳过", flush=True)
            continue
        done += 1
        pages += n
        secs += t
        print(f"{tag}  {n} 页 / {t:.0f}s（{t / n:.1f} s/页）"
              f"  累计 {pages:.0f} 页 {secs / 60:.0f} 分钟", flush=True)

    print(f"\n完成 {done} 本，跳过 {skipped} 本，共 {pages:.0f} 页，"
          f"用时 {(time.time() - t_all) / 60:.1f} 分钟"
          + (f"，均速 {secs / pages:.1f} s/页" if pages else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
