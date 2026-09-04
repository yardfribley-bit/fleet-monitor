"""告警判定与通知。

去重策略：
- 同类告警有冷却窗口，窗口内不重复触发
- 新 IP 登录采用基线静默：某台主机首次采集时只登记 IP 不告警，
  否则第一次运行会把 last 里的历史登录全部误报成新 IP
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import AlertConfig, NotifyConfig
from .parser import ServerStatus
from .storage import Storage


@dataclass
class Alert:
    server_id: str
    server_name: str
    category: str
    severity: str
    message: str

    @property
    def icon(self) -> str:
        return {"critical": "[CRIT]", "warning": "[WARN]", "info": "[INFO]"}.get(
            self.severity, "[INFO]"
        )

    def format(self) -> str:
        return f"{self.icon} {self.server_name} · {self.message}"


def evaluate(
    status: ServerStatus,
    cfg: AlertConfig,
    storage: Storage,
) -> list[Alert]:
    """对单台主机的状态做告警判定，返回需要发出的告警。"""
    alerts: list[Alert] = []
    name = status.server_name or status.server_id

    def add(category: str, severity: str, message: str) -> None:
        if storage.recent_alert_exists(status.server_id, category, cfg.cooldown_minutes):
            return
        alerts.append(Alert(status.server_id, name, category, severity, message))

    # 离线优先判定，离线时其他指标无意义
    if not status.online:
        if cfg.offline:
            reason = status.error or "无法连接"
            add("offline", "critical", f"主机离线：{reason}")
        return alerts

    if cfg.memory_percent and status.mem_percent >= cfg.memory_percent:
        add(
            "memory",
            "warning",
            f"内存使用率 {status.mem_percent:.1f}% ≥ 阈值 {cfg.memory_percent}%"
            f"（{status.mem_used_mb:.0f}/{status.mem_total_mb:.0f} MB）",
        )

    if cfg.disk_percent and status.disk_percent >= cfg.disk_percent:
        add(
            "disk",
            "warning",
            f"磁盘使用率 {status.disk_percent:.1f}% ≥ 阈值 {cfg.disk_percent}%"
            f"（{status.disk_used_gb:.1f}/{status.disk_total_gb:.1f} GB）",
        )

    if cfg.swap_percent and status.swap_percent >= cfg.swap_percent:
        add(
            "swap",
            "warning",
            f"Swap 使用率 {status.swap_percent:.1f}% ≥ 阈值 {cfg.swap_percent}%",
        )

    if cfg.load_per_core and status.cpu_count > 0:
        per_core = status.load_per_core
        if per_core >= cfg.load_per_core:
            add(
                "load",
                "warning",
                f"每核负载 {per_core:.2f} ≥ 阈值 {cfg.load_per_core}"
                f"（load1={status.load1:.2f}，{status.cpu_count} 核）",
            )

    # 新 IP 登录：先登记再判断是否首次出现
    if cfg.new_login_ip:
        is_first_run = len(storage.known_ips(status.server_id)) == 0
        new_ips = storage.register_ips(status.server_id, status.remote_ips)
        if new_ips and not is_first_run:
            add(
                "new_ip",
                "warning",
                f"检测到新的 SSH 来源 IP：{', '.join(new_ips)}",
            )

    return alerts


def send_notifications(alerts: list[Alert], cfg: NotifyConfig) -> int:
    """推送告警到 webhook。未配置则只返回 0，由调用方落日志。"""
    if not cfg.webhook_url or not alerts:
        return 0

    payload = {
        "text": "\n".join(a.format() for a in alerts),
        "alerts": [
            {
                "server_id": a.server_id,
                "server_name": a.server_name,
                "category": a.category,
                "severity": a.severity,
                "message": a.message,
            }
            for a in alerts
        ],
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        cfg.webhook_url,
        data=data,
        headers={"Content-Type": "application/json", **cfg.webhook_headers},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 1 if 200 <= response.status < 300 else 0
    except (urllib.error.URLError, TimeoutError):
        return 0
