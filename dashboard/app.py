"""Streamlit 可视化面板。

Prefect UI 看的是「编排运行状态」（哪些 flow 跑了、耗时、有没有失败），
这个面板看的是「业务指标本身」（CPU/内存曲线、谁从哪个 IP 登过服务器）。
两者是互补的双层可视化，不是替代关系。

启动: streamlit run dashboard/app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fleet_monitor.config import load_config  # noqa: E402
from fleet_monitor.storage import Storage  # noqa: E402

st.set_page_config(
    page_title="fleet-monitor · 主机巡检",
    page_icon="🖥️",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource
def get_storage() -> Storage:
    cfg = load_config()
    return Storage(cfg.storage.sqlite_path)


@st.cache_resource
def get_name_map() -> dict[str, str]:
    cfg = load_config()
    return {s.server_id: s.name for s in cfg.servers}


@st.cache_data(ttl=30)
def load_latest() -> pd.DataFrame:
    rows = get_storage().latest_statuses()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    name_map = get_name_map()
    df["name"] = df["server_id"].map(name_map).fillna(df["server_id"])
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    return df


@st.cache_data(ttl=60)
def load_history(server_id: str, hours: int) -> pd.DataFrame:
    rows = get_storage().history(server_id, hours=hours)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    return df.sort_values("timestamp")


def severity_color(value: float, warn: float = 80.0, crit: float = 90.0) -> str:
    if value >= crit:
        return "🔴"
    if value >= warn:
        return "🟡"
    return "🟢"


def main() -> None:
    st.title("🖥️ fleet-monitor · 主机巡检")

    df = load_latest()
    if df.empty:
        st.warning("暂无数据。请先运行采集：`python -m fleet_monitor.cli run`")
        st.stop()

    # ---------- 概览指标 ----------
    total = len(df)
    online = int(df["online"].sum())
    offline = total - online
    alerts = len(get_storage().alert_history(hours=24))

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("纳管主机", total)
    col2.metric("在线", online, delta=None if offline == 0 else f"-{offline}")
    col3.metric("离线", offline)
    col4.metric("近 24h 告警", alerts)

    updated = df["timestamp"].max()
    if pd.notna(updated):
        st.caption(f"最后更新: {updated:%Y-%m-%d %H:%M:%S} UTC · 每 30 分钟自动刷新")

    # ---------- 主机总览表 ----------
    st.subheader("主机总览")
    view = df[
        ["name", "host", "online", "cpu_percent", "mem_percent",
         "disk_percent", "load1", "elapsed_ms", "error"]
    ].copy()
    view["状态"] = view["online"].map({1: "🟢 在线", 0: "🔴 离线"})
    view["CPU %"] = view["cpu_percent"].round(1)
    view["内存 %"] = view["mem_percent"].apply(lambda v: f"{severity_color(v)} {v:.1f}")
    view["磁盘 %"] = view["disk_percent"].apply(lambda v: f"{severity_color(v)} {v:.1f}")
    view["负载"] = view["load1"].round(2)
    view["耗时 ms"] = view["elapsed_ms"]
    view["备注"] = view["error"].fillna("")

    st.dataframe(
        view[["name", "host", "状态", "CPU %", "内存 %", "磁盘 %", "负载", "耗时 ms", "备注"]],
        use_container_width=True,
        hide_index=True,
    )

    tab_trend, tab_login, tab_proc, tab_alert = st.tabs(
        ["📈 资源趋势", "🔐 SSH 登录审计", "⚙️ Top 进程", "🔔 告警历史"]
    )

    # ---------- 趋势 ----------
    with tab_trend:
        name_map = get_name_map()
        options = {v: k for k, v in name_map.items()}
        selected = st.selectbox("选择主机", list(options.keys()))
        server_id = options[selected]

        hours = st.slider("时间范围（小时）", min_value=1, max_value=168, value=24, step=1)
        hist = load_history(server_id, hours)

        if hist.empty:
            st.info("该主机在此时间范围内暂无数据")
        else:
            metrics = st.multiselect(
                "指标",
                ["cpu_percent", "mem_percent", "disk_percent", "swap_percent", "load1"],
                default=["cpu_percent", "mem_percent", "disk_percent"],
                format_func=lambda x: {
                    "cpu_percent": "CPU 使用率 %",
                    "mem_percent": "内存使用率 %",
                    "disk_percent": "磁盘使用率 %",
                    "swap_percent": "Swap 使用率 %",
                    "load1": "1 分钟负载",
                }[x],
            )
            if metrics:
                fig = px.line(
                    hist, x="timestamp", y=metrics,
                    labels={"value": "数值", "variable": "指标", "timestamp": "时间"},
                    title=f"{selected} 资源趋势（近 {hours} 小时）",
                )
                fig.update_layout(height=420, hovermode="x unified")
                st.plotly_chart(fig, use_container_width=True)

            latest = hist.iloc[-1]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("CPU", f"{latest['cpu_percent']:.1f}%")
            c2.metric("内存", f"{latest['mem_percent']:.1f}%")
            c3.metric("磁盘", f"{latest['disk_percent']:.1f}%")
            c4.metric("负载", f"{latest['load1']:.2f}")

    # ---------- SSH 登录审计 ----------
    with tab_login:
        st.markdown("**历史登录记录**（来自 `last` 与 `who`）")
        logins = get_storage().login_history(limit=300)
        if not logins:
            st.info("暂无登录记录")
        else:
            ldf = pd.DataFrame(logins)
            name_map = get_name_map()
            ldf["主机"] = ldf["server_id"].map(name_map).fillna(ldf["server_id"])
            ldf["登录时间"] = pd.to_datetime(
                ldf["observed_at"], errors="coerce", utc=True
            )

            col_a, col_b = st.columns([3, 2])
            with col_a:
                st.dataframe(
                    ldf[["主机", "username", "from_ip", "login_time", "duration", "source"]]
                    .rename(columns={
                        "username": "用户", "from_ip": "来源 IP",
                        "login_time": "登录时刻", "duration": "时长", "source": "来源",
                    }),
                    use_container_width=True,
                    hide_index=True,
                    height=420,
                )
            with col_b:
                st.markdown("**来源 IP 分布**")
                ip_counts = (
                    ldf[ldf["from_ip"].astype(str).str.match(r"^\d+\.\d+\.\d+\.\d+$")]
                    ["from_ip"].value_counts().head(15).reset_index()
                )
                ip_counts.columns = ["IP", "次数"]
                if not ip_counts.empty:
                    st.dataframe(ip_counts, use_container_width=True, hide_index=True)
                else:
                    st.caption("未发现 IPv4 来源记录")

            st.markdown("**已登记 IP**（首次出现后不再重复告警）")
            known = get_storage().known_ips()
            if known:
                kdf = pd.DataFrame(known)
                kdf["主机"] = kdf["server_id"].map(name_map).fillna(kdf["server_id"])
                st.dataframe(
                    kdf[["主机", "ip", "first_seen", "last_seen"]].rename(columns={
                        "ip": "IP", "first_seen": "首次出现", "last_seen": "最近出现",
                    }),
                    use_container_width=True,
                    hide_index=True,
                )

    # ---------- Top 进程 ----------
    with tab_proc:
        st.markdown("**各主机内存占用最高的进程**")
        for _, row in df[df["online"] == 1].iterrows():
            with st.expander(f"{row['name']} ({row['host']})", expanded=False):
                detail = row.get("detail_json")
                if not detail:
                    st.caption("无进程明细")
                    continue
                import json
                try:
                    data = json.loads(detail)
                except (TypeError, ValueError):
                    st.caption("进程明细解析失败")
                    continue

                procs = data.get("top_processes", [])
                if procs:
                    pdf = pd.DataFrame(procs)
                    st.dataframe(
                        pdf.rename(columns={
                            "business": "业务", "comm": "进程",
                            "mem_percent": "内存 %", "cpu_percent": "CPU %", "pid": "PID",
                        }),
                        use_container_width=True,
                        hide_index=True,
                    )
                else:
                    st.caption("无进程数据")

                disks = data.get("disks", [])
                if disks:
                    st.caption("磁盘挂载点")
                    st.dataframe(
                        pd.DataFrame(disks).rename(columns={
                            "mount": "挂载点", "percent": "使用率 %",
                            "used_gb": "已用 GB", "total_gb": "总量 GB",
                        }),
                        use_container_width=True,
                        hide_index=True,
                    )

    # ---------- 告警 ----------
    with tab_alert:
        hours = st.slider("告警时间范围（小时）", 1, 720, 168, key="alert_hours")
        rows = get_storage().alert_history(hours=hours)
        if not rows:
            st.success("该时间范围内无告警")
        else:
            adf = pd.DataFrame(rows)
            adf["created_at"] = pd.to_datetime(adf["created_at"], errors="coerce", utc=True)
            adf = adf.sort_values("created_at", ascending=False)

            c1, c2 = st.columns([1, 3])
            with c1:
                st.markdown("**按类别**")
                st.dataframe(
                    adf["category"].value_counts().reset_index().rename(
                        columns={"index": "类别", "category": "次数"}
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
            with c2:
                st.dataframe(
                    adf[["created_at", "server_name", "category", "severity", "message"]]
                    .rename(columns={
                        "created_at": "时间", "server_name": "主机",
                        "category": "类别", "severity": "级别", "message": "内容",
                    }),
                    use_container_width=True,
                    hide_index=True,
                    height=420,
                )


if __name__ == "__main__":
    main()
