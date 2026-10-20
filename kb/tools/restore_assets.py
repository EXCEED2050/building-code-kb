"""核验并恢复私有 Release 的原件/OCR 附件。无需模型或第三方依赖。

从 kb/ 运行：python -m tools.restore_assets /附件文件夹 [--check-only]。
各 ZIP 为独立资料卷，可以仅下载所需卷；已有文件不一致时拒绝覆盖。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def destination(root: Path, relative: str) -> Path:
    name = PurePosixPath(relative)
    if (name.is_absolute() or ".." in name.parts or "\\" in relative
            or not name.parts or name.parts[0] not in {"建筑规范", "MinerU", "kb"}
            or (name.parts[0] == "kb" and name.parts[:3] != ("kb", "data", "ocr"))
            or any(p.startswith(".env") for p in name.parts)):
        raise ValueError(f"附件中存在越界路径：{relative}")
    target = root.joinpath(*name.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"路径指向项目外：{relative}")
    return target


@contextmanager
def assembled_downloads(folder: Path):
    """将网络传输片段校验并临时合并；不修改下载目录或现有资料卷。"""
    manifest_path = folder / "asset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mapping = json.loads((folder / "upload-parts.json").read_text(encoding="utf-8"))
    assets = {a["file"]: a for a in manifest["assets"]}

    def safe_name(name):
        if not isinstance(name, str) or not name or Path(name).name != name or "\\" in name or name in {".", ".."}:
            raise ValueError("传输片段名称不能包含目录")
        return name

    with tempfile.TemporaryDirectory(prefix="kb-assembled-") as temporary:
        combined = Path(temporary)
        (combined / "asset-manifest.json").write_bytes(manifest_path.read_bytes())
        for name, asset in assets.items():
            safe_name(name)
            original = folder / name
            if original.is_file():
                (combined / name).symlink_to(original.resolve())
                continue
            parts = mapping.get("archives", {}).get(name, [])
            names = [safe_name(p["file"]) for p in parts]
            if len(set(names)) != len(names):
                raise ValueError("发现重复传输片段")
            present = [(folder / n).is_file() for n in names]
            if not any(present):
                continue
            if not all(present):
                missing = [n for n, found in zip(names, present) if not found]
                raise FileNotFoundError(f"{name} 尚缺 {len(missing)} 个片段：{missing[0]}")
            total = hashlib.sha256()
            size = 0
            with (combined / name).open("xb") as output:
                for row in parts:
                    part = folder / row["file"]
                    if part.stat().st_size != row["bytes"]:
                        raise ValueError(f"片段大小不符：{part.name}")
                    h = hashlib.sha256()
                    with part.open("rb") as source:
                        for data in iter(lambda: source.read(4 * 1024 * 1024), b""):
                            output.write(data)
                            h.update(data)
                            total.update(data)
                            size += len(data)
                    if h.hexdigest() != row["sha256"]:
                        raise ValueError(f"片段校验失败：{part.name}")
            if size != asset["bytes"] or total.hexdigest() != asset["sha256"]:
                raise ValueError(f"合并后的 ZIP 校验失败：{name}")
            print(f"已合并并核验 {name}（{len(parts)} 个片段）", flush=True)
        yield combined


def restore(folder: Path, root: Path, check_only: bool = False) -> dict:
    if (folder / "upload-parts.json").is_file():
        with assembled_downloads(folder) as combined:
            return restore(combined, root, check_only)
    manifest = json.loads((folder / "asset-manifest.json").read_text(encoding="utf-8"))
    expected = {r["path"]: r for r in manifest["files"]}
    result = {"checked_archives": 0, "checked_files": 0, "restored": 0, "unchanged": 0,
              "missing_archives": [], "check_only": check_only}
    for asset in manifest["assets"]:
        if Path(asset["file"]).name != asset["file"]:
            raise ValueError("附件名称不能包含目录")
        archive = folder / asset["file"]
        if not archive.exists():
            result["missing_archives"].append(asset["file"])
            continue
        if archive.stat().st_size != asset["bytes"] or digest(archive) != asset["sha256"]:
            raise ValueError(f"附件校验失败：{archive.name}")
        with zipfile.ZipFile(archive) as z:
            embedded_name = "ASSET_MANIFEST_" + asset["file"] + ".json"
            embedded = json.loads(z.read(embedded_name))
            records = embedded["files"]
            if len(records) != asset["files"] or len({r["path"] for r in records}) != len(records):
                raise ValueError("卷内文件数或唯一性不符合清单")
            if set(z.namelist()) != {embedded_name, *(r["path"] for r in records)}:
                raise ValueError("ZIP 中包含清单外文件")
            for row in records:
                if row != expected.get(row["path"]):
                    raise ValueError("卷内清单与总清单不一致")
                target = destination(root, row["path"])
                entry = z.getinfo(row["path"])
                if stat.S_ISLNK(entry.external_attr >> 16) or entry.file_size != row["bytes"]:
                    raise ValueError("附件包含链接或文件大小不符")
                already = False
                if not check_only and target.exists():
                    if not target.is_file() or digest(target) != row["sha256"]:
                        raise FileExistsError(f"保留已有不同内容的文件，未覆盖：{row['path']}")
                    already = True
                temp_path = None
                sink = None
                try:
                    if not check_only and not already:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        fd, name = tempfile.mkstemp(prefix=".restore-", dir=target.parent)
                        temp_path = Path(name)
                        sink = os.fdopen(fd, "wb")
                    h = hashlib.sha256()
                    size = 0
                    with z.open(entry) as source:
                        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
                            h.update(chunk)
                            size += len(chunk)
                            if sink is not None:
                                sink.write(chunk)
                    if size != row["bytes"] or h.hexdigest() != row["sha256"]:
                        raise ValueError(f"解压内容校验失败：{row['path']}")
                    if sink is not None:
                        sink.close()
                        sink = None
                        # hard link refuses a file created by another process after our existence check.
                        os.link(temp_path, target)
                        temp_path.unlink()
                        temp_path = None
                        result["restored"] += 1
                    elif already:
                        result["unchanged"] += 1
                    result["checked_files"] += 1
                finally:
                    if sink is not None:
                        sink.close()
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)
        result["checked_archives"] += 1
        print(f"已核验 {archive.name}", flush=True)
    if not result["checked_archives"]:
        raise FileNotFoundError("文件夹中没有找到清单列出的任何资料卷")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT, help="项目根目录，默认为当前工具所属项目")
    parser.add_argument("--check-only", action="store_true", help="只核验，不写入项目文件")
    args = parser.parse_args()
    print(json.dumps(restore(args.folder, args.root, args.check_only), ensure_ascii=False))


if __name__ == "__main__":
    main()
