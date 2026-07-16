from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TuiImportTest(unittest.TestCase):
    def test_tui_modules_are_importable_in_both_orders(self) -> None:
        env = os.environ.copy()
        source_root = str(ROOT / "src")
        env["PYTHONPATH"] = os.pathsep.join(
            path for path in (source_root, env.get("PYTHONPATH", "")) if path
        )
        snippets = (
            "import agent_workflow.tui_components",
            "import agent_workflow.tui; import agent_workflow.tui_components",
            "import agent_workflow.tui_components; import agent_workflow.tui",
            "from agent_workflow.tui import TuiApp, run_tui",
            "import agent_workflow.cli",
        )

        for snippet in snippets:
            with self.subTest(snippet=snippet):
                result = subprocess.run(
                    [sys.executable, "-c", snippet],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    0,
                    result.returncode,
                    msg=f"{snippet}\nstdout={result.stdout}\nstderr={result.stderr}",
                )
