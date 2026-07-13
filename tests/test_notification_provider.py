from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.config import NotificationProviderSettings, default_settings, save_settings
from agent_workflow.notify.provider import (
    CodexNotificationProvider,
    CommandNotificationProvider,
    notification_provider,
)


class NotificationProviderTest(unittest.TestCase):
    def test_codex_provider_uses_isolated_non_persistent_invocation(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="notification\n", stderr="")
        with mock.patch("agent_workflow.notify.provider.subprocess.run", return_value=completed) as run:
            output = CodexNotificationProvider(timeout_seconds=12).generate("prompt")

        self.assertEqual("notification\n", output)
        command = run.call_args.args[0]
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--ephemeral", command)
        self.assertEqual("read-only", command[command.index("--sandbox") + 1])
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("shell_environment_policy.inherit=none", command)
        self.assertEqual("-", command[-1])
        self.assertEqual("prompt", run.call_args.kwargs["input"])
        self.assertEqual(12, run.call_args.kwargs["timeout"])
        self.assertNotEqual(Path.cwd(), Path(run.call_args.kwargs["cwd"]))

    def test_named_provider_uses_provider_specific_command(self) -> None:
        settings = default_settings()
        settings.notification.provider = "claude"
        settings.notification.providers["claude"] = NotificationProviderSettings(
            kind="command",
            command=("claude", "--print"),
            timeout_seconds=30,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = save_settings(settings, Path(temp_dir) / "config.toml")
            with mock.patch.dict(os.environ, {}, clear=True):
                provider = notification_provider(config_path)

        self.assertIsInstance(provider, CommandNotificationProvider)
        assert isinstance(provider, CommandNotificationProvider)
        self.assertEqual("claude", provider.name)
        self.assertEqual(("claude", "--print"), provider.command)
        self.assertEqual(30, provider.timeout_seconds)

    def test_command_provider_passes_prompt_on_stdin(self) -> None:
        provider = CommandNotificationProvider(
            "fixture",
            (
                sys.executable,
                "-c",
                "import sys; print(sys.stdin.read().upper())",
            ),
            timeout_seconds=5,
        )

        self.assertEqual("HELLO\n", provider.generate("hello"))


if __name__ == "__main__":
    unittest.main()
