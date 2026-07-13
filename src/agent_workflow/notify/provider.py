from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


DEFAULT_NOTIFICATION_TIMEOUT_SECONDS = 120.0


class NotificationProvider(Protocol):
    @property
    def name(self) -> str: ...

    def generate(self, prompt: str) -> str | None: ...


@dataclass(frozen=True)
class CommandNotificationProvider:
    name: str
    command: tuple[str, ...]
    timeout_seconds: float = DEFAULT_NOTIFICATION_TIMEOUT_SECONDS

    def generate(self, prompt: str) -> str | None:
        if not self.command:
            return None
        with tempfile.TemporaryDirectory(prefix="agent-workflow-notify-") as work_dir:
            return _run_command(self.command, prompt, Path(work_dir), self.timeout_seconds)


@dataclass(frozen=True)
class CodexNotificationProvider:
    model: str = "gpt-5.6-luna"
    reasoning_effort: str = "medium"
    timeout_seconds: float = DEFAULT_NOTIFICATION_TIMEOUT_SECONDS
    name: str = "codex"

    def generate(self, prompt: str) -> str | None:
        with tempfile.TemporaryDirectory(prefix="agent-workflow-notify-") as work_dir:
            command = (
                "codex",
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "-C",
                work_dir,
                "-m",
                self.model,
                "--config",
                f"model_reasoning_effort={self.reasoning_effort}",
                "--config",
                "shell_environment_policy.inherit=none",
                "-",
            )
            return _run_command(command, prompt, Path(work_dir), self.timeout_seconds)


def notification_provider_from_env() -> NotificationProvider | None:
    provider_name = os.environ.get("AGENT_WORKFLOW_NOTIFICATION_PROVIDER", "codex").strip().lower()
    if provider_name in {"", "disabled", "none"}:
        return None

    timeout_seconds = _positive_float(
        os.environ.get("AGENT_WORKFLOW_NOTIFICATION_TIMEOUT_SECONDS"),
        DEFAULT_NOTIFICATION_TIMEOUT_SECONDS,
    )
    if provider_name == "codex":
        return CodexNotificationProvider(
            model=os.environ.get("AGENT_WORKFLOW_NOTIFICATION_CODEX_MODEL", "gpt-5.6-luna").strip()
            or "gpt-5.6-luna",
            reasoning_effort=os.environ.get(
                "AGENT_WORKFLOW_NOTIFICATION_CODEX_REASONING_EFFORT", "medium"
            ).strip()
            or "medium",
            timeout_seconds=timeout_seconds,
        )

    provider_key = re.sub(r"[^A-Z0-9]+", "_", provider_name.upper()).strip("_")
    raw_command = os.environ.get(f"AGENT_WORKFLOW_NOTIFICATION_{provider_key}_COMMAND")
    if not raw_command:
        raw_command = os.environ.get("AGENT_WORKFLOW_NOTIFICATION_COMMAND")
    if not raw_command:
        return None
    command = tuple(shlex.split(raw_command))
    if not command:
        return None
    return CommandNotificationProvider(provider_name, command, timeout_seconds)


def _run_command(command: tuple[str, ...], prompt: str, cwd: Path, timeout_seconds: float) -> str | None:
    try:
        result = subprocess.run(
            list(command),
            input=prompt,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None
    if result.returncode != 0:
        return None
    stdout = result.stdout
    if not isinstance(stdout, str) or not stdout.strip():
        return None
    return stdout


def _positive_float(raw: str | None, default: float) -> float:
    try:
        value = float(raw) if raw is not None else default
    except ValueError:
        return default
    return value if value > 0 else default
