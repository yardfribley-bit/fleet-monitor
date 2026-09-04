# fleet-monitor

基于 **Prefect** 的多主机健康巡检与 SSH 登录审计系统。

定时采集各主机的 CPU / 内存 / 磁盘 / 负载 / Top 进程 / SSH 登录来源，
写入 SQLite，超阈值自动告警，并提供 Web 可视化面板。

> A Prefect-based fleet health monitor and SSH login audit tool.
> It periodically collects CPU / memory / disk / load / top processes / SSH login
> sources from your servers, stores them in SQLite, raises threshold alerts,
> and ships a web dashboard.

---

## 为什么替换 DeepSeekBalance / Why replace DeepSeekBalance

原 [DeepSeekBalance](https://github.com/tajleonbennis-maker/deepseek-balance) 是
4724 行 Swift 写的 macOS 菜单栏 App，服务器监控只是它的一个模块。它有三个硬伤：

| 问题 | 旧版 | 本方案 |
|---|---|---|
| Mac 关机即无数据 | README 明列为已知限制 | 部署在常开主机上，7×24 连续采集 |
| 只能本机运行 | macOS App，换台机器要重装 | Python 服务，任何 Linux/macOS 都能跑 |
| 扩展成本高 | 加指标要改 Swift 并重编译 | 加指标改 Python 即可，无需编译 |
| 采集频率 | 固定每小时 | 配置 cron，默认每 30 分钟 |
| 凭据处理 | 明文密码 + `SSH_ASKPASS` 脚本 hack | paramiko 直连，支持密钥与环境变量 |
| CPU 指标 | 只有 load average，**没有真实 CPU%** | 采集真实 CPU 使用率 |

> 保留的能力：DeepSeek 余额监控、服务器 AI 助手属于另一个功能域，
> 本方案只接管**服务器监控与登录审计**部分。

---

## 架构 / Architecture

```
config/servers.yaml  →  Prefect Flow (cron */30 * * * *)
                              ↓
                    并发 SSH 采集 (paramiko, 带重试)
                              ↓
                    解析 → SQLite → 告警判定
                              ↓
        ┌─────────────────────┴─────────────────────┐
   Streamlit 面板                              Prefect UI
   :8501 指标曲线/登录审计/告警                :4200 编排状态/日志/重试
```

**并发策略**：只有 SSH 采集并发（慢 IO），SQLite 写入与告警判定串行，
避免并发读写导致同一 IP 被重复判定为「新 IP」。

> Concurrency: only SSH collection runs in parallel (slow I/O).
> SQLite writes and alert evaluation are serial to avoid race conditions
> that would double-report a "new IP".

---

## 快速开始 / Quick start

```bash
cd /Users/jatsmith/WorkBuddy/code/fleet-monitor

# 1) 连通性体检（不写数据库，先确认能连上）
PYTHONPATH=src .venv/bin/python -m fleet_monitor.cli check -v

# 2) 立即采集一次
PYTHONPATH=src .venv/bin/python -m fleet_monitor.cli run
```

**一键启动（推荐）**：封装了「prefect server + serve」两步，内部自动处理
NO_PROXY 与端口探测：

```bash
./scripts/start.sh          # 前台运行
./scripts/start.sh -d       # 后台运行
./scripts/start.sh -d -s    # 额外启动 Streamlit 面板 (:8501)
```

> **为什么必须两步**：Prefect 3.x 的 `serve` 若连不到真实 server 会退回
> ephemeral（内存）模式，而 ephemeral 模式**无法调度**——cron 不会真正触发。
> 所以手动启动时，必须先起 `prefect server`，再起 `serve`：

```bash
# 终端 A：编排 server（API + 调度器 + UI）—— http://localhost:4200
.venv/bin/prefect server start

# 终端 B：runner（创建 deployment 并轮询调度）
export PREFECT_API_URL="http://127.0.0.1:4200/api"
PYTHONPATH=src .venv/bin/python -m fleet_monitor.cli serve

# 可选：指标面板 —— http://localhost:8501
.venv/bin/streamlit run dashboard/app.py
```

---

## 配置 / Configuration

复制模板后填写：

```bash
cp config/servers.example.yaml config/servers.yaml
chmod 600 config/servers.yaml
```

或从旧 App 自动迁移（含 5 台服务器与历史数据）：

```bash
python scripts/migrate_from_deepseekbalance.py --with-history
```

### 认证方式（三选一）

```yaml
servers:
  - name: "推荐-密钥"
    host: "1.2.3.4"
    username: "root"
    auth:
      key_path: "~/.ssh/id_ed25519"

  - name: "推荐-环境变量"
    host: "1.2.3.5"
    username: "ubuntu"
    auth:
      password_env: "NODE_B_PASSWORD"   # 从环境变量读取

  - name: "不推荐-明文"
    host: "1.2.3.6"
    username: "root"
    auth:
      password: "change-me"
```

### 告警阈值

| 项 | 默认值 | 说明 |
|---|---|---|
| `memory_percent` | 90 | 内存使用率 |
| `disk_percent` | 85 | 磁盘使用率 |
| `swap_percent` | 80 | Swap 使用率 |
| `load_per_core` | 4.0 | 每核负载（按核数归一化，跨机型可比） |
| `offline` | true | 主机离线 |
| `new_login_ip` | true | 出现新的 SSH 来源 IP |
| `cooldown_minutes` | 60 | 同类告警去重窗口 |

**新 IP 采用基线静默**：某台主机首次采集时只登记 IP 不告警，
否则第一次运行会把 `last` 里的历史登录全部误报成新 IP。

---

## 命令 / Commands

| 命令 | 作用 |
|---|---|
| `check [-v]` | 连通性体检，采集并打印，不写数据库 |
| `run` | 立即执行一次完整采集 + 落库 + 告警 |
| `serve` | 常驻进程，按 cron 定时触发 |
| `cleanup [--days N]` | 清理超出保留期的历史数据 |

---

## 可视化 / Dashboards

两层可视化，互补而非替代：

| 面板 | 端口 | 看什么 |
|---|---|---|
| **Prefect UI** | 4200 | 编排：哪些 flow 跑了、耗时、失败重试、运行日志 |
| **Streamlit** | 8501 | 业务：CPU/内存/磁盘曲线、SSH 登录审计、Top 进程、告警历史 |

Streamlit 面板包含 4 个标签页：资源趋势、SSH 登录审计、Top 进程、告警历史。
登录审计会列出每个来源 IP 的登录次数，以及已登记 IP（首次出现后不再重复告警）。

---

## 生产部署 / Production deployment

建议部署到一台**常开**的主机上（任意一台云主机即可，可以监控包括自己在内的所有机器）。

```bash
# 服务器上
git clone <your-repo> /opt/fleet-monitor
cd /opt/fleet-monitor
uv venv .venv && uv pip install -e .
cp config/servers.example.yaml config/servers.yaml && chmod 600 config/servers.yaml
```

**systemd 双服务**（`deploy/systemd/` 已提供，两个单元缺一不可——
`fleet-monitor.service` 依赖 `fleet-monitor-server.service`）：

```bash
cp deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now fleet-monitor-server fleet-monitor
journalctl -u fleet-monitor -f
```

> 两个进程的分工：`fleet-monitor-server` 跑 `prefect server`（调度器 + UI），
> `fleet-monitor` 跑 `serve`（runner，轮询调度并执行采集）。只启动其中一个，
> 定时采集不会工作。

**macOS 本机**（launchd，`deploy/launchd/` 已提供）：

```bash
cp deploy/launchd/com.fleetmonitor.agent.plist ~/Library/LaunchAgents/
# 编辑 plist，把两个路径改成你的实际项目路径
launchctl load ~/Library/LaunchAgents/com.fleetmonitor.agent.plist
```

> 注意：Mac 休眠/关机期间采集会中断（与旧版 DeepSeekBalance 相同限制），
> 如需 7×24 连续采集请部署到常开主机。

---

### 部署位置决定能采集到谁

fleet-monitor 只能采集**它自己能 SSH 到**的目标。你的服务器里有内网机器
（如 192.168.1.39），这点要特别注意：

| 部署位置 | 能采集 | 采不到 |
|---|---|---|
| 家里常开主机（与 .39 同内网） | 公网云主机 + 家里服务器 | —— |
| 公网云主机（如 CVM） | 其他公网云主机 | 内网的 192.168.1.39 |
| 本机 Mac（临时验证） | 同网段目标 | Mac 关机即断 |

若必须从公网采集内网机器，需另配 VPN / 内网穿透 / 跳板机，超出本项目范围。

---

## 安全建议 / Security notes

1. **配置文件绝不入库** —— `config/servers.yaml` 已被 `.gitignore` 排除，迁移脚本自动设为 600 权限。
2. **优先用 SSH 密钥** —— 迁移生成的是明文密码，建议尽快换成 `auth.key_path`。
   一键升级（幂等，用现有密码登录后追加公钥，部署后用密钥验证）：
   ```bash
   python scripts/deploy_keys.py        # 先确认 servers.yaml 里密码仍有效
   # 然后把对应主机的 auth 改为 key_path: "~/.ssh/fleet_monitor_ed25519"
   ```
3. **主机指纹校验** —— 当前用 `AutoAddPolicy`（等价旧版 `-o StrictHostKeyChecking=no`），
   会跳过指纹校验。在不可信网络下应改为 `load_system_host_keys` + `RejectPolicy`。
4. **告警推送** —— 配置 `notify.webhook_url` 可推送到企业微信 / Telegram / Server酱，
   未配置则只写日志与数据库。

---

## 故障排查 / Troubleshooting

| 现象 | 原因与处理 |
|---|---|
| `No existing session` | 跨公网偶发协商失败，重试机制会自动恢复；持续失败则调大 `collect.retries` |
| `网络不可达 / Network is unreachable` | 采集端与目标主机不在同一网络（如家里服务器需在内网） |
| `认证失败` | 密码错误或密钥路径不对，这类失败不重试，需改配置 |
| `命令执行超时` | 目标机负载过高（实测 node-c 曾达 19.8s），调大 `collect.command_timeout` |
| `SOCKS proxy requires python-socks` | 本机开着系统 SOCKS 代理且 bypass 未含 127.0.0.1，本地连接被误判走代理。用 `scripts/start.sh` 会自动注入 `NO_PROXY`，或手动 `export NO_PROXY=127.0.0.1,localhost` |
| `Cannot schedule flows on an ephemeral server` | `serve` 没连到真实 server。先 `prefect server start`，并设 `PREFECT_API_URL` |
| 磁盘告警但 `df` 显示正常 | 检查 `disk_paths` 是否覆盖了实际使用中的挂载点 |
| 趋势图无数据 | 先跑一次 `run` 产生快照；旧版历史数据不含 CPU 使用率 |

---

## 目录结构 / Layout

```
fleet-monitor/
├── config/
│   ├── servers.yaml            # 实际配置（gitignore, 600）
│   └── servers.example.yaml    # 模板
├── src/fleet_monitor/
│   ├── config.py        # YAML 配置加载与校验
│   ├── collector.py     # paramiko SSH 采集与重试
│   ├── parser.py        # 原始输出解析
│   ├── business.py      # 进程 → 业务名映射
│   ├── storage.py       # SQLite 存储
│   ├── alerts.py        # 告警判定与通知
│   ├── flows.py         # Prefect flow 定义
│   └── cli.py           # 命令行入口
├── dashboard/app.py     # Streamlit 可视化
├── scripts/
│   ├── migrate_from_deepseekbalance.py
│   └── start.sh         # 一键启动（prefect server + serve + 可选 streamlit）
├── deploy/
│   ├── systemd/         # Linux 常驻：双服务单元
│   └── launchd/         # macOS 常驻：LaunchAgent plist
└── data/fleet.db        # SQLite 数据库（gitignore）
```

## License

MIT
