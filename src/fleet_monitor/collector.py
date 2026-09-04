"""通过 SSH 采集远程主机的原始指标文本。

相对旧版 Swift 实现的改动：
1. 用 paramiko 直连，替掉 SSH_ASKPASS + base64 密码的临时脚本 hack
2. 新增真实 CPU 使用率采集（旧版只有 load average，没有 CPU%）
3. 新增 nproc（核心数），让负载告警能按核数归一化
4. 新增 who（当前在线会话），配合 last 做登录审计
5. 磁盘支持多个挂载点，不再只看 /
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import paramiko

from .config import ServerConfig


class SSHAuthError(RuntimeError):
    """认证失败，重试无意义，应直接标记为主机异常。"""


class SSHConnectError(RuntimeError):
    """网络层失败，可重试。"""


@dataclass
class CollectResult:
    """一台主机的一次采集结果。raw 为原始输出，交给 parser 解析。"""

    server_id: str
    server_name: str
    host: str
    online: bool = False
    raw: str = ""
    error: str | None = None
    error_kind: str | None = None  # auth / connect / timeout / command
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    elapsed_ms: int = 0

    @property
    def label(self) -> str:
        return f"{self.server_name} ({self.host})"


def build_command(disk_paths: list[str]) -> str:
    """拼装远程采集脚本。

    各段用 ===NAME=== 分隔，parser 按此切分。
    所有命令都加 LC_ALL=C，避免不同语言环境下表头被本地化导致解析失败。
    """
    paths = disk_paths or ["/"]
    df_targets = " ".join(paths)

    return f"""
echo "===NPROC==="; LC_ALL=C nproc 2>/dev/null
echo "===CPU==="; LC_ALL=C top -bn2 -d 0.2 2>/dev/null | grep -iE '^%?[Cc]pu' | tail -1
echo "===MEM==="; LC_ALL=C free -m | head -3
echo "===LOAD==="; LC_ALL=C uptime
echo "===DISK==="; LC_ALL=C df -h {df_targets} 2>/dev/null | tail -n +1
echo "===PROC==="; LC_ALL=C ps -eo pid,user,pmem,pcpu,comm,args --sort=-pmem 2>/dev/null | head -10
echo "===LAST==="; LC_ALL=C last -n 20 2>/dev/null | grep -v '^$' | grep -v 'wtmp begins' | head -20
echo "===WHO==="; LC_ALL=C who 2>/dev/null | head -10
""".strip()


def collect(server: ServerConfig, timeout: int = 8, command_timeout: int = 15) -> CollectResult:
    """SSH 连上一台主机并取回原始指标。

    不抛异常：任何失败都封装进 CollectResult.error，
    这样单台主机故障不会中断整批采集。
    """
    started = time.monotonic()
    result = CollectResult(
        server_id=server.server_id,
        server_name=server.name,
        host=server.host,
    )

    client: paramiko.SSHClient | None = None
    try:
        client = paramiko.SSHClient()
        # 与旧版 "-o StrictHostKeyChecking=no" 等价。
        # 注意：这会跳过主机指纹校验，在不可信网络下存在中间人风险。
        # 生产环境建议改为 load_system_host_keys + RejectPolicy。
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        password = server.auth.resolve_password()
        key_path = server.auth.resolve_key_path()

        connect_kwargs: dict = {
            "hostname": server.host,
            "port": server.port,
            "username": server.username,
            "timeout": timeout,
            "allow_agent": True,
            "look_for_keys": key_path is None,
        }
        if password:
            connect_kwargs["password"] = password
        if key_path:
            connect_kwargs["key_filename"] = str(key_path)
            if server.auth.passphrase:
                connect_kwargs["passphrase"] = server.auth.passphrase

        try:
            client.connect(**connect_kwargs)
        except paramiko.AuthenticationException as exc:
            raise SSHAuthError(f"认证失败: {exc}") from exc
        except paramiko.SSHException as exc:
            msg = str(exc).lower()
            if "authentication" in msg or "auth" in msg:
                raise SSHAuthError(f"认证失败: {exc}") from exc
            raise SSHConnectError(f"SSH 协商失败: {exc}") from exc
        except TimeoutError as exc:
            raise SSHConnectError(f"连接超时 ({timeout}s)") from exc
        except OSError as exc:
            raise SSHConnectError(f"网络不可达: {exc}") from exc

        command = build_command(server.disk_paths)
        try:
            _stdin, stdout, stderr = client.exec_command(command, timeout=command_timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            exit_code = stdout.channel.recv_exit_status()
        except TimeoutError as exc:
            raise SSHConnectError(f"命令执行超时 ({command_timeout}s)") from exc

        if not out.strip():
            detail = err.strip() or f"命令无输出，退出码 {exit_code}"
            raise SSHConnectError(detail)

        result.online = True
        result.raw = out

    except SSHAuthError as exc:
        result.error = str(exc)
        result.error_kind = "auth"
    except SSHConnectError as exc:
        result.error = str(exc)
        result.error_kind = "connect"
    except Exception as exc:  # noqa: BLE001 - 兜底，避免单台主机异常拖垮整批
        result.error = f"{type(exc).__name__}: {exc}"
        result.error_kind = "unknown"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        result.elapsed_ms = int((time.monotonic() - started) * 1000)

    return result


def collect_with_retry(
    server: ServerConfig,
    timeout: int = 8,
    command_timeout: int = 15,
    retries: int = 2,
    retry_delay_seconds: int = 5,
    logger=None,
) -> CollectResult:
    """带重试的采集。

    跨公网连境外主机时偶发协商失败（如 paramiko 的 "No existing session"），
    不加重试会误报离线。认证失败不重试——重试也过不了，只会拖慢整批。

    抽成独立函数而非放在 Prefect task 内部，是为了让 CLI 的 check 命令
    也能复用同一套重试语义，避免"手动检查失败、定时跑却成功"的困惑。
    """
    result: CollectResult | None = None

    for attempt in range(retries + 1):
        result = collect(server, timeout=timeout, command_timeout=command_timeout)

        if result.online:
            return result

        if result.error_kind == "auth":
            if logger:
                logger.warning("%s 认证失败，跳过重试", server.label)
            break

        if attempt < retries:
            if logger:
                logger.info(
                    "%s 第 %d 次采集失败（%s），%d 秒后重试",
                    server.label, attempt + 1, result.error, retry_delay_seconds,
                )
            time.sleep(retry_delay_seconds)

    return result  # type: ignore[return-value]
