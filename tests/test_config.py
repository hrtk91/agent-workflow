from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AW = ROOT / "scripts" / "aw"
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.config import (
    NotificationProviderSettings,
    default_settings,
    load_settings,
    save_settings,
)


class ConfigTest(unittest.TestCase):
    def test_provider_settings_round_trip(self) -> None:
        settings = default_settings()
        settings.notification.provider = "claude"
        settings.notification.providers["claude"] = NotificationProviderSettings(
            kind="command",
            command=("claude", "--print"),
            timeout_seconds=45,
        )
        settings.notification.providers["grok"] = NotificationProviderSettings(
            kind="command",
            command=("grok-wrapper", "--stdin"),
            timeout_seconds=60,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = save_settings(settings, Path(temp_dir) / "config.toml")
            loaded = load_settings(path)

        self.assertEqual("claude", loaded.notification.provider)
        self.assertEqual(("claude", "--print"), loaded.notification.providers["claude"].command)
        self.assertEqual(45, loaded.notification.providers["claude"].timeout_seconds)
        self.assertEqual(("grok-wrapper", "--stdin"), loaded.notification.providers["grok"].command)

    def test_config_cli_initializes_and_shows_effective_toml(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "agent-workflow" / "config.toml"
            initialized = self._aw("--config-file", str(config_path), "config", "init")
            shown = self._aw("--config-file", str(config_path), "config", "show")

            self.assertEqual(str(config_path), initialized.stdout.strip())
            self.assertTrue(config_path.exists())
            self.assertIn('provider = "codex"', shown.stdout)
            self.assertIn("[notification.providers.codex]", shown.stdout)
            self.assertIn('command = ["codex", "exec"]', shown.stdout)
            self.assertIn("timeout_seconds = 120", shown.stdout)

    def _aw(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PATH"] = f"{Path(sys.executable).parent}:{env['PATH']}"
        result = subprocess.run(
            [str(AW), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        if result.returncode != 0:
            self.fail(f"aw failed with {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}")
        return result


if __name__ == "__main__":
    unittest.main()
