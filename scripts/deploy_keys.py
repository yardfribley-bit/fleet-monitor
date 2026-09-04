#!/usr/bin/env python3
"""把本机公钥部署到 config/servers.yaml 里列出的所有服务器。

目的：把明文密码认证升级为 SSH 密钥认证（安全建议第 2 条）。

用法：
    python scripts/deploy_keys.py [--pubkey ~/.ssh/fleet_monitor_ed25519.pub]

执行前请确认：
    1. config/servers.yaml 里的密码仍有效（本脚本用它做首次登录）
    2. 你已经看过本脚本逻辑，接受对每台服务器 ~/.ssh/authorized_keys 的追加写入

行为：
    - 幂等：目标机已存在该公钥则跳过
    - 只追加，不覆盖 authorized_keys
    - 部署后会用密钥重新连接验证
    - 认证失败的主机会跳过并报告，不影响其他主机

部署完成后，建议把 servers.yaml 中对应主机的 auth.password 改为：
    auth:
      key_path: "~/.ssh/fleet_monitor_ed25519"
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

# 允许以脚本方式运行时从项目根导入包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import paramiko  # noqa: E402

from fleet_monitor.config import load_config  # noqa: E402

AUTHKEYS_HEADER = "# fleet-monitor (deployed by scripts/deploy_keys.py)"


def _ssh_dir(client: paramiko.SSHClient) -> None:
    """确保 ~/.ssh 存在且权限正确。"""
    client.exec_command("mkdir -p ~/.ssh && chmod 700 ~/.ssh")[1].read()


def _read_authorized_keys(client: paramiko.SSHClient) -> str:
    _, out, _ = client.exec_command("cat ~/.ssh/authorized_keys 2>/dev/null")
    return out.read().decode("utf-8", errors="replace")


def _cleanup_stale(client: paramiko.SSHClient) -> None:
    """删除历史部署残留的 fleet-monitor 行（含早期 repr() 污染的脏行）。

    只精确匹配带本脚本标记的行，不触碰用户已有的其他公钥。
    """
    cmd = (
        "grep -v 'fleet-monitor (deployed by scripts/deploy_keys.py)' "
        "~/.ssh/authorized_keys > ~/.ssh/.ak.tmp 2>/dev/null; "
        "mv ~/.ssh/.ak.tmp ~/.ssh/authorized_keys 2>/dev/null; "
        "chmod 600 ~/.ssh/authorized_keys"
    )
    client.exec_command(cmd)[1].read()


def _append_key(client: paramiko.SSHClient, pubkey: str) -> None:
    # 用 base64 承载公钥块，彻底规避 shell 转义（换行符不会再被 repr 破坏）
    block = f"{AUTHKEYS_HEADER}\n{pubkey}\n"
    b64 = base64.b64encode(block.encode("utf-8")).decode("ascii")
    cmd = (
        f"echo {b64} | base64 -d >> ~/.ssh/authorized_keys && "
        "chmod 600 ~/.ssh/authorized_keys"
    )
    client.exec_command(cmd)[1].read()


def _verify_key_auth(server, timeout: int) -> None:
    """用 key_filename 方式验证密钥认证（比手动 from_private_key_file 更可靠）。"""
    v = paramiko.SSHClient()
    v.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        v.connect(
            server.host,
            port=server.port,
            username=server.username,
            key_filename=str(Path.home() / ".ssh/fleet_monitor_ed25519"),
            look_for_keys=False,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
        )
    finally:
        v.close()


def deploy_one(server, pubkey: str, timeout: int = 15) -> tuple[bool, str]:
    """把公钥部署到单台服务器，返回 (是否成功, 消息)。"""
    # 1) 用现有密码连接
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            server.host,
            port=server.port,
            username=server.username,
            password=server.auth.password,
            timeout=timeout,
            banner_timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"密码登录失败: {type(exc).__name__}: {exc}"

    try:
        _ssh_dir(client)
        # 先清掉历史污染行（早期 repr() bug 产生的脏行），再判断是否已存在
        _cleanup_stale(client)
        existing = _read_authorized_keys(client)
        if pubkey.strip() in existing:
            _verify_key_auth(server, timeout)
            return True, "公钥已存在，密钥认证验证通过"

        _append_key(client, pubkey)
        client.close()

        # 2) 用密钥验证
        _verify_key_auth(server, timeout)
        return True, "已部署，密钥登录验证通过"
    except Exception as exc:  # noqa: BLE001
        return False, f"部署后验证失败: {type(exc).__name__}: {exc}"
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="部署公钥到所有服务器")
    parser.add_argument("--pubkey", default="~/.ssh/fleet_monitor_ed25519.pub")
    parser.add_argument("-c", "--config", default=None)
    args = parser.parse_args()

    pubkey_path = Path(args.pubkey).expanduser()
    if not pubkey_path.exists():
        print(f"错误: 公钥不存在 {pubkey_path}，先运行: "
              "ssh-keygen -t ed25519 -f ~/.ssh/fleet_monitor_ed25519 -N ''")
        return 1
    pubkey = pubkey_path.read_text().strip()

    cfg = load_config(args.config)
    servers = cfg.enabled_servers
    print(f"将把公钥部署到 {len(servers)} 台服务器:\n")

    ok = 0
    for server in servers:
        success, msg = deploy_one(server, pubkey, timeout=cfg.collect.timeout_seconds)
        mark = "OK " if success else "FAIL"
        print(f"  [{mark}] {server.name:<16} {server.host:<18} {msg}")
        ok += success

    print(f"\n完成: {ok}/{len(servers)} 台部署成功")
    print("\n下一步：把 config/servers.yaml 中成功主机的密码改为密钥认证：")
    print('    auth:\n      key_path: "~/.ssh/fleet_monitor_ed25519"')
    return 0 if ok == len(servers) else 1


if __name__ == "__main__":
    raise SystemExit(main())
