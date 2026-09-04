"""SQLite 存储层。

选择 SQLite 的理由：单机部署零运维，标准库自带，
对这个量级（5 台主机、30 分钟一次、约 480 条/天）完全够用。

并发说明：Prefect 的 task 跑在线程池里，这里每个操作都开独立连接，
配合 WAL 模式避免写锁竞争，比共享单连接更省心。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .parser import ServerStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id     TEXT    NOT NULL,
    timestamp     TEXT    NOT NULL,
    online        INTEGER NOT NULL DEFAULT 0,
    cpu_percent   REAL    DEFAULT 0,
    cpu_count     INTEGER DEFAULT 0,
    mem_percent   REAL    DEFAULT 0,
    mem_used_mb   REAL    DEFAULT 0,
    mem_total_mb  REAL    DEFAULT 0,
    swap_percent  REAL    DEFAULT 0,
    load1         REAL    DEFAULT 0,
    load5         REAL    DEFAULT 0,
    load15        REAL    DEFAULT 0,
    disk_percent  REAL    DEFAULT 0,
    disk_used_gb  REAL    DEFAULT 0,
    disk_total_gb REAL    DEFAULT 0,
    error         TEXT,
    error_kind    TEXT,
    elapsed_ms    INTEGER DEFAULT 0,
    detail_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_server_time
    ON snapshots (server_id, timestamp);

CREATE TABLE IF NOT EXISTS logins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id   TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    username    TEXT,
    from_ip     TEXT,
    login_time  TEXT,
    duration    TEXT,
    source      TEXT,
    UNIQUE (server_id, username, from_ip, login_time, source)
);
CREATE INDEX IF NOT EXISTS idx_logins_server_time
    ON logins (server_id, observed_at);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id   TEXT NOT NULL,
    server_name TEXT,
    category    TEXT NOT NULL,
    severity    TEXT NOT NULL,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at);

CREATE TABLE IF NOT EXISTS known_ips (
    server_id  TEXT NOT NULL,
    ip         TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    PRIMARY KEY (server_id, ip)
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class Storage:
    """指标与告警的持久化入口。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    # ---------- 写入 ----------

    def save_status(self, status: ServerStatus) -> None:
        detail = {
            "disks": [
                {"mount": d.mount, "percent": d.percent,
                 "used_gb": d.used_gb, "total_gb": d.total_gb}
                for d in status.disks
            ],
            "top_processes": [
                {"business": p.business, "comm": p.comm,
                 "mem_percent": p.mem_percent, "cpu_percent": p.cpu_percent,
                 "pid": p.pid}
                for p in status.top_processes
            ],
            "who_online": [
                {"user": w.user, "from_ip": w.from_ip,
                 "login_time": w.login_time, "duration": w.duration}
                for w in status.who_online
            ],
        }

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO snapshots (
                    server_id, timestamp, online, cpu_percent, cpu_count,
                    mem_percent, mem_used_mb, mem_total_mb, swap_percent,
                    load1, load5, load15, disk_percent, disk_used_gb,
                    disk_total_gb, error, error_kind, elapsed_ms, detail_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    status.server_id,
                    _iso(status.timestamp),
                    int(status.online),
                    status.cpu_percent,
                    status.cpu_count,
                    status.mem_percent,
                    status.mem_used_mb,
                    status.mem_total_mb,
                    status.swap_percent,
                    status.load1,
                    status.load5,
                    status.load15,
                    status.disk_percent,
                    status.disk_used_gb,
                    status.disk_total_gb,
                    status.error,
                    status.error_kind,
                    status.elapsed_ms,
                    json.dumps(detail, ensure_ascii=False),
                ),
            )

            if status.online:
                observed = _iso(status.timestamp)
                for rec in status.logins:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO logins
                            (server_id, observed_at, username, from_ip,
                             login_time, duration, source)
                        VALUES (?,?,?,?,?,?,?)
                        """,
                        (
                            status.server_id, observed, rec.user, rec.from_ip,
                            rec.login_time, rec.duration, rec.source,
                        ),
                    )

    def register_ips(self, server_id: str, ips: Iterable[str]) -> list[str]:
        """登记观测到的 IP，返回其中首次出现的新 IP。"""
        now = _now_iso()
        new_ips: list[str] = []

        with self._connect() as conn:
            for ip in ips:
                if not ip:
                    continue
                cursor = conn.execute(
                    "SELECT 1 FROM known_ips WHERE server_id = ? AND ip = ?",
                    (server_id, ip),
                )
                if cursor.fetchone() is None:
                    conn.execute(
                        "INSERT INTO known_ips (server_id, ip, first_seen, last_seen)"
                        " VALUES (?,?,?,?)",
                        (server_id, ip, now, now),
                    )
                    new_ips.append(ip)
                else:
                    conn.execute(
                        "UPDATE known_ips SET last_seen = ? WHERE server_id = ? AND ip = ?",
                        (now, server_id, ip),
                    )

        return new_ips

    def save_alert(
        self,
        server_id: str,
        server_name: str,
        category: str,
        severity: str,
        message: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO alerts
                    (server_id, server_name, category, severity, message, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (server_id, server_name, category, severity, message, _now_iso()),
            )

    def recent_alert_exists(
        self, server_id: str, category: str, within_minutes: int
    ) -> bool:
        """冷却窗口内是否已有同类告警，用于去重。"""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT 1 FROM alerts
                WHERE server_id = ? AND category = ? AND created_at >= ?
                LIMIT 1
                """,
                (server_id, category, cutoff),
            )
            return cursor.fetchone() is not None

    # ---------- 查询 ----------

    def latest_statuses(self) -> list[sqlite3.Row]:
        """每台主机最近一次快照。"""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT s.* FROM snapshots s
                JOIN (
                    SELECT server_id, MAX(timestamp) AS ts
                    FROM snapshots GROUP BY server_id
                ) latest ON s.server_id = latest.server_id AND s.timestamp = latest.ts
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def history(
        self, server_id: str, hours: int = 24, limit: int = 2000
    ) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT timestamp, online, cpu_percent, mem_percent, swap_percent,
                       load1, disk_percent, error
                FROM snapshots
                WHERE server_id = ? AND timestamp >= ?
                ORDER BY timestamp ASC
                LIMIT ?
                """,
                (server_id, cutoff, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def login_history(self, server_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if server_id:
                cursor = conn.execute(
                    """
                    SELECT * FROM logins WHERE server_id = ?
                    ORDER BY observed_at DESC, id DESC LIMIT ?
                    """,
                    (server_id, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM logins ORDER BY observed_at DESC, id DESC LIMIT ?",
                    (limit,),
                )
            return [dict(row) for row in cursor.fetchall()]

    def alert_history(self, hours: int = 168, limit: int = 200) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM alerts WHERE created_at >= ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (cutoff, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def known_ips(self, server_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if server_id:
                cursor = conn.execute(
                    "SELECT * FROM known_ips WHERE server_id = ? ORDER BY last_seen DESC",
                    (server_id,),
                )
            else:
                cursor = conn.execute("SELECT * FROM known_ips ORDER BY last_seen DESC")
            return [dict(row) for row in cursor.fetchall()]

    def server_ids(self) -> list[str]:
        with self._connect() as conn:
            cursor = conn.execute("SELECT DISTINCT server_id FROM snapshots")
            return [row[0] for row in cursor.fetchall()]

    # ---------- 维护 ----------

    def cleanup(self, retention_days: int) -> dict[str, int]:
        """按保留期清理旧数据，返回各表删除行数。"""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat()
        counts: dict[str, int] = {}

        with self._connect() as conn:
            for table, column in (
                ("snapshots", "timestamp"),
                ("logins", "observed_at"),
                ("alerts", "created_at"),
            ):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE {column} < ?", (cutoff,)
                )
                counts[table] = cursor.rowcount or 0
            conn.execute("VACUUM")

        return counts
