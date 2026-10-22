"""Run the shared checks without model downloads, credentials or PDF assets."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
env = {**os.environ, "KB_DEV": "1", "PYTHONPATH": str(ROOT / "src")}
commands = [
    [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
    [sys.executable, "-m", "building_kb", "check-snapshot"],
    [sys.executable, "-m", "building_kb", "export-public", "--check"],
    [sys.executable, "-m", "building_kb", "check-trial"],
    ["node", "tests/check_ui.js"],
]
for command in commands:
    subprocess.run(command, cwd=ROOT, env=env, check=True)
