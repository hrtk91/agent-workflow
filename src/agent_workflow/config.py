from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CONFIG_FILE_ENV = "AGENT_WORKFLOW_CONFIG_FILE"
DEFAULT_NOTIFICATION_TIMEOUT_SECONDS = 120.0


class ConfigError(ValueError):
    pass


@dataclass
class NotificationProviderSettings:
    kind: str
    command: tuple[str, ...]
    timeout_seconds: float = DEFAULT_NOTIFICATION_TIMEOUT_SECONDS
    model: str | None = None
    reasoning_effort: str | None = None


@dataclass
class NotificationSettings:
    provider: str = "codex"
    providers: dict[str, NotificationProviderSettings] = field(default_factory=dict)


@dataclass
class AgentWorkflowSettings:
    notification: NotificationSettings = field(default_factory=NotificationSettings)


def default_config_path() -> Path:
    explicit = os.environ.get(CONFIG_FILE_ENV)
    if explicit:
        return Path(explicit).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "agent-workflow" / "config.toml"


def default_settings() -> AgentWorkflowSettings:
    return AgentWorkflowSettings(
        notification=NotificationSettings(
            provider="codex",
            providers={
                "codex": NotificationProviderSettings(
                    kind="codex",
                    command=("codex", "exec"),
                    timeout_seconds=DEFAULT_NOTIFICATION_TIMEOUT_SECONDS,
                    model="gpt-5.6-luna",
                    reasoning_effort="medium",
                )
            },
        )
    )


def load_settings(path: Path | None = None) -> AgentWorkflowSettings:
    """TOML を型付き設定へ読み込む。

    処理フロー:
    - [1] 読み込み先と組み込みの既定値を決める。
    - [2] ファイルがなければ既定値をそのまま返す。
    - [3] TOML ファイルを読み込んで解析する。
    - [4] TOML の値を既定値へ重ね、検証済みの設定を返す。
    """
    # [1] 明示されたパスを優先し、設定されていない項目の土台を用意する。
    config_path = (path or default_config_path()).expanduser()
    settings = default_settings()
    # [2] 初回起動は設定ファイルを必須にせず、安全な既定値で動かす。
    if not config_path.exists():
        return settings
    # [3] 読み込み・文字コード・TOML 構文の失敗を設定エラーとしてまとめる。
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"failed to read config {config_path}: {exc}") from exc
    # [4] provider ごとの部分設定を既定値へ重ね、型と値を検証する。
    return _settings_from_dict(data, settings, config_path)


def save_settings(settings: AgentWorkflowSettings, path: Path | None = None) -> Path:
    """型付き設定を、途中状態を見せずに TOML へ保存する。

    処理フロー:
    - [1] 保存先と設定内容を検証し、親ディレクトリを作る。
    - [2] 同じディレクトリの一時ファイルへ TOML を書く。
    - [3] 一時ファイルを対象ファイルへ原子的に置き換える。
    - [4] 失敗時は一時ファイルを削除して例外を戻す。
    """
    # [1] 不正な設定を永続化せず、保存先だけ先に用意する。
    config_path = (path or default_config_path()).expanduser()
    validate_settings(settings)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        # [2] TUI と CLI が読み込み途中の TOML を参照しないよう、別ファイルへ完成形を書く。
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(render_settings(settings))
        # [3] 完成した一時ファイルだけを、同一ファイルシステム上で原子的に反映する。
        os.replace(temp_path, config_path)
    except BaseException:
        # [4] 書き込みや置換に失敗した場合は一時ファイルを残さない。
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    return config_path


def initialize_settings(path: Path | None = None, force: bool = False) -> Path:
    config_path = (path or default_config_path()).expanduser()
    if config_path.exists() and not force:
        raise ConfigError(f"config already exists: {config_path}; pass --force to overwrite")
    return save_settings(default_settings(), config_path)


def render_settings(settings: AgentWorkflowSettings) -> str:
    lines = [
        "[notification]",
        f"provider = {_toml_string(settings.notification.provider)}",
    ]
    for name in sorted(settings.notification.providers):
        provider = settings.notification.providers[name]
        lines.extend(
            [
                "",
                f"[notification.providers.{_toml_key(name)}]",
                f"kind = {_toml_string(provider.kind)}",
                f"command = {_toml_array(provider.command)}",
                f"timeout_seconds = {provider.timeout_seconds:g}",
            ]
        )
        if provider.model:
            lines.append(f"model = {_toml_string(provider.model)}")
        if provider.reasoning_effort:
            lines.append(f"reasoning_effort = {_toml_string(provider.reasoning_effort)}")
    return "\n".join(lines) + "\n"


def validate_settings(settings: AgentWorkflowSettings) -> None:
    selected = settings.notification.provider.strip().lower()
    if selected not in settings.notification.providers:
        raise ConfigError(f"notification provider is not configured: {selected}")
    for name, provider in settings.notification.providers.items():
        if not name.strip():
            raise ConfigError("notification provider name must not be empty")
        if provider.kind not in {"codex", "command"}:
            raise ConfigError(f"{name}.kind must be codex or command")
        if not provider.command or not all(isinstance(item, str) and item for item in provider.command):
            raise ConfigError(f"{name}.command must be a non-empty string array")
        if provider.timeout_seconds <= 0:
            raise ConfigError(f"{name}.timeout_seconds must be positive")


def _settings_from_dict(
    data: dict[str, Any],
    defaults: AgentWorkflowSettings,
    config_path: Path,
) -> AgentWorkflowSettings:
    notification_raw = data.get("notification", {})
    if not isinstance(notification_raw, dict):
        raise ConfigError(f"notification must be a table in {config_path}")
    selected_provider = _string(
        notification_raw.get("provider", defaults.notification.provider),
        "notification.provider",
        config_path,
    ).lower()
    providers = dict(defaults.notification.providers)
    providers_raw = notification_raw.get("providers", {})
    if not isinstance(providers_raw, dict):
        raise ConfigError(f"notification.providers must be a table in {config_path}")
    for name, raw in providers_raw.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise ConfigError(f"notification provider entries must be tables in {config_path}")
        normalized_name = name.strip().lower()
        base = providers.get(normalized_name)
        kind = _string(
            raw.get("kind", base.kind if base else "command"),
            f"{normalized_name}.kind",
            config_path,
        ).lower()
        if kind not in {"codex", "command"}:
            raise ConfigError(f"{normalized_name}.kind must be codex or command in {config_path}")
        providers[normalized_name] = NotificationProviderSettings(
            kind=kind,
            command=_command(
                raw.get("command", list(base.command) if base else []),
                normalized_name,
                config_path,
            ),
            timeout_seconds=_positive_float(
                raw.get("timeout_seconds", base.timeout_seconds if base else DEFAULT_NOTIFICATION_TIMEOUT_SECONDS),
                f"{normalized_name}.timeout_seconds",
                config_path,
            ),
            model=_optional_string(
                raw.get("model", base.model if base else None),
                f"{normalized_name}.model",
                config_path,
            ),
            reasoning_effort=_optional_string(
                raw.get("reasoning_effort", base.reasoning_effort if base else None),
                f"{normalized_name}.reasoning_effort",
                config_path,
            ),
        )
    if selected_provider not in providers:
        raise ConfigError(f"notification provider is not configured: {selected_provider}")
    settings = AgentWorkflowSettings(NotificationSettings(selected_provider, providers))
    validate_settings(settings)
    return settings


def _string(value: object, field_name: str, config_path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} must be a non-empty string in {config_path}")
    return value.strip()


def _optional_string(value: object, field_name: str, config_path: Path) -> str | None:
    if value is None:
        return None
    return _string(value, field_name, config_path)


def _command(value: object, provider_name: str, config_path: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{provider_name}.command must be a non-empty string array in {config_path}")
    return tuple(value)


def _positive_float(value: object, field_name: str, config_path: Path) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{field_name} must be positive in {config_path}")
    return float(value)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else _toml_string(value)


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"
