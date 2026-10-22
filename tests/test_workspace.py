"""The installed app must not depend on the terminal's current directory."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from building_kb import paths


class WorkspaceTests(unittest.TestCase):
    def test_defaults_and_environment_overrides_resolve_from_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = {**os.environ, "PYTHONPATH": str(paths.ROOT / "src"),
                   "KB_HOME": str(paths.ROOT), "KB_CHUNKS": "examples/demo/chunks"}
            result = subprocess.run([sys.executable, "-c", "\n".join([
                "from building_kb import paths",
                "from building_kb.search.retrieve import Retriever",
                "r = Retriever(chroma_dir='/does-not-exist')",
                "assert len(r.pool) == 3",
                "assert paths.CHUNKS == paths.ROOT / 'examples/demo/chunks'",
                "assert paths.CATALOG.is_file()",
                "from building_kb.web.main import STATIC",
                "assert (STATIC / 'index.html').is_file()",
            ])], cwd=temporary, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
