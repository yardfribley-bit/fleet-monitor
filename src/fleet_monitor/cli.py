"""命令行入口。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__


def _ensure_local_proxy_bypass() -> None:
    """确保本地回环地址不走系统代理。

    本机可能开着 SOCKS/HTTP 系统代理（如 127.0.0.1:1081），而 macOS 的
    proxy_bypass 默认只放行 localhost、不放行 127.0.0.1。Prefect 连接本地
    server 用的是 ``ws://127.0.0.1:PORT``，会被误判为需走 SOCKS 代理，
    导致 events 客户端报 ``SOCKS proxy requires python-socks``。

    这里在进程内补上 NO_PROXY，让本地连接直连。该变量只影响走 HTTP 的库
    （httpx/urllib/websockets），不影响 paramiko 的裸 TCP SSH 连接。
    """
    loopback = "127.0.0.1,localhost,::1"
    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    merged = ",".join(x for x in (loopback, existing) if x)
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged
from .config import load_config
from .collector import collect_with_retry
from .parser import parse


def _print_status(status, verbose: bool = False) -> None:
    if status.online:
        print(
            f"  [OK]   {status.server_name:<24} {status.host:<18} "
            f"CPU {status.cpu_percent:5.1f}% | 内存 {status.mem_percent:5.1f}% | "
            f"磁盘 {status.disk_percent:5.1f}% | 负载 {status.load1:5.2f} | "
            f"{status.elapsed_ms}ms"
        )
        if verbose:
            print(f"         核数 {status.cpu_count}，Swap {status.swap_percent:.1f}%")
            for disk in status.disks:
                print(
                    f"         磁盘 {disk.mount}: {disk.percent:.1f}% "
                    f"({disk.used_gb:.1f}/{disk.total_gb:.1f} GB)"
                )
            top = status.top_processes[:5]
            if top:
                print("         Top 进程:")
                for proc in top:
                    print(
                        f"           {proc.business:<18} 内存 {proc.mem_percent:5.1f}% "
                        f"CPU {proc.cpu_percent:5.1f}%  (pid {proc.pid})"
                    )
            remote = status.remote_ips
            if remote:
                print(f"         SSH 来源 IP: {', '.join(remote[:8])}")
    else:
        print(
            f"  [FAIL] {status.server_name:<24} {status.host:<18} "
            f"{status.error}"
        )


def cmd_check(args: argparse.Namespace) -> int:
    """连通性体检：只采集并打印，不写入数据库。"""
    cfg = load_config(args.config)
    servers = cfg.enabled_servers
    print(f"连通性检查：{len(servers)} 台主机\n")

    failed = 0
    for server in servers:
        result = collect_with_retry(
            server,
            timeout=cfg.collect.timeout_seconds,
            command_timeout=cfg.collect.command_timeout,
            retries=cfg.collect.retries,
            retry_delay_seconds=cfg.collect.retry_delay_seconds,
        )
        status = parse(result)
        _print_status(status, verbose=args.verbose)
        if not status.online:
            failed += 1

    print(f"\n在线 {len(servers) - failed}/{len(servers)}")
    return 1 if failed else 0


def cmd_run(args: argparse.Namespace) -> int:
    """立即执行一次完整采集。"""
    from .flows import fleet_health_check

    summary = fleet_health_check(config_path=args.config)
    print(f"\n汇总: {summary}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """常驻进程，按 cron 定时触发。"""
    from prefect.client.schemas.schedules import CronSchedule

    from .flows import fleet_health_check

    cfg = load_config(args.config)
    config_path = str(Path(args.config).resolve()) if args.config else None

    print(f"启动常驻调度: {args.name}")
    print(f"  cron     : {cfg.schedule.cron} ({cfg.schedule.timezone})")
    print(f"  主机数量 : {len(cfg.enabled_servers)}")
    print(f"  数据库   : {cfg.storage.sqlite_path}")
    print("按 Ctrl+C 停止\n")

    # Prefect 3.x 的 serve 不接受 timezone 参数，时区须通过 CronSchedule 对象传入
    schedules = [
        CronSchedule(cron=cfg.schedule.cron, timezone=cfg.schedule.timezone)
    ]
    fleet_health_check.serve(
        name=args.name,
        schedules=schedules,
        parameters={"config_path": config_path},
    )
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    """清理超出保留期的历史数据。"""
    from .storage import Storage

    cfg = load_config(args.config)
    storage = Storage(cfg.storage.sqlite_path)
    days = args.days or cfg.storage.retention_days
    counts = storage.cleanup(days)
    print(f"已清理 {days} 天前的数据: {counts}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleet-monitor",
        description="基于 Prefect 的多主机健康巡检与 SSH 登录审计",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c", "--config", default=None,
        help="配置文件路径（默认 config/servers.yaml）",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="连通性体检，不写数据库")
    p_check.add_argument("-v", "--verbose", action="store_true", help="显示详细指标")
    p_check.set_defaults(func=cmd_check)

    p_run = sub.add_parser("run", help="立即执行一次采集")
    p_run.set_defaults(func=cmd_run)

    p_serve = sub.add_parser("serve", help="常驻调度，按 cron 定时执行")
    p_serve.add_argument(
        "-n", "--name", default="fleet-health-30min", help="部署名称"
    )
    p_serve.set_defaults(func=cmd_serve)

    p_cleanup = sub.add_parser("cleanup", help="清理历史数据")
    p_cleanup.add_argument("--days", type=int, default=None, help="保留天数")
    p_cleanup.set_defaults(func=cmd_cleanup)

    return parser


def main(argv: list[str] | None = None) -> int:
    _ensure_local_proxy_bypass()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已停止")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"执行失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
