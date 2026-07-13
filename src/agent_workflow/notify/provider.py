from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_workflow.config import (
    DEFAULT_NOTIFICATION_TIMEOUT_SECONDS,
    ConfigError,
    NotificationProviderSettings,
    load_settings,
)


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
    command_prefix: tuple[str, ...] = ("codex", "exec")
    model: str = "gpt-5.6-luna"
    reasoning_effort: str = "medium"
    timeout_seconds: float = DEFAULT_NOTIFICATION_TIMEOUT_SECONDS
    name: str = "codex"

    def generate(self, prompt: str) -> str | None:
        with tempfile.TemporaryDirectory(prefix="agent-workflow-notify-") as work_dir:
            command = (
                *self.command_prefix,
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


def notification_provider(config_path: Path | None = None) -> NotificationProvider | None:
    """永続設定と一時上書きから、今回使う通知 provider を組み立てる。

    処理フロー:
    - [1] TOML の永続設定を読み込む。
    - [2] 使用する provider 名を決め、無効化指定なら終了する。
    - [3] provider 固有、共通、TOML の順でコマンドを解決する。
    - [4] 未登録 provider は環境変数のコマンドから一時設定を作る。
    - [5] 環境変数を優先して timeout を解決する。
    - [6] kind に応じた実行オブジェクトを返す。
    """
    # [1] CLI が選んだ config path を起点に、provider ごとの永続設定を読む。
    settings = load_settings(config_path)
    # [2] 環境変数は一時的な選択を優先し、明示的な無効化もここで扱う。
    provider_name = os.environ.get(
        "AGENT_WORKFLOW_NOTIFICATION_PROVIDER",
        settings.notification.provider,
    ).strip().lower()
    if provider_name in {"", "disabled", "none"}:
        return None

    # [3] provider 固有の環境変数、共通環境変数、TOML の command の順で解決する。
    provider_settings = settings.notification.providers.get(provider_name)
    provider_key = re.sub(r"[^A-Z0-9]+", "_", provider_name.upper()).strip("_")
    raw_command = os.environ.get(f"AGENT_WORKFLOW_NOTIFICATION_{provider_key}_COMMAND")
    if not raw_command:
        raw_command = os.environ.get("AGENT_WORKFLOW_NOTIFICATION_COMMAND")
    # [4] 後方互換のため、未登録名でも command があれば一時的な汎用 provider として扱う。
    if provider_settings is None:
        if not raw_command:
            raise ConfigError(f"notification provider is not configured: {provider_name}")
        provider_settings = NotificationProviderSettings("command", tuple(shlex.split(raw_command)))

    # [5] timeout は一時上書きを優先し、不正値なら provider 設定へ戻す。
    timeout_seconds = _positive_float_override(
        os.environ.get("AGENT_WORKFLOW_NOTIFICATION_TIMEOUT_SECONDS"),
        provider_settings.timeout_seconds,
    )
    # [6] Codex は隔離オプションを付ける専用実装、それ以外は汎用 command 実装を返す。
    if provider_settings.kind == "codex":
        return CodexNotificationProvider(
            command_prefix=_command_override(raw_command, provider_settings.command),
            model=os.environ.get(
                "AGENT_WORKFLOW_NOTIFICATION_CODEX_MODEL",
                provider_settings.model or "gpt-5.6-luna",
            ).strip()
            or "gpt-5.6-luna",
            reasoning_effort=os.environ.get(
                "AGENT_WORKFLOW_NOTIFICATION_CODEX_REASONING_EFFORT",
                provider_settings.reasoning_effort or "medium",
            ).strip()
            or "medium",
            timeout_seconds=timeout_seconds,
        )
    if provider_settings.kind != "command":
        raise ConfigError(f"unknown notification provider kind: {provider_settings.kind}")
    command = _command_override(raw_command, provider_settings.command)
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


def _command_override(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(shlex.split(raw)) if raw else default


def _positive_float_override(raw: str | None, default: float) -> float:
    try:
        value = float(raw) if raw is not None else default
    except ValueError:
        return default
    return value if value > 0 else default
