"""把进程名翻译成可读的业务名。

规则迁移自 DeepSeekBalance 的 ServerMonitor.businessName()，
原实现是 Swift 里的一长串 if-else，这里改成有序规则表，
加新业务只需往 RULES 里加一行，不用动解析逻辑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BusinessRule:
    """一条匹配规则。

    field: 匹配 "args"（完整命令行，已小写）还是 "comm"（进程名）
    pattern: 正则或子串，用 re.search 匹配
    name: 命中后返回的业务名
    """

    field: str
    pattern: str
    name: str


# 顺序敏感：靠前的规则优先命中
RULES: tuple[BusinessRule, ...] = (
    # ---- 命令行关键词优先（同一个进程可能承载多个业务）----
    BusinessRule("args", r"deeptutor", "deeptutor"),
    BusinessRule("args", r"ecomm|cyberstroll|ecom-intel", "ecomm"),
    BusinessRule("args", r"openwifi", "openwifi"),
    BusinessRule("args", r"supplier|supply[-_]chain", "supply-chain"),
    BusinessRule("args", r"supply-chain-brain", "supply-chain-brain"),
    BusinessRule("args", r"ai-news", "ai-news"),

    # ---- 代理与面板 ----
    BusinessRule("comm", r"xray-linux", "xray代理"),
    BusinessRule("comm", r"x-ui", "xray面板"),
    BusinessRule("comm", r"next-server", "deeptutor-web"),

    # ---- Web 服务器进程，需结合命令行细分 ----
    BusinessRule("comm", r"gunicorn.*supply-chain-brain", "supply-chain-brain"),
    BusinessRule("comm", r"gunicorn.*ai-news", "ai-news"),
    BusinessRule("comm", r"gunicorn", "gunicorn"),
    BusinessRule("comm", r"uvicorn.*deeptutor", "deeptutor"),
    BusinessRule("comm", r"uvicorn", "uvicorn"),
    BusinessRule("comm", r"python.*deeptutor", "deeptutor"),
    BusinessRule("comm", r"python", "python"),

    # ---- 系统与基础设施 ----
    BusinessRule("comm", r"^java$", "java(ubuntu)"),
    BusinessRule("comm", r"nginx", "nginx"),
    BusinessRule("comm", r"dockerd", "docker"),
    BusinessRule("comm", r"containerd", "containerd"),
    BusinessRule("comm", r"fail2ban", "fail2ban"),
    BusinessRule("comm", r"fwupd", "fwupd"),
    BusinessRule("comm", r"journal", "systemd"),
    BusinessRule("comm", r"multipathd", "multipathd"),
    BusinessRule("comm", r"cron", "cron"),
    BusinessRule("comm", r"sshd", "sshd"),
    BusinessRule("comm", r"polkit", "polkit"),
    BusinessRule("comm", r"mysqld|mariadbd", "mysql"),
    BusinessRule("comm", r"redis-server", "redis"),
    BusinessRule("comm", r"postgres", "postgres"),
    BusinessRule("comm", r"node", "node"),
)

_COMPILED = tuple(
    (rule.field, re.compile(rule.pattern, re.IGNORECASE), rule.name) for rule in RULES
)


def business_name(comm: str, args: str = "") -> str:
    """识别进程属于哪个业务。识别不出就返回原始进程名。"""
    comm = comm or ""
    args_lower = (args or "").lower()

    for field, pattern, name in _COMPILED:
        target = args_lower if field == "args" else comm
        if pattern.search(target):
            return name

    return comm
