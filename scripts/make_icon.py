"""生成 FleetMonitor.app 的 AppIcon.icns（监控波形风格图标）。

用 Pillow 画一个 1024×1024 主图（深色圆角底 + 绿色趋势折线），
再生成 macOS iconset 各尺寸，最后用 iconutil 转成 icns。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
BG = (31, 41, 55, 255)       # 深灰蓝
LINE = (34, 197, 94, 255)    # 绿色
DOT = (255, 255, 255, 255)   # 白色节点
ALERT = (248, 113, 113, 255)  # 红色告警点


def _draw_master() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角矩形背景
    d.rounded_rectangle([0, 0, SIZE, SIZE], radius=SIZE // 5, fill=BG)

    # 趋势折线（监控波形，左下 → 右上，带一个尖峰）
    pts = [
        (150, 740), (300, 560), (430, 640), (560, 380),
        (690, 470), (800, 320), (880, 360),
    ]
    d.line(pts, fill=LINE, width=58, joint="curve")

    # 折线端点圆角
    for x, y in pts:
        d.ellipse([x - 34, y - 34, x + 34, y + 34], fill=LINE)

    # 白色节点
    for x, y in pts:
        d.ellipse([x - 22, y - 22, x + 22, y + 22], fill=DOT)

    # 红色告警点（尖峰顶部）
    d.ellipse([560 - 40, 380 - 40, 560 + 40, 380 + 40], fill=ALERT)

    return img


def _make_iconset(master: Image.Image, iconset_dir: Path) -> None:
    spec = [
        (16, "icon_16x16.png", 16),
        (32, "icon_16x16@2x.png", 32),
        (32, "icon_32x32.png", 32),
        (64, "icon_32x32@2x.png", 64),
        (128, "icon_128x128.png", 128),
        (256, "icon_128x128@2x.png", 256),
        (256, "icon_256x256.png", 256),
        (512, "icon_256x256@2x.png", 512),
        (512, "icon_512x512.png", 512),
        (1024, "icon_512x512@2x.png", 1024),
    ]
    for _, name, px in spec:
        master.resize((px, px), Image.LANCZOS).save(iconset_dir / name)


def main(output: str) -> int:
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)

    master = _draw_master()
    tmp = Path(tempfile.mkdtemp(prefix="fleetmonitor-", suffix=".iconset"))
    try:
        _make_iconset(master, tmp)
        subprocess.run(
            ["iconutil", "-c", "icns", str(tmp), "-o", str(out)],
            check=True,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"已生成图标: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "dist/FleetMonitor.app/Contents/Resources/AppIcon.icns"))
