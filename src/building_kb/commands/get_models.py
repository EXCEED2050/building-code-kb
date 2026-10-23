"""把向量与重排模型下到本机，之后整条检索链路不再出网。

走 ModelScope 而不是 HuggingFace：国内直连，不用代理，
而且 magic-pdf 的模型本来就在 ModelScope 上，来源统一好核对。

这一步是一次性的。下完之后 `src/providers.py` 的 native 路径直接从
`var/models/` 读，断网也能建索引、能检索。

用法:
    python -m building_kb.commands.get_models              # 两个都下
    python -m building_kb.commands.get_models --only embed # 只下向量模型
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from building_kb import paths

MODELS = paths.MODELS

# (本地目录名, ModelScope 仓库, 用途, 大致体积)
SPECS = {
    "embed": ("bge-m3", "BAAI/bge-m3", "向量（1024 维）", "约 2.3 GB"),
    "rerank": ("bge-reranker-v2-m3", "BAAI/bge-reranker-v2-m3", "重排", "约 2.3 GB"),
}

# 只要这些。仓库里还有 onnx/ 子目录、图片、以及 colbert/sparse 两个附加头，
# 全下会多拖好几个 GB，而我们只用 dense 向量，transformers 一个都不读。
#
# 权重两种格式都列：ModelScope 上这两个仓库**格式不一样**——
# bge-m3 只有 pytorch_model.bin，bge-reranker-v2-m3 只有 model.safetensors。
# 只列 safetensors 的话 bge-m3 会下回来一堆小文件、没有权重，
# 而目录看着是存在的（这正是 `fetch` 末尾那道完整性检查要挡的情况）。
WEIGHTS = ("model.safetensors", "pytorch_model.bin")
ALLOW = [
    "config.json",
    "configuration.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
    "sentence_bert_config.json",
    *WEIGHTS,
]


def _has_weights(d: Path) -> bool:
    return any((d / w).exists() for w in WEIGHTS)


def fetch(key: str, force: bool = False) -> bool:
    name, repo, use, size = SPECS[key]
    dest = MODELS / name
    if (dest / "config.json").exists() and _has_weights(dest) and not force:
        print(f"[{use}] 已在 {dest}，跳过")
        return True

    from modelscope import snapshot_download

    print(f"[{use}] 从 ModelScope 下载 {repo}（{size}）…")
    try:
        got = snapshot_download(repo, allow_file_pattern=ALLOW)
    except Exception as exc:  # noqa: BLE001 - 下载失败要说清是哪一个，别把两个混在一起
        print(f"  ! {repo} 下载失败：{exc}", file=sys.stderr)
        return False

    dest.mkdir(parents=True, exist_ok=True)
    for f in Path(got).iterdir():
        if f.is_file():
            shutil.copy2(f, dest / f.name)
    # 缺权重就是没下全。此时留着目录会让 providers 以为「模型在本机」，
    # 于是在加载时才炸，而且报的是 transformers 的内部错——
    # 不如在这里说清楚。
    if not _has_weights(dest):
        print(
            f"  ! {repo} 下下来了但没有权重文件（{' / '.join(WEIGHTS)}），判定为不完整",
            file=sys.stderr,
        )
        return False
    total = sum(f.stat().st_size for f in dest.iterdir() if f.is_file())
    print(f"  -> {dest}（{total / 1e9:.2f} GB）")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="下载本机向量 / 重排模型")
    ap.add_argument("--only", choices=sorted(SPECS), help="只下其中一个")
    ap.add_argument("--force", action="store_true", help="已存在也重下")
    args = ap.parse_args(argv)

    keys = [args.only] if args.only else list(SPECS)
    ok = [k for k in keys if fetch(k, args.force)]
    print(f"\n完成 {len(ok)}/{len(keys)}。模型目录：{MODELS}")
    if len(ok) < len(keys):
        print("没下全。可以重跑本命令（已下好的会跳过）。")
        return 1
    print("下一步：python -m building_kb.search.index   # 建向量索引")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
