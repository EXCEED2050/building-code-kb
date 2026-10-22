"""把检索池灌进 Chroma 向量库。

只索引检索池（排除被修订取代的、废止的、条文说明），理由见 retrieve.py：
索引里有一条 2010 版的旧条文，就可能有人照着它做设计。

增量重建：chunk 的内容哈希存在 metadata 里，没变的不重新算 embedding。
切条器改一次就全量重算的话，4000 条 × 每次几分钟，迭代会很难受。

向量默认在本机算（bge-m3，见 providers.py），不出网、不要 key。
模型还没下的话先跑 `python -m building_kb.commands.get_models`。

用法:
    python -m building_kb.search.index                # 增量
    python -m building_kb.search.index --rebuild      # 清空重建
"""

from __future__ import annotations

from building_kb import paths

import argparse
import hashlib
import json
from pathlib import Path

from .. import providers
from .retrieve import CHROMA_DIR, COLLECTION, Retriever


def digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def doc_of(c: dict) -> str:
    """送进 embedding 的文本。

    带上规范名和章节路径：条文正文常只说「不应小于表 5.2.2 的规定」，
    「防火间距」四个字只在节标题上。不带前缀，这一条在语义空间里几乎没有信息。
    """
    head = f"{c['std_code']} {c['std_name']} {c['chapter_path']} 第{c['clause_no']}条"
    return "\n".join([head, c["text"], *c["tables"]])


def meta_of(c: dict) -> dict:
    """Chroma 的 metadata 只接受标量，且要支撑检索时的精确过滤。"""
    return {
        "std_code": c["std_code"],
        "std_name": c["std_name"],
        "clause_no": c["clause_no"],
        "chapter_path": c["chapter_path"],
        "section_type": c["section_type"],
        "page_idx": -1 if c["page_idx"] is None else c["page_idx"],
        "is_mandatory": bool(c["is_mandatory"]),
        "status": c["status"],
        "hash": digest(doc_of(c)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="建立向量索引")
    ap.add_argument("--chunks", default=str(paths.CHUNKS))
    ap.add_argument("--out", default=CHROMA_DIR)
    ap.add_argument("--rebuild", action="store_true", help="清空重建")
    ap.add_argument("--batch", type=int, default=64)
    args = ap.parse_args(argv)

    try:
        embedder = providers.get_embedder()
    except providers.ProviderUnavailable as exc:
        print(f"无法建立向量索引：\n{exc}\n\n"
              "在此之前检索会自动退回纯 BM25，仍可正常使用（python -m building_kb.search.retrieve）。")
        return 1

    import chromadb

    pool = Retriever._load(args.chunks)
    print(f"检索池 {len(pool)} 条")

    client = chromadb.PersistentClient(path=args.out)
    if args.rebuild:
        try:
            client.delete_collection(COLLECTION)
        except Exception:  # noqa: BLE001 - 本来就不存在
            pass
    coll = client.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})

    # 已有的哈希，用来跳过没变的
    have: dict[str, str] = {}
    if coll.count():
        got = coll.get(include=["metadatas"])
        have = {i: (m or {}).get("hash", "") for i, m in zip(got["ids"], got["metadatas"])}

    todo = [c for c in pool if have.get(c["chunk_id"]) != digest(doc_of(c))]
    stale = set(have) - {c["chunk_id"] for c in pool}
    print(f"需要新建/更新 {len(todo)} 条，需要删除 {len(stale)} 条")

    if stale:
        coll.delete(ids=list(stale))

    for i in range(0, len(todo), args.batch):
        batch = todo[i: i + args.batch]
        docs = [doc_of(c) for c in batch]
        vecs = embedder.embed(docs)
        coll.upsert(
            ids=[c["chunk_id"] for c in batch],
            embeddings=vecs,
            documents=docs,
            metadatas=[meta_of(c) for c in batch],
        )
        print(f"  {min(i + args.batch, len(todo))}/{len(todo)}")

    print(f"完成，索引共 {coll.count()} 条 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
