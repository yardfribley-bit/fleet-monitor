"""把 SSH 返回的原始文本解析成结构化指标。

设计原则：任何一段解析失败都不能影响其他段。
不同发行版的 free/top/ps/last 输出存在细微差异，
所以每个字段都做容错，解析不出来就保持零值而不是抛异常。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .business import business_name
from .collector import CollectResult

SECTION_RE = re.compile(r"^===([A-Z]+)===\s*$", re.MULTILINE)
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


@dataclass
class DiskInfo:
    mount: str
    total_gb: float = 0.0
    used_gb: float = 0.0
    percent: float = 0.0


@dataclass
class ProcessInfo:
    business: str
    comm: str
    mem_percent: float
    cpu_percent: float
    pid: int


@dataclass
class LoginRecord:
    user: str
    from_ip: str
    login_time: str
    duration: str
    source: str  # last = 历史登录, who = 当前在线

    @property
    def is_remote(self) -> bool:
        """是否为远程 IP 登录，本地 tty 或 :0 不算。"""
        return bool(IPV4_RE.match(self.from_ip or ""))


@dataclass
class ServerStatus:
    server_id: str
    server_name: str = ""
    host: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    online: bool = False
    error: str | None = None
    error_kind: str | None = None
    elapsed_ms: int = 0

    cpu_percent: float = 0.0
    cpu_count: int = 0

    mem_percent: float = 0.0
    mem_used_mb: float = 0.0
    mem_total_mb: float = 0.0
    mem_avail_mb: float = 0.0
    swap_percent: float = 0.0

    load1: float = 0.0
    load5: float = 0.0
    load15: float = 0.0

    disks: list[DiskInfo] = field(default_factory=list)
    top_processes: list[ProcessInfo] = field(default_factory=list)
    logins: list[LoginRecord] = field(default_factory=list)
    who_online: list[LoginRecord] = field(default_factory=list)

    @property
    def disk_percent(self) -> float:
        """主磁盘（/ 或第一个挂载点）使用率，供告警与趋势图使用。"""
        if not self.disks:
            return 0.0
        for d in self.disks:
            if d.mount == "/":
                return d.percent
        return self.disks[0].percent

    @property
    def disk_used_gb(self) -> float:
        return self.disks[0].used_gb if self.disks else 0.0

    @property
    def disk_total_gb(self) -> float:
        return self.disks[0].total_gb if self.disks else 0.0

    @property
    def load_per_core(self) -> float:
        """按核数归一化的负载，跨机型可比。"""
        if self.cpu_count <= 0:
            return 0.0
        return self.load1 / self.cpu_count

    @property
    def remote_ips(self) -> list[str]:
        """本次观测到的所有远程登录 IP（去重，保持顺序）。"""
        seen: dict[str, None] = {}
        for rec in self.logins + self.who_online:
            if rec.is_remote:
                seen.setdefault(rec.from_ip, None)
        return list(seen.keys())


def split_sections(raw: str) -> dict[str, list[str]]:
    """按 ===NAME=== 切成 {段名: 行列表}。"""
    sections: dict[str, list[str]] = {}
    matches = list(SECTION_RE.finditer(raw))
    for i, match in enumerate(matches):
        name = match.group(1)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[start:end].strip("\n")
        sections[name] = [line for line in body.split("\n") if line.strip()]
    return sections


def _to_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _gb_value(text: str) -> float:
    """把 df 的 40G / 1.5T / 512M 转成 GB。"""
    text = text.strip()
    try:
        if text.endswith("T"):
            return float(text[:-1]) * 1024
        if text.endswith("G"):
            return float(text[:-1])
        if text.endswith("M"):
            return float(text[:-1]) / 1024
        if text.endswith("K"):
            return float(text[:-1]) / 1024 / 1024
        if text.endswith("P"):
            return float(text[:-1]) * 1024 * 1024
        return float(text)
    except ValueError:
        return 0.0


def parse_cpu(lines: list[str]) -> float:
    """从 top 输出算 CPU 使用率。

    兼容两种格式：
      新版: %Cpu(s):  5.9 us,  2.0 sy,  0.0 ni, 91.2 id, ...
      旧版: Cpu(s):  5.9%us,  2.0%sy,  0.0%ni, 91.2%id, ...
    优先取 idle 反推，取不到就用 us+sy+ni 累加。
    """
    if not lines:
        return 0.0
    line = lines[0]

    match = re.search(r"([\d.]+)\s*%?\s*id\b", line, re.IGNORECASE)
    if match:
        return round(100.0 - _to_float(match.group(1), 100.0), 2)

    total = 0.0
    for key in ("us", "sy", "ni"):
        m = re.search(rf"([\d.]+)\s*%?\s*{key}\b", line, re.IGNORECASE)
        if m:
            total += _to_float(m.group(1))
    return round(total, 2)


def parse_mem(lines: list[str]) -> tuple[float, float, float, float, float]:
    """解析 free -m，返回 (percent, used_mb, total_mb, avail_mb, swap_percent)。"""
    percent = used = total = avail = 0.0
    swap_percent = 0.0

    for line in lines:
        fields = line.split()
        if not fields:
            continue

        if fields[0] == "Mem:" and len(fields) >= 7:
            total = _to_float(fields[1])
            used = _to_float(fields[2])
            # 不同版本 available 列位置可能是 index 6（free 新版本）
            avail = _to_float(fields[6])
            if total > 0:
                percent = round((total - avail) / total * 100, 2)

        elif fields[0] == "Swap:" and len(fields) >= 3:
            swap_total = _to_float(fields[1])
            swap_used = _to_float(fields[2])
            if swap_total > 0:
                swap_percent = round(swap_used / swap_total * 100, 2)

    return percent, used, total, avail, swap_percent


def parse_load(lines: list[str]) -> tuple[float, float, float]:
    """从 uptime 解析 1/5/15 分钟负载。"""
    if not lines:
        return 0.0, 0.0, 0.0
    match = re.search(r"load average:\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)", lines[0])
    if not match:
        return 0.0, 0.0, 0.0
    return (
        _to_float(match.group(1)),
        _to_float(match.group(2)),
        _to_float(match.group(3)),
    )


def parse_disks(lines: list[str]) -> list[DiskInfo]:
    """解析 df -h 输出，支持多个挂载点。"""
    disks: list[DiskInfo] = []

    for line in lines:
        fields = line.split()
        if len(fields) < 6 or not fields[0].startswith("/dev"):
            continue

        mount = fields[5]
        percent = _to_float(fields[4].rstrip("%"))
        disks.append(
            DiskInfo(
                mount=mount,
                total_gb=round(_gb_value(fields[1]), 2),
                used_gb=round(_gb_value(fields[2]), 2),
                percent=percent,
            )
        )

    return disks


def parse_processes(lines: list[str], limit: int = 10) -> list[ProcessInfo]:
    """解析 ps 输出，前两行是表头，需要跳过。"""
    procs: list[ProcessInfo] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("PID"):
            continue

        fields = stripped.split(maxsplit=5)
        if len(fields) < 6:
            continue

        pid_str, _user, mem_str, cpu_str, comm, args = fields[:6]
        pid = int(pid_str) if pid_str.isdigit() else 0
        mem = _to_float(mem_str)
        cpu = _to_float(cpu_str)

        procs.append(
            ProcessInfo(
                business=business_name(comm, args),
                comm=comm,
                mem_percent=mem,
                cpu_percent=cpu,
                pid=pid,
            )
        )

        if len(procs) >= limit:
            break

    return procs


def parse_last(lines: list[str]) -> list[LoginRecord]:
    """解析 last 输出的历史登录记录。

    过滤 reboot / shutdown 这类非登录条目。
    """
    records: list[LoginRecord] = []

    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue

        user = fields[0]
        if user in ("reboot", "shutdown", "wtmp"):
            continue

        source_ip = fields[2] if len(fields) > 2 else ""
        # 时间形如: Sun Aug 16 11:54
        login_time = " ".join(fields[3:6]) if len(fields) >= 6 else ""
        duration = fields[-1].strip("()") if fields else ""

        records.append(
            LoginRecord(
                user=user,
                from_ip=source_ip,
                login_time=login_time,
                duration=duration,
                source="last",
            )
        )

    return records


def parse_who(lines: list[str]) -> list[LoginRecord]:
    """解析 who 输出的当前在线会话。

    格式: root  pts/0  2026-09-04 15:20 (1.2.3.4)
    """
    records: list[LoginRecord] = []

    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue

        user = fields[0]
        login_time = " ".join(fields[2:4]) if len(fields) >= 4 else ""
        ip = ""
        if fields and fields[-1].startswith("("):
            ip = fields[-1].strip("()")

        records.append(
            LoginRecord(
                user=user,
                from_ip=ip,
                login_time=login_time,
                duration="在线",
                source="who",
            )
        )

    return records


def parse(result: CollectResult) -> ServerStatus:
    """把一次采集结果解析成 ServerStatus。"""
    status = ServerStatus(
        server_id=result.server_id,
        server_name=result.server_name,
        host=result.host,
        timestamp=result.timestamp,
        online=result.online,
        error=result.error,
        error_kind=result.error_kind,
        elapsed_ms=result.elapsed_ms,
    )

    if not result.online or not result.raw:
        return status

    sections = split_sections(result.raw)

    nproc_lines = sections.get("NPROC", [])
    if nproc_lines and nproc_lines[0].strip().isdigit():
        status.cpu_count = int(nproc_lines[0].strip())

    status.cpu_percent = parse_cpu(sections.get("CPU", []))

    (
        status.mem_percent,
        status.mem_used_mb,
        status.mem_total_mb,
        status.mem_avail_mb,
        status.swap_percent,
    ) = parse_mem(sections.get("MEM", []))

    status.load1, status.load5, status.load15 = parse_load(sections.get("LOAD", []))
    status.disks = parse_disks(sections.get("DISK", []))
    status.top_processes = parse_processes(sections.get("PROC", []))
    status.logins = parse_last(sections.get("LAST", []))
    status.who_online = parse_who(sections.get("WHO", []))

    return status
