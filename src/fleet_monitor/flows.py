"""Prefect 编排层。

并发策略：
- SSH 采集是慢 IO（单台可能几秒），用线程池并发跑
- SQLite 写入和告警判定是本地快操作，串行执行，
  避免并发读写导致同一 IP 被重复判定为「新 IP」

重试策略：
- 认证失败不重试（重试也没用，只会拖慢整批）
- 网络类失败按配置重试
"""

from __future__ import annotations

import os
from typing import Any

from prefect import flow, get_run_logger, task
from prefect.task_runners import ThreadPoolTaskRunner


def _default_workers() -> int:
    """并发采集线程数。可用环境变量覆盖，默认 10 足够覆盖常见规模。"""
    return int(os.environ.get("FLEET_MAX_WORKERS", "10"))

from .alerts import Alert, evaluate, send_notifications
from .collector import collect_with_retry
from .config import AppConfig, load_config, server_from_dict
from .parser import parse
from .storage import Storage


@task(
    name="collect-host",
    retries=0,  # 重试在 task 内部控制，以便区分认证失败
    retry_delay_seconds=0,
    log_prints=False,
)
def collect_host_task(
    server_dict: dict[str, Any],
    timeout: int = 8,
    command_timeout: int = 15,
    retries: int = 2,
    retry_delay_seconds: int = 5,
) -> dict[str, Any]:
    """采集并解析单台主机，返回可序列化的状态字典。"""
    server = server_from_dict(server_dict)
    logger = get_run_logger()

    raw_result = collect_with_retry(
        server,
        timeout=timeout,
        command_timeout=command_timeout,
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
        logger=logger,
    )
    return _status_to_dict(parse(raw_result))


def _status_to_dict(status) -> dict[str, Any]:
    """把 ServerStatus 转成可跨线程/进程传递的字典。"""
    return {
        "server_id": status.server_id,
        "server_name": status.server_name,
        "host": status.host,
        "timestamp": status.timestamp.isoformat(),
        "online": status.online,
        "error": status.error,
        "error_kind": status.error_kind,
        "elapsed_ms": status.elapsed_ms,
        "cpu_percent": status.cpu_percent,
        "cpu_count": status.cpu_count,
        "mem_percent": status.mem_percent,
        "mem_used_mb": status.mem_used_mb,
        "mem_total_mb": status.mem_total_mb,
        "swap_percent": status.swap_percent,
        "load1": status.load1,
        "load5": status.load5,
        "load15": status.load15,
        "disk_percent": status.disk_percent,
        "disk_used_gb": status.disk_used_gb,
        "disk_total_gb": status.disk_total_gb,
        "disks": [
            {"mount": d.mount, "percent": d.percent,
             "used_gb": d.used_gb, "total_gb": d.total_gb}
            for d in status.disks
        ],
        "top_processes": [
            {"business": p.business, "comm": p.comm, "mem_percent": p.mem_percent,
             "cpu_percent": p.cpu_percent, "pid": p.pid}
            for p in status.top_processes
        ],
        "logins": [
            {"user": r.user, "from_ip": r.from_ip, "login_time": r.login_time,
             "duration": r.duration, "source": r.source}
            for r in status.logins
        ],
        "who_online": [
            {"user": w.user, "from_ip": w.from_ip, "login_time": w.login_time,
             "duration": w.duration, "source": w.source}
            for w in status.who_online
        ],
        "remote_ips": status.remote_ips,
    }


@flow(
    name="fleet-health-check",
    log_prints=True,
    task_runner=ThreadPoolTaskRunner(max_workers=_default_workers()),
)
def fleet_health_check(config_path: str | None = None) -> dict[str, Any]:
    """采集全部启用主机的健康指标、落库并判定告警。"""
    logger = get_run_logger()
    cfg: AppConfig = load_config(config_path)

    servers = cfg.enabled_servers
    if not servers:
        logger.warning("配置中没有启用的服务器")
        return {"checked": 0, "online": 0, "alerts": 0}

    logger.info("开始采集 %d 台主机", len(servers))

    # 慢 IO 并发
    futures = [
        collect_host_task.submit(
            _server_to_dict(s),
            timeout=cfg.collect.timeout_seconds,
            command_timeout=cfg.collect.command_timeout,
            retries=cfg.collect.retries,
            retry_delay_seconds=cfg.collect.retry_delay_seconds,
        )
        for s in servers
    ]
    raw_statuses = [f.result() for f in futures]

    # 本地操作串行，避免 SQLite 并发写竞争
    storage = Storage(cfg.storage.sqlite_path)
    alerts: list[Alert] = []
    online_count = 0

    for data in raw_statuses:
        status = _dict_to_status(data)
        if status.online:
            online_count += 1

        storage.save_status(status)
        found = evaluate(status, cfg.alerts, storage)
        alerts.extend(found)

        if status.online:
            logger.info(
                "%s CPU %.1f%% | 内存 %.1f%% | 磁盘 %.1f%% | 负载 %.2f | %dms",
                status.server_name, status.cpu_percent, status.mem_percent,
                status.disk_percent, status.load1, status.elapsed_ms,
            )
        else:
            logger.error("%s 采集失败: %s", status.server_name, status.error)

    for alert in alerts:
        storage.save_alert(
            alert.server_id, alert.server_name,
            alert.category, alert.severity, alert.message,
        )
        logger.warning(alert.format())

    if alerts:
        sent = send_notifications(alerts, cfg.notify)
        if cfg.notify.webhook_url:
            logger.info("已推送 %d 条告警（webhook %s）", len(alerts),
                        "成功" if sent else "失败")

    summary = {
        "checked": len(raw_statuses),
        "online": online_count,
        "offline": len(raw_statuses) - online_count,
        "alerts": len(alerts),
    }
    logger.info("采集完成: %s", summary)
    return summary


def _server_to_dict(server) -> dict[str, Any]:
    return {
        "name": server.name,
        "host": server.host,
        "username": server.username,
        "port": server.port,
        "enabled": server.enabled,
        "disk_paths": server.disk_paths,
        "auth": {
            "password": server.auth.password,
            "password_env": server.auth.password_env,
            "key_path": server.auth.key_path,
            "passphrase": server.auth.passphrase,
        },
    }


def _dict_to_status(data: dict[str, Any]):
    """把字典还原成 ServerStatus，供存储与告警模块使用。"""
    from datetime import datetime

    from .parser import DiskInfo, LoginRecord, ProcessInfo, ServerStatus

    ts = data.get("timestamp")
    timestamp = datetime.fromisoformat(ts) if isinstance(ts, str) else datetime.now()

    return ServerStatus(
        server_id=data["server_id"],
        server_name=data.get("server_name", ""),
        host=data.get("host", ""),
        timestamp=timestamp,
        online=data.get("online", False),
        error=data.get("error"),
        error_kind=data.get("error_kind"),
        elapsed_ms=data.get("elapsed_ms", 0),
        cpu_percent=data.get("cpu_percent", 0.0),
        cpu_count=data.get("cpu_count", 0),
        mem_percent=data.get("mem_percent", 0.0),
        mem_used_mb=data.get("mem_used_mb", 0.0),
        mem_total_mb=data.get("mem_total_mb", 0.0),
        swap_percent=data.get("swap_percent", 0.0),
        load1=data.get("load1", 0.0),
        load5=data.get("load5", 0.0),
        load15=data.get("load15", 0.0),
        disks=[DiskInfo(**d) for d in data.get("disks", [])],
        top_processes=[ProcessInfo(**p) for p in data.get("top_processes", [])],
        logins=[LoginRecord(**r) for r in data.get("logins", [])],
        who_online=[LoginRecord(**w) for w in data.get("who_online", [])],
    )
