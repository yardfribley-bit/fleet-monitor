"""FleetMonitor 桌面应用：原生窗口包装面板。

双击 FleetMonitor.app 后：
1. 检查面板 http://127.0.0.1:8501 是否就绪
2. 未就绪则拉起后台服务（prefect server + 常驻调度 + Streamlit）
3. 用 pywebview 打开原生窗口显示面板

窗口只是「查看器」：关闭窗口不影响采集（采集由 launchd 常驻服务负责）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import webview

PROJECT = Path(__file__).resolve().parents[1]
PANEL_URL = "http://127.0.0.1:8501"
TITLE = "fleet-monitor · 主机巡检"


def _panel_ready(timeout: float = 2.0) -> bool:
    try:
        urllib.request.urlopen(PANEL_URL, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 - 未就绪一律视为 False
        return False


def _ensure_services() -> None:
    """面板未就绪时拉起后台服务，最多等待约 60 秒。"""
    if _panel_ready():
        return

    env = os.environ.copy()
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    subprocess.Popen(
        ["bash", str(PROJECT / "scripts" / "start.sh"), "-d", "-s"],
        cwd=str(PROJECT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    for _ in range(120):  # 120 × 0.5s = 60s
        if _panel_ready():
            return
        time.sleep(0.5)


def main() -> None:
    _ensure_services()
    webview.create_window(
        TITLE,
        PANEL_URL,
        width=1320,
        height=860,
        min_size=(900, 600),
    )
    webview.start()


if __name__ == "__main__":
    sys.exit(main())
