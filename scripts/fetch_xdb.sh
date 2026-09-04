#!/usr/bin/env bash
# 下载 ip2region 离线 IP 数据库到 data/ip2region.xdb
# （首次克隆后跑一次，后续 dashboard 启动会读取并常驻内存）
#
# 用法: ./scripts/fetch_xdb.sh

set -euo pipefail
cd "$(dirname "$0")/.."

OUT="data/ip2region.xdb"
mkdir -p data

if [ -f "$OUT" ] && [ "$(stat -f%z "$OUT" 2>/dev/null || stat -c%s "$OUT")" -gt 1000000 ]; then
  echo "[跳过] 已存在 $OUT ($(du -h "$OUT" | cut -f1))"
  exit 0
fi

URL="https://raw.githubusercontent.com/lionsoul2014/ip2region/master/data/ip2region_v4.xdb"
PROXY="${PROXY:-http://127.0.0.1:1087}"
echo "下载 ip2region.xdb (约 11MB) ..."
if curl -fsSL --proxy "$PROXY" -o "$OUT" "$URL" 2>/dev/null; then
  echo "[完成] $OUT ($(du -h "$OUT" | cut -f1))"
elif curl -fsSL -o "$OUT" "$URL" 2>/dev/null; then
  echo "[完成] $OUT ($(du -h "$OUT" | cut -f1))"
else
  echo "[失败] 请检查网络/代理。手动下载: $URL -> $OUT"
  exit 1
fi