"""本机跑一下看看页面。只绑 127.0.0.1，口令每次启动现随机生成并打在日志里。

正式部署不要用这个入口——用:
    export KB_PASSCODE='……'
    uvicorn building_kb.web.main:app --host 0.0.0.0 --port 8000
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from building_kb import paths  # loads root .env before auth is imported

import os

os.environ.setdefault("KB_DEV", "1")

if __name__ == "__main__":
    import uvicorn

    # 只听回环地址：这个入口没有正经口令，不该让同网段的人摸到
    uvicorn.run("building_kb.web.main:app", host="127.0.0.1", port=8000, log_level="warning")
