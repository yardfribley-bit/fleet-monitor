#!/usr/bin/env python3
"""从 DeepSeekBalance macOS App 迁移服务器配置与历史数据。

用法:
    python scripts/migrate_from_deepseekbalance.py                 # 只迁移服务器清单
    python scripts/migrate_from_deepseekbalance.py --with-history  # 连历史数据一起迁移

说明:
- 旧 App 把服务器配置存在 ~/Library/Application Support/DeepSeekBalance/servers.json
- 历史时间戳是 Core Data 格式（自 2001-01-01 起的秒），需加 978307200 转成 Unix 时间
- 生成的 config/servers.yaml 含明文密码，脚本会自动设置 600 权限
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Core Data 时间戳纪元（2001-01-01）与 Unix 纪元（1970-01-01）的差值
CORE_DATA_EPOCH_OFFSET = 978307200

DEFAULT_SOURCE_DIR = Path.home() / "Library" / "Application Support" / "DeepSeekBalance"


def load_old_servers(source_dir: Path) -> list[dict]:
    path = source_dir / "servers.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到旧配置: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("servers", [])


def build_server_entry(old: dict) -> dict:
    """把旧配置条目转成新格式。"""
    host_raw = old.get("host", "")
    host, _, port_str = host_raw.partition(":")
    port = int(port_str) if port_str.isdigit() else 22

    return {
        "name": old.get("name", host),
        "host": host,
        "port": port,
        "username": old.get("username", "root"),
        "enabled": bool(old.get("enabled", True)),
        "disk_paths": ["/"],
        "auth": {"password": old.get("password", "")},
    }


def convert_timestamp(value: float) -> str:
    """Core Data 时间戳 -> ISO8601 UTC 字符串。"""
    # 小于该值的肯定是 Core Data 时间戳而非 Unix 时间戳
    if value < CORE_DATA_EPOCH_OFFSET:
        value += CORE_DATA_EPOCH_OFFSET
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def migrate_history(source_dir: Path, db_path: Path) -> int:
    """迁移 server_history.json 到新数据库。"""
    import sqlite3

    path = source_dir / "server_history.json"
    if not path.exists():
        print(f"  [跳过] 历史文件不存在: {path}")
        return 0

    with path.open("r", encoding="utf-8") as f:
        entries = json.load(f)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from fleet_monitor.storage import Storage  # noqa: E402

    storage = Storage(db_path)
    count = 0

    with sqlite3.connect(str(db_path)) as conn:
        for entry in entries:
            ts = entry.get("timestamp")
            if not isinstance(ts, (int, float)):
                continue
            conn.execute(
                """
                INSERT INTO snapshots (
                    server_id, timestamp, online, mem_percent, swap_percent,
                    load1, disk_percent
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    entry.get("serverId", ""),
                    convert_timestamp(ts),
                    int(bool(entry.get("online", False))),
                    float(entry.get("memPercent", 0) or 0),
                    float(entry.get("swapPercent", 0) or 0),
                    float(entry.get("load1", 0) or 0),
                    float(entry.get("diskPercent", 0) or 0),
                ),
            )
            count += 1

    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="从 DeepSeekBalance 迁移配置与历史")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE_DIR), help="旧配置目录")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[1] / "config" / "servers.yaml"),
        help="输出配置文件路径",
    )
    parser.add_argument("--with-history", action="store_true", help="同时迁移历史数据")
    parser.add_argument(
        "--history-db",
        default=str(Path(__file__).resolve().parents[1] / "data" / "fleet.db"),
        help="历史数据写入的 SQLite 路径",
    )
    args = parser.parse_args()

    source_dir = Path(args.source).expanduser()
    config_path = Path(args.config)

    print(f"源目录: {source_dir}")

    try:
        old_servers = load_old_servers(source_dir)
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    if not old_servers:
        print("旧配置中没有服务器记录")
        return 1

    print(f"发现 {len(old_servers)} 台服务器:")
    for s in old_servers:
        print(f"  - {s.get('name')} ({s.get('host')}) user={s.get('username')}")

    # 生成新配置
    config = {
        "schedule": {"cron": "*/30 * * * *", "timezone": "Asia/Shanghai"},
        "storage": {"sqlite_path": "data/fleet.db", "retention_days": 90},
        "collect": {
            "timeout_seconds": 8,
            "command_timeout": 15,
            "max_workers": 10,
            "retries": 2,
            "retry_delay_seconds": 5,
        },
        "alerts": {
            "memory_percent": 90,
            "disk_percent": 85,
            "swap_percent": 80,
            "load_per_core": 4.0,
            "offline": True,
            "new_login_ip": True,
            "cooldown_minutes": 60,
        },
        "notify": {"webhook_url": ""},
        "servers": [build_server_entry(s) for s in old_servers],
    }

    # 已存在则先备份，避免覆盖用户手工调整过的配置
    if config_path.exists():
        backup = config_path.with_suffix(f".yaml.bak.{datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(config_path, backup)
        print(f"\n已存在的配置已备份为: {backup}")

    try:
        import yaml
    except ImportError:
        print("错误: 需要 PyYAML，请先 pip install pyyaml", file=sys.stderr)
        return 1

    config_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# fleet-monitor 配置 - 由 migrate_from_deepseekbalance.py 生成\n"
        f"# 迁移时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        "#\n"
        "# 安全提示: 本文件含明文 SSH 密码，已设为 600 权限且被 .gitignore 排除。\n"
        "# 建议尽快改用 SSH 密钥认证，把 auth.password 换成 auth.key_path。\n\n"
    )
    with config_path.open("w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    os.chmod(config_path, 0o600)
    print(f"\n配置已写入: {config_path} (权限 600)")

    if args.with_history:
        print("\n迁移历史数据...")
        count = migrate_history(source_dir, Path(args.history_db))
        print(f"  已迁移 {count} 条历史快照")
        print("  注意: 旧版本不采集 CPU 使用率，历史数据的 cpu_percent 为空")

    print("\n下一步:")
    print(f"  1. 连通性体检:  python -m fleet_monitor.cli check -v")
    print(f"  2. 立即采集一次: python -m fleet_monitor.cli run")
    print(f"  3. 启动定时调度: python -m fleet_monitor.cli serve")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
