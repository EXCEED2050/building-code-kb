"""把所有「要把文字发到外部」的调用收在这一个文件里。

2026-09-21 信息中心答复：中石化企业标准（DEP-*）可以走公有云 API。
这一层因此不再是合规闸门，但保留——理由变成了另外两条：

  1. 答复是可以改的。哪天收紧了，换内网模型只要在这里加一个实现、改一个环境变量，
     index.py / retrieve.py / answer.py / web/main.py 一行都不用动。
  2. 只有一个地方出网，出了事只用查一个文件。

业务代码一律不得直接 import requests 去调任何模型服务。

例外只有一个，写在这里是为了别让人以为「看完这个文件就看全了」：
commands/ocr_cloud.py 也出网（把扫描件传给 MinerU 云端做 OCR）。
没并进来是因为形态不同——那边是传文件加轮询，不是下面这三个协议，
硬塞进来会把查询链路搞脏。要审出网行为，这两个文件一起看。

默认搭配（2026-09-22 定）：**向量与重排跑在本机，问答走 DeepSeek**。
所以 `.env` 里只需要一个 DEEPSEEK_API_KEY，向量那一半一个字都不出网。

环境变量（写进 .env，别写进代码，也别贴进聊天记录）:
    DEEPSEEK_API_KEY     DeepSeek 密钥（作答）—— 唯一必填项
    KB_PROVIDER          三路统一切换：native | siliconflow | local
    KB_EMBED_PROVIDER    只切向量这一路（下同）。默认 native
    KB_RERANK_PROVIDER   默认 native
    KB_LLM_PROVIDER      默认 deepseek
    SILICONFLOW_API_KEY  硅基流动密钥（切到 siliconflow 时才要）
    KB_LOCAL_BASE_URL    私有化部署的 OpenAI 兼容地址（切到 local 时用）
    KB_DEVICE            本机推理设备，默认自动选 mps / cpu
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

import requests

from building_kb import paths

_ENV_FILE = paths.ROOT / ".env"
_env_loaded = False


def _load_env() -> None:
    """从 .env 读密钥。

    密钥写在文件里、由人自己填，好处是它不用经过任何别的地方：
    不进代码、不进聊天记录、不进命令历史（export 会被 shell 记下来）。
    已经在环境变量里的不覆盖——临时想换一把 key 时 `KEY=... python -m ...` 仍然有效。
    """
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


SILICONFLOW = "https://api.siliconflow.cn/v1"
DEEPSEEK = "https://api.deepseek.com/v1"

EMBED_MODEL = "BAAI/bge-m3"  # 1024 维，硅基流动当前免费
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
CHAT_MODEL = "deepseek-chat"

TIMEOUT = 60


class ProviderUnavailable(RuntimeError):
    """没有可用密钥 / 未配置。调用方应当降级而不是崩掉。"""


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    def rerank(self, query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]: ...


class LLM(Protocol):
    def stream(self, system: str, user: str): ...


# --------------------------------------------------------------------------
# 硅基流动（公有云）
# --------------------------------------------------------------------------


class SiliconFlowEmbedder:
    def __init__(self, key: str, model: str = EMBED_MODEL, base: str = SILICONFLOW):
        self.key, self.model, self.base = key, model, base

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        # 接口对单次条数和总 token 都有限制，分批发
        for i in range(0, len(texts), 32):
            batch = texts[i : i + 32]
            r = requests.post(
                f"{self.base}/embeddings",
                headers={"Authorization": f"Bearer {self.key}"},
                json={"model": self.model, "input": batch},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            out.extend(d["embedding"] for d in data)
        return out


class SiliconFlowReranker:
    def __init__(self, key: str, model: str = RERANK_MODEL, base: str = SILICONFLOW):
        self.key, self.model, self.base = key, model, base

    def rerank(self, query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
        if not docs:
            return []
        r = requests.post(
            f"{self.base}/rerank",
            headers={"Authorization": f"Bearer {self.key}"},
            json={
                "model": self.model,
                "query": query,
                "documents": docs,
                "top_n": min(top_n, len(docs)),
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return [(d["index"], d["relevance_score"]) for d in r.json()["results"]]


class OpenAICompatLLM:
    """DeepSeek 与多数私有化部署都是 OpenAI 兼容接口，共用一个实现。"""

    def __init__(self, key: str, base: str = DEEPSEEK, model: str = CHAT_MODEL):
        self.key, self.base, self.model = key, base, model

    def stream(self, system: str, user: str):
        r = requests.post(
            f"{self.base}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": True,
                # 查规范不需要发挥，温度压到最低
                "temperature": 0.1,
            },
            stream=True,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        import json as _json

        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            delta = _json.loads(payload)["choices"][0]["delta"].get("content")
            if delta:
                yield delta


# --------------------------------------------------------------------------
# 本机模型（不出网）
#
# 2026-09-22 定：向量与重排改在本机跑，一个字都不发出去。
# 这样连「企标可以出公网」那份批复都不必依赖——依赖别人的口径，
# 口径一变就得返工，而本机跑没有这个风险。问答仍走 DeepSeek：
# 16GB 的 M1 Pro 跑不动能把规范讲清楚的生成模型，硬跑出来的是似是而非的话，
# 在这个场景里比不回答更危险。
#
# 故意不依赖 sentence-transformers / FlagEmbedding：
# 本机这套 conda 环境是 magic-pdf 在用的，装新包有把 OCR 环境搞坏的风险
# （实测 sentence-transformers 已经跟 huggingface_hub 版本对不上了）。
# transformers + torch 本来就在，直接用它们够了——
# bge-m3 的 dense 向量就是 CLS 位置的隐状态做 L2 归一化，
# bge-reranker 就是一个二分类头取 logit，两者都没有什么需要封装的魔法。
# --------------------------------------------------------------------------

MODELS_DIR = paths.MODELS
# 模型目录名与 ModelScope / HuggingFace 上的仓库名对应，方便核对来源
NATIVE_EMBED_DIR = MODELS_DIR / "bge-m3"
NATIVE_RERANK_DIR = MODELS_DIR / "bge-reranker-v2-m3"

# 加载一次 2.3GB 模型要十几秒，且常驻显存/内存。
# get_embedder() 在 CLI、web 后端里都会被反复调用，不缓存就会反复加载直至内存耗尽。
_NATIVE_CACHE: dict[tuple, object] = {}


def _torch_device() -> str:
    """M1 的 GPU 叫 mps。没有就退 CPU——慢，但结果一致。"""
    import torch

    if os.environ.get("KB_DEVICE"):
        return os.environ["KB_DEVICE"]
    return "mps" if torch.backends.mps.is_available() else "cpu"


def _need_model(path: Path, what: str) -> Path:
    if not (path / "config.json").exists():
        raise ProviderUnavailable(
            f"{what}模型不在本机：{path}\n"
            f"先下载（从 ModelScope，国内直连）：\n"
            f"  python -m building_kb.commands.get_models\n"
            f"改用云端：在 .env 里写 KB_PROVIDER=siliconflow 并填 SILICONFLOW_API_KEY"
        )
    return path


def _by_budget(texts: list[str], batch: int, max_len: int):
    """按「批大小 × 批内最长长度」的预算分批，而不是按固定条数。

    注意力矩阵的开销是 批大小 × 长度²，而 padding 补到批内最长。
    检索池里绝大多数条文只有一两百字，可 5.2 节那条塞着 25 页的大表——
    固定 8 条一批，就得按最坏情况留够显存，否则偶遇一个长条就 OOM，
    而那一刻已经跑了半小时。按预算分：短条照样几十条一批，
    遇上长条自动缩到一两条，峰值是平的。

    长度用字符数估，不先 tokenize：估得粗一点没关系，
    为了分批把全库 tokenize 一遍反而更贵。
    """
    budget = batch * max_len
    group: list[str] = []
    longest = 0
    for t in texts:
        n = min(len(t), max_len)
        if group and max(longest, n) * (len(group) + 1) > budget:
            yield group
            group, longest = [], 0
        group.append(t)
        longest = max(longest, n)
    if group:
        yield group


def _dtype(torch, device: str):
    """GPU 上用 fp16，CPU 上用 fp32。

    权重从 2.3 GB 降到 1.15 GB，激活也减半。这不是微调性能，是能不能跑完：
    16 GB 统一内存上，fp32 的 bge-m3 建到一半就被 MPS 判 OOM。
    实测 fp16 与 fp32 的向量余弦相似度 0.999999，检索排序看不出差别。
    CPU 不走 fp16——半精度在 CPU 上没有加速，反而更慢。
    """
    return torch.float16 if device in ("mps", "cuda") else torch.float32


def _oom(exc: Exception) -> bool:
    return "out of memory" in str(exc).lower()


def _retry_oom(fn, group: list, torch, device: str) -> list:
    """撞上显存不足就把这一批对半拆开重试，拆到单条为止。

    建一次索引要跑半小时以上，这台机器上还同时开着编辑器和浏览器，
    可用内存是会变的。为一次偶发的 OOM 丢掉整趟跑不值得，
    而按最坏情况把批调小，则是让全程都慢下来去迁就那一次。
    拆批是**退一步就够**的做法：慢的只有出事的那一批。
    """
    try:
        return fn(group)
    except RuntimeError as exc:
        if not _oom(exc) or len(group) == 1:
            raise
        _release(torch, device)
        mid = len(group) // 2
        return _retry_oom(fn, group[:mid], torch, device) + _retry_oom(
            fn, group[mid:], torch, device
        )


def _run(m, group: list) -> list:
    """先按 GPU 跑，拆批也救不回来就整体退到 CPU，把这一趟跑完。

    拆批只能解决「这一批太大」，解决不了「整台机器没内存了」——
    实测 16 GB 的 MacBook 上，我们自己只占 1.16 GB，系统却报
    other allocations 19.51 GiB，连 2 KiB 都分不出来。
    这时候再拆批是徒劳的，只会把失败推迟几秒。

    退到 CPU 大约慢 6 倍（实测 630 ms/条，全库约 50 分钟），
    但内存占用是有上界的，一定跑得完。建索引是一次性的事，
    宁可慢一趟，也不要跑了半小时之后炸掉——何况炸掉的那次
    还得靠增量续跑，反复几轮比一次跑完更慢。
    """
    try:
        return _retry_oom(m._forward, group, m._torch, m.device)
    except RuntimeError as exc:
        if not _oom(exc) or m.device == "cpu":
            raise
        print(f"  ! {m.device} 内存不足，本次余下的都改用 CPU（会慢，但跑得完）")
        m.model = m.model.to("cpu").float()
        _release(m._torch, m.device)
        m.device = "cpu"
        return m._forward(group)


def _release(torch, device: str) -> None:
    """每批算完把显存还回去。

    MPS 的缓存分配器不会主动归还已经要到的块，跑长任务时只涨不落。
    这台机器是 16 GB 统一内存，OCR（magic-pdf）常常同时在跑并占着大头——
    实测建索引跑到 1344/7678 就 OOM 了，报的是
    「MPS allocated: 2.96 GiB, other allocations: 16.92 GiB」，
    也就是说我们自己只用了 3 GB，是没给别人留余地。
    每批一还，峰值才压得住，也不用去动别人的进程。
    """
    if device == "mps":
        torch.mps.empty_cache()
    elif device == "cuda":
        torch.cuda.empty_cache()


class NativeEmbedder:
    """本机跑 bge-m3，取 dense 向量（1024 维）。

    与硅基流动那条路产出的是同一个模型的同一种向量，维度、语义空间都一致，
    所以两边可以互换，已经建好的索引不用重建。
    """

    def __init__(self, model_dir: Path, batch: int = 8, max_len: int = 1024):
        import torch
        from transformers import AutoModel, AutoTokenizer

        self._torch = torch
        self.batch, self.max_len = batch, max_len
        self.device = _torch_device()
        self.tok = AutoTokenizer.from_pretrained(str(model_dir))
        self.model = (
            AutoModel.from_pretrained(str(model_dir), dtype=_dtype(torch, self.device))
            .to(self.device)
            .eval()
        )

    def _forward(self, group: list[str]) -> list[list[float]]:
        enc = self.tok(
            group,
            padding=True,
            truncation=True,
            max_length=self.max_len,
            return_tensors="pt",
        ).to(self.device)
        with self._torch.no_grad():
            # bge-m3 的 dense 向量取 CLS（第 0 个 token），再 L2 归一化。
            # 归一化不能省：Chroma 用余弦距离，没归一化的向量长度会混进距离里。
            h = self.model(**enc).last_hidden_state[:, 0]
            h = self._torch.nn.functional.normalize(h.float(), p=2, dim=-1)
        return h.cpu().tolist()

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for group in _by_budget(texts, self.batch, self.max_len):
            out.extend(_run(self, group))
            _release(self._torch, self.device)
        return out


class NativeReranker:
    """本机跑 bge-reranker-v2-m3。输出 logit，越大越相关。

    分数不做 sigmoid：调用方只拿它排序，不看绝对值，换算一道反而多一处出错的地方。
    """

    def __init__(self, model_dir: Path, batch: int = 8, max_len: int = 512):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self._torch = torch
        self.batch, self.max_len = batch, max_len
        self.device = _torch_device()
        self.tok = AutoTokenizer.from_pretrained(str(model_dir))
        self.model = (
            AutoModelForSequenceClassification.from_pretrained(
                str(model_dir), dtype=_dtype(torch, self.device)
            )
            .to(self.device)
            .eval()
        )

    def _forward(self, pairs: list[list[str]]) -> list[float]:
        enc = self.tok(
            pairs,
            padding=True,
            truncation=True,
            max_length=self.max_len,
            return_tensors="pt",
        ).to(self.device)
        with self._torch.no_grad():
            return self.model(**enc).logits.view(-1).float().cpu().tolist()

    def rerank(self, query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
        if not docs:
            return []
        scores: list[float] = []
        for group in _by_budget(docs, self.batch, self.max_len):
            scores.extend(_run(self, [[query, d] for d in group]))
            _release(self._torch, self.device)
        order = sorted(range(len(scores)), key=lambda i: -scores[i])[: min(top_n, len(docs))]
        return [(i, scores[i]) for i in order]


def _native(cls, path: Path, what: str):
    key = (cls.__name__, str(path))
    if key not in _NATIVE_CACHE:
        _NATIVE_CACHE[key] = cls(_need_model(path, what))
    return _NATIVE_CACHE[key]


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------


def _key(name: str) -> str:
    _load_env()
    v = os.environ.get(name, "").strip()
    if not v:
        raise ProviderUnavailable(
            f"没找到 {name}。在 .env 里写一行：\n"
            f"  {name}=sk-xxxx\n"
            f"（照着 .env.example 抄。别用 export——会进 shell 历史）\n"
            f"改用内网模型：KB_PROVIDER=local，KB_LOCAL_BASE_URL=http://内网地址/v1"
        )
    return v


def _mode(component: str, default: str) -> str:
    """某一路的供应商。分路可配，因为三路的取舍本来就不同。

    向量和重排走本机（不出网），问答走 DeepSeek——这是 09-22 定下的搭配，
    所以它是**默认值**，`.env` 里不写任何东西就是这个组合。
    整体切回云端：`KB_PROVIDER=siliconflow`。
    只切某一路：`KB_EMBED_PROVIDER=` / `KB_RERANK_PROVIDER=` / `KB_LLM_PROVIDER=`。

    取值：native（本机模型）| siliconflow（公有云）| local（OpenAI 兼容的内网地址）
    """
    _load_env()
    return (
        os.environ.get(f"KB_{component}_PROVIDER") or os.environ.get("KB_PROVIDER") or default
    ).strip()


def get_embedder() -> Embedder:
    mode = _mode("EMBED", "native")
    if mode == "native":
        return _native(NativeEmbedder, NATIVE_EMBED_DIR, "向量")  # type: ignore[return-value]
    if mode == "local":
        return SiliconFlowEmbedder(
            os.environ.get("KB_LOCAL_API_KEY", "none"),
            base=_key("KB_LOCAL_BASE_URL"),
        )
    return SiliconFlowEmbedder(_key("SILICONFLOW_API_KEY"))


def get_reranker() -> Reranker:
    mode = _mode("RERANK", "native")
    if mode == "native":
        return _native(NativeReranker, NATIVE_RERANK_DIR, "重排")  # type: ignore[return-value]
    if mode == "local":
        return SiliconFlowReranker(
            os.environ.get("KB_LOCAL_API_KEY", "none"),
            base=_key("KB_LOCAL_BASE_URL"),
        )
    return SiliconFlowReranker(_key("SILICONFLOW_API_KEY"))


def get_llm() -> LLM:
    mode = _mode("LLM", "deepseek")
    if mode == "local":
        return OpenAICompatLLM(
            os.environ.get("KB_LOCAL_API_KEY", "none"),
            base=_key("KB_LOCAL_BASE_URL"),
            model=os.environ.get("KB_LOCAL_MODEL", "local"),
        )
    return OpenAICompatLLM(_key("DEEPSEEK_API_KEY"))


def available() -> dict[str, bool]:
    """探测各能力是否可用，供 CLI / 健康检查显示。

    **只探测、不加载**。native 那两路真去 `get_*()` 会把 2.3GB 的模型读进内存，
    而这个函数是给「启动时打一行状态」用的——为了打一行字加载两个大模型，
    在 web 后端里等于每次健康检查都卡十几秒。看目录在不在就够了。
    """
    out = {}
    probes = (
        ("embed", "EMBED", "native", NATIVE_EMBED_DIR, "SILICONFLOW_API_KEY"),
        ("rerank", "RERANK", "native", NATIVE_RERANK_DIR, "SILICONFLOW_API_KEY"),
        ("llm", "LLM", "deepseek", None, "DEEPSEEK_API_KEY"),
    )
    for name, comp, default, model_dir, key_name in probes:
        mode = _mode(comp, default)
        if mode == "native":
            out[name] = model_dir is not None and (model_dir / "config.json").exists()
        elif mode == "local":
            out[name] = bool(os.environ.get("KB_LOCAL_BASE_URL", "").strip())
        else:
            out[name] = bool(os.environ.get(key_name, "").strip())
    return out
