"""Install this checkout's Git hooks and commit template."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    subprocess.run(
        [sys.executable, "-m", "pre_commit", "install"],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        ["git", "config", "--local", "commit.template", str(ROOT / ".gitmessage")],
        cwd=ROOT,
        check=True,
    )
    print("当前仓库已启用代码检查、提交信息检查和提交模板。")


if __name__ == "__main__":
    main()
