"""配置加载：把 servers.yaml 解析成强类型配置对象。

安全设计：
- 密码支持三种来源，优先级 key_path > password_env > password
- 明文密码仅作兼容保留，生产环境建议用密钥或环境变量
- server_id 沿用旧 DeepSeekBalance 的规则，保证历史数据可平滑迁移
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "servers.yaml"


class ConfigError(ValueError):
    """配置缺失或非法时抛出，便于在 flow 启动时快速失败。"""


@dataclass
class AuthConfig:
    """SSH 认证信息。三种方式三选一。"""

    password: str | None = None
    password_env: str | None = None
    key_path: str | None = None
    passphrase: str | None = None

    def resolve_password(self) -> str | None:
        """按优先级解析出最终密码，取不到就返回 None。"""
        if self.password:
            return self.password
        if self.password_env:
            value = os.environ.get(self.password_env)
            if not value:
                raise ConfigError(
                    f"环境变量 {self.password_env} 未设置，无法完成 SSH 认证"
                )
            return value
        return None

    def resolve_key_path(self) -> Path | None:
        if not self.key_path:
            return None
        return Path(os.path.expanduser(self.key_path))

    def validate(self, server_name: str) -> None:
        has_key = bool(self.key_path)
        has_pwd = bool(self.password) or bool(self.password_env)
        if not has_key and not has_pwd:
            raise ConfigError(
                f"服务器 {server_name} 未配置任何认证方式，"
                f"需要 auth.key_path、auth.password 或 auth.password_env 之一"
            )
        if has_key and not self.resolve_key_path().exists():  # type: ignore[union-attr]
            raise ConfigError(
                f"服务器 {server_name} 的密钥文件不存在: {self.key_path}"
            )


@dataclass
class ServerConfig:
    name: str
    host: str
    username: str
    auth: AuthConfig
    port: int = 22
    enabled: bool = True
    disk_paths: list[str] = field(default_factory=lambda: ["/"])

    @property
    def server_id(self) -> str:
        """稳定 ID，沿用旧版规则以保证历史数据可对齐。

        旧版格式: SRV-50-118-187-180
        非 22 端口追加 -端口，避免同 IP 不同端口冲突。
        """
        base = f"SRV-{self.host.replace('.', '-')}"
        return base if self.port == 22 else f"{base}-{self.port}"

    @property
    def label(self) -> str:
        return f"{self.name} ({self.host})"


@dataclass
class ScheduleConfig:
    cron: str = "*/30 * * * *"
    timezone: str = "Asia/Shanghai"


@dataclass
class StorageConfig:
    sqlite_path: str = "data/fleet.db"
    retention_days: int = 90


@dataclass
class CollectConfig:
    timeout_seconds: int = 8
    command_timeout: int = 15
    max_workers: int = 10
    retries: int = 2
    retry_delay_seconds: int = 5


@dataclass
class AlertConfig:
    memory_percent: float = 90
    disk_percent: float = 85
    swap_percent: float = 80
    load_per_core: float = 4.0
    offline: bool = True
    new_login_ip: bool = True
    cooldown_minutes: int = 60


@dataclass
class NotifyConfig:
    webhook_url: str = ""
    webhook_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class AppConfig:
    servers: list[ServerConfig]
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    collect: CollectConfig = field(default_factory=CollectConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    @property
    def enabled_servers(self) -> list[ServerConfig]:
        return [s for s in self.servers if s.enabled]

    def get(self, server_id: str) -> ServerConfig | None:
        for s in self.servers:
            if s.server_id == server_id:
                return s
        return None


def _build_auth(raw: dict[str, Any]) -> AuthConfig:
    return AuthConfig(
        password=raw.get("password"),
        password_env=raw.get("password_env"),
        key_path=raw.get("key_path"),
        passphrase=raw.get("passphrase"),
    )


def _build_server(raw: dict[str, Any]) -> ServerConfig:
    for key in ("name", "host", "username"):
        if not raw.get(key):
            raise ConfigError(f"服务器配置缺少必填字段: {key}")

    auth_raw = raw.get("auth") or {}
    if not auth_raw:
        raise ConfigError(f"服务器 {raw['name']} 缺少 auth 配置段")

    auth = _build_auth(auth_raw)
    auth.validate(raw["name"])

    disk_paths = raw.get("disk_paths") or ["/"]
    if isinstance(disk_paths, str):
        disk_paths = [disk_paths]

    return ServerConfig(
        name=raw["name"],
        host=raw["host"],
        username=raw["username"],
        auth=auth,
        port=int(raw.get("port", 22)),
        enabled=bool(raw.get("enabled", True)),
        disk_paths=list(disk_paths),
    )


def server_from_dict(raw: dict[str, Any]) -> ServerConfig:
    """从字典重建 ServerConfig，供 Prefect task 跨线程传递参数使用。

    Prefect 提交 task 时会对参数做序列化，直接传 dataclass 容易踩坑，
    统一转成普通 dict 传递，在 task 内部再重建。
    """
    auth_raw = raw.get("auth") or {}
    auth = AuthConfig(
        password=auth_raw.get("password"),
        password_env=auth_raw.get("password_env"),
        key_path=auth_raw.get("key_path"),
        passphrase=auth_raw.get("passphrase"),
    )
    return ServerConfig(
        name=raw["name"],
        host=raw["host"],
        username=raw["username"],
        auth=auth,
        port=int(raw.get("port", 22)),
        enabled=bool(raw.get("enabled", True)),
        disk_paths=list(raw.get("disk_paths") or ["/"]),
    )


def load_config(path: Path | str | None = None) -> AppConfig:
    """加载并校验配置文件。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(
            f"配置文件不存在: {cfg_path}\n"
            f"请复制 config/servers.example.yaml 为 config/servers.yaml 后填写"
        )

    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    servers_raw = raw.get("servers") or []
    if not servers_raw:
        raise ConfigError(f"配置文件中没有 servers 段，无服务器可监控: {cfg_path}")

    servers = [_build_server(s) for s in servers_raw]

    # 同名或同 ID 会导致数据串台，启动时就拦下
    seen_ids: set[str] = set()
    for s in servers:
        if s.server_id in seen_ids:
            raise ConfigError(f"服务器 ID 冲突: {s.server_id} ({s.name})")
        seen_ids.add(s.server_id)

    return AppConfig(
        servers=servers,
        schedule=ScheduleConfig(**(raw.get("schedule") or {})),
        storage=StorageConfig(**(raw.get("storage") or {})),
        collect=CollectConfig(**(raw.get("collect") or {})),
        alerts=AlertConfig(**(raw.get("alerts") or {})),
        notify=NotifyConfig(**(raw.get("notify") or {})),
    )
