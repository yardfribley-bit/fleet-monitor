"""IP 地理位置解析（离线 ip2region xdb v4）。

数据文件: data/ip2region.xdb（来自 ip2region_v4.xdb，约 11MB，只含 IPv4）
返回格式: 国家|区域|省份|城市|ISP，例如 "中国|0|广东省|深圳市|电信"

选择离线 xdb 而非在线 API 的理由：
1. 零网络依赖，采集与面板离线可用，不受 API 限流 / 代理抖动影响
2. 免费、无需注册 key
3. 查询在内存中微秒级完成，面板每次加载几百个 IP 无压力
4. 对中国大陆 IP 精确到省/市/运营商；海外 IP 通常到国家，部分到城市

局限：海外 IP 精度弱于 MaxMind GeoLite2，若需城市级海外定位可后续叠加在线 API。

xdb v4 文件格式（小端）：
  0..256        header（version/indexPolicy/createdAt/startIndexPtr/endIndexPtr/...）
  256..524544   vector index：二维 [IP首段×256 + IP次段] 定位，每项 8 字节(s_ptr, e_ptr)
  segment index：每项 14 字节 [start_ip(4) end_ip(4) data_len(2) data_ptr(4)]
"""

from __future__ import annotations

import ipaddress
import threading
from pathlib import Path

XDB_DEFAULT = Path(__file__).resolve().parents[2] / "data" / "ip2region.xdb"

HEADER_LEN = 256
VECTOR_LEN = 256 * 256 * 8  # 524288
INDEX_SIZE = 14  # IPv4 segment index 条目字节数


def _le16(buf: bytes, off: int) -> int:
    return buf[off] | (buf[off + 1] << 8)


def _le32(buf: bytes, off: int) -> int:
    return (
        buf[off]
        | (buf[off + 1] << 8)
        | (buf[off + 2] << 16)
        | (buf[off + 3] << 24)
    )


def _v4_cmp(ip_bytes: bytes, buf: bytes, off: int) -> int:
    """比较大端 ip_bytes 与 buf 中 off 起的小端编码 4 字节 IP。"""
    j = off + 3
    for i in range(4):
        if ip_bytes[i] < buf[j]:
            return -1
        if ip_bytes[i] > buf[j]:
            return 1
        j -= 1
    return 0


def _special_net(ip: str) -> str | None:
    """判断是否内网/回环等非公网地址，命中则返回可读标签。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.is_loopback:
        return "本机回环"
    if addr.is_private:
        return "内网地址"
    if addr.is_link_local:
        return "链路本地地址"
    if addr.is_multicast:
        return "组播地址"
    if addr.is_reserved or addr.is_unspecified:
        return "保留地址"
    return None


class _Searcher:
    """ip2region xdb 查询器（整文件读入内存，查询无状态、线程安全）。"""

    def __init__(self, db_path: Path):
        with open(db_path, "rb") as f:
            self._buf = f.read()
        self._start_ptr = _le32(self._buf, 8)
        self._end_ptr = _le32(self._buf, 12)
        self._vector = self._buf[HEADER_LEN:HEADER_LEN + VECTOR_LEN]

    def search(self, ip: str) -> str:
        """返回原始 region 字符串，如 "中国|0|广东省|深圳市|电信"。"""
        a, b, c, d = (int(x) for x in ip.split("."))
        ip_bytes = bytes((a, b, c, d))

        idx = a * 256 * 8 + b * 8
        s_ptr = _le32(self._vector, idx)
        e_ptr = _le32(self._vector, idx + 4)
        if s_ptr == 0 or e_ptr == 0:
            return ""

        lo, hi = 0, (e_ptr - s_ptr) // INDEX_SIZE
        d_len = d_ptr = 0
        while lo <= hi:
            mid = (lo + hi) >> 1
            p = s_ptr + mid * INDEX_SIZE
            seg = self._buf[p:p + INDEX_SIZE]
            if _v4_cmp(ip_bytes, seg, 0) < 0:
                hi = mid - 1
            elif _v4_cmp(ip_bytes, seg, 4) > 0:
                lo = mid + 1
            else:
                d_len = _le16(seg, 8)
                d_ptr = _le32(seg, 10)
                break

        if d_len == 0:
            return ""
        return self._buf[d_ptr:d_ptr + d_len].decode("utf-8", errors="replace")


_searcher: _Searcher | None = None
_lock = threading.Lock()


def _get_searcher(db_path: Path | None = None) -> _Searcher:
    global _searcher
    if _searcher is None:
        with _lock:
            if _searcher is None:
                _searcher = _Searcher(db_path or XDB_DEFAULT)
    return _searcher


# ISO 3166-1 alpha-2 国家码 → 中文名。xdb 里海外国家名是英文，
# 用国家码映射统一成中文；未覆盖的国家码回退到原文。
_CN_NAME = {
    "CN": "中国", "HK": "中国香港", "MO": "中国澳门", "TW": "中国台湾",
    "US": "美国", "CA": "加拿大", "MX": "墨西哥",
    "GB": "英国", "FR": "法国", "DE": "德国", "IT": "意大利", "ES": "西班牙",
    "PT": "葡萄牙", "NL": "荷兰", "BE": "比利时", "LU": "卢森堡", "CH": "瑞士",
    "AT": "奥地利", "SE": "瑞典", "NO": "挪威", "DK": "丹麦", "FI": "芬兰",
    "IS": "冰岛", "IE": "爱尔兰", "PL": "波兰", "CZ": "捷克", "SK": "斯洛伐克",
    "HU": "匈牙利", "RO": "罗马尼亚", "BG": "保加利亚", "GR": "希腊",
    "RU": "俄罗斯", "UA": "乌克兰", "BY": "白俄罗斯", "EE": "爱沙尼亚",
    "LV": "拉脱维亚", "LT": "立陶宛", "HR": "克罗地亚", "SI": "斯洛文尼亚",
    "RS": "塞尔维亚", "AL": "阿尔巴尼亚", "MD": "摩尔多瓦",
    "JP": "日本", "KR": "韩国", "KP": "朝鲜", "MN": "蒙古",
    "SG": "新加坡", "MY": "马来西亚", "TH": "泰国", "VN": "越南",
    "PH": "菲律宾", "ID": "印度尼西亚", "MM": "缅甸", "KH": "柬埔寨",
    "LA": "老挝", "BN": "文莱", "IN": "印度", "PK": "巴基斯坦",
    "BD": "孟加拉国", "LK": "斯里兰卡", "NP": "尼泊尔", "AF": "阿富汗",
    "KZ": "哈萨克斯坦", "UZ": "乌兹别克斯坦", "KG": "吉尔吉斯斯坦",
    "TJ": "塔吉克斯坦", "IR": "伊朗", "IQ": "伊拉克", "SA": "沙特阿拉伯",
    "AE": "阿联酋", "IL": "以色列", "TR": "土耳其", "JO": "约旦",
    "LB": "黎巴嫩", "QA": "卡塔尔", "KW": "科威特", "OM": "阿曼",
    "YE": "也门", "AM": "亚美尼亚", "AZ": "阿塞拜疆", "GE": "格鲁吉亚",
    "BR": "巴西", "AR": "阿根廷", "CL": "智利", "PE": "秘鲁",
    "CO": "哥伦比亚", "VE": "委内瑞拉", "EC": "厄瓜多尔", "BO": "玻利维亚",
    "PY": "巴拉圭", "UY": "乌拉圭",
    "ZA": "南非", "EG": "埃及", "NG": "尼日利亚", "KE": "肯尼亚",
    "ET": "埃塞俄比亚", "MA": "摩洛哥", "DZ": "阿尔及利亚", "TN": "突尼斯",
    "GH": "加纳", "TZ": "坦桑尼亚", "AO": "安哥拉",
    "AU": "澳大利亚", "NZ": "新西兰",
    "CU": "古巴", "DO": "多米尼加", "GT": "危地马拉", "CR": "哥斯达黎加",
    "PA": "巴拿马", "JM": "牙买加",
}


def locate(ip: str | None, db_path: Path | None = None) -> str:
    """把 IP 转成可读地理位置（国家名中文化）。

    返回示例：
      "中国 江苏省 南京市"
      "美国 California Santa Clara"
      "英国 England London"
      "内网地址" / "本机回环"
    无法解析或数据缺失时返回空字符串。
    """
    if not ip:
        return ""
    ip = str(ip).strip()
    if not ip:
        return ""

    special = _special_net(ip)
    if special:
        return special

    try:
        raw = _get_searcher(db_path).search(ip)
    except Exception:  # noqa: BLE001 - 单条解析失败不影响整体
        return ""

    # xdb v4 返回 5 段: 国家|省份|城市|ISP|国家码
    fields = raw.split("|")
    if len(fields) < 5 or not fields[0]:
        return ""

    country, region, city, isp, code = fields[0], fields[1], fields[2], fields[3], fields[4]

    # 国家名：优先用国家码映射成中文，映射不到且原文已是中文则保留原文
    cn = _CN_NAME.get(code)
    if cn:
        name = cn
    else:
        name = country

    parts = [name]
    for seg in (region, city):
        if seg and seg != "0":
            parts.append(seg)
    if isp and isp != "0":
        parts.append(isp)
    return " ".join(parts)
