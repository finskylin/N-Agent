"""
USGS Explorer Skill
USGS 卫星数据搜索 — M2M API + Playwright 降级

数据来源:
- USGS M2M API (需 USGS_M2M_TOKEN)
- Playwright 爬取 earthexplorer.usgs.gov (降级方案)
"""
import asyncio
from typing import Dict, Any, List
from datetime import datetime


# ============================================================
# --- inlined from _shared/geopolitical_common.py ---
# ============================================================

"""
Geopolitical Common - 大国博弈分析共享 HTTP 工具模块
所有地缘政治分析 Skill 复用的异步 HTTP 请求函数。

数据源:
- GDELT (免费, 无 Key)
- World Bank (免费, 无 Key)
- FRED (免费, 需 Key)
- IMF Data Mapper (免费, 无 Key)
- Semantic Scholar (免费, 有限速)
- ACLED 冲突事件 (免费, 需 Key)
- YouTube Data API (免费, 需 Key)
- IMF COFER 外汇储备 (免费, 无 Key)
- OECD SDMX (免费, 无 Key)
- SIPRI 军费数据 (免费, Excel 下载)
- GlobalFirepower (免费, HTML 抓取)
- UN 大会投票记录 (免费, GitHub CSV)
- NATO 新闻 (免费, RSS/HTML)
- Cloudflare Radar (免费, 无 Key)
- USGS 矿产数据 (免费, 无 Key)
"""
import csv
import io
import json
import os
import asyncio
import logging
import re
from typing import Dict, Any, List, Optional
from urllib.parse import urlencode, quote_plus

import aiohttp

logger = logging.getLogger(__name__)

# 超时配置
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)

# 数据源时效性分类
DATA_SOURCE_TYPES = {
    "GDELT": {"type": "准实时", "update_freq": "每15分钟", "description": "GDELT全球事件数据库"},
    "FRED": {"type": "实时", "update_freq": "每日", "description": "美联储经济数据库"},
    "World_Bank": {"type": "滞后", "update_freq": "年度", "lag": "1-2年", "description": "世界银行发展指标"},
    "IMF": {"type": "滞后", "update_freq": "季度/年度", "lag": "3-12个月", "description": "IMF宏观经济数据"},
    "UN_Comtrade": {"type": "滞后", "update_freq": "年度", "lag": "6-18个月", "description": "联合国贸易统计"},
    "Semantic_Scholar": {"type": "滞后", "update_freq": "持续收录", "lag": "论文发布后数天", "description": "学术论文数据库"},
    "MITRE_ATT_CK": {"type": "滞后", "update_freq": "半年度", "description": "MITRE ATT&CK网络威胁知识框架"},
    "ACLED": {"type": "准实时", "update_freq": "每周", "description": "武装冲突事件数据库"},
    "YouTube": {"type": "实时", "update_freq": "实时", "description": "YouTube视频搜索"},
    "IMF_COFER": {"type": "滞后", "update_freq": "季度", "lag": "3个月", "description": "IMF外汇储备构成"},
    "OECD": {"type": "滞后", "update_freq": "年度", "lag": "1-2年", "description": "OECD研发统计"},
    "SIPRI": {"type": "滞后", "update_freq": "年度", "lag": "1年", "description": "SIPRI军费数据库"},
    "GlobalFirepower": {"type": "准实时", "update_freq": "年度更新", "description": "全球军力排名"},
    "UN_Votes": {"type": "滞后", "update_freq": "年度", "lag": "6-12个月", "description": "联合国大会投票记录"},
    "NATO_News": {"type": "准实时", "update_freq": "每日", "description": "北约官方新闻"},
    "Cloudflare_Radar": {"type": "实时", "update_freq": "每日", "description": "Cloudflare网络攻击趋势"},
    "USGS": {"type": "滞后", "update_freq": "年度", "lag": "1年", "description": "USGS矿产数据"},
    "NewsAPI": {"type": "实时", "update_freq": "实时", "description": "国际新闻聚合"},
    "Wikipedia": {"type": "准实时", "update_freq": "持续更新", "description": "维基百科知识库"},
    "Marine_Cadastre": {"type": "滞后", "update_freq": "月度", "lag": "1-3个月", "description": "美国海上AIS船舶追踪"},
    "Sentinel_Hub": {"type": "准实时", "update_freq": "5天重访", "description": "Sentinel-2卫星影像"},
    "NASA_Earthdata": {"type": "准实时", "update_freq": "1-2天", "description": "NASA地球观测数据"},
    "USGS_Explorer": {"type": "准实时", "update_freq": "16天重访", "description": "Landsat卫星数据"},
    "Google_Earth_Engine": {"type": "准实时", "update_freq": "5-16天", "description": "Google Earth Engine卫星数据"},
    "Taiwan_NLSC": {"type": "准实时", "update_freq": "定期更新", "description": "台湾国土测绘中心正射影像"},
}

# 免责声明
DISCLAIMER = "数据来源为公开API，具体时效性见data_source_types字段"


def _get_proxy() -> Optional[str]:
    """获取 HTTP 代理配置"""
    return os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")


async def _async_get(url: str, params: Optional[Dict] = None,
                     headers: Optional[Dict] = None,
                     timeout: Optional[aiohttp.ClientTimeout] = None) -> Any:
    """
    通用异步 GET 请求，统一代理/超时/异常处理。
    返回 JSON 解析后的 Python 对象。
    """
    proxy = _get_proxy()
    _timeout = timeout or DEFAULT_TIMEOUT
    _headers = headers or {"User-Agent": "GeopoliticalAnalysis/1.0"}

    try:
        async with aiohttp.ClientSession(timeout=_timeout) as session:
            async with session.get(url, params=params, proxy=proxy,
                                   headers=_headers, ssl=False) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"HTTP {resp.status} from {url}: {text[:200]}")
                    return None
                content_type = resp.headers.get("Content-Type", "")
                if "json" in content_type or "javascript" in content_type:
                    return await resp.json(content_type=None)
                # 有些 API 返回 text/plain 但内容是 JSON
                text = await resp.text()
                if text.strip().startswith(("{", "[")):
                    import json
                    return json.loads(text)
                return text
    except asyncio.TimeoutError:
        logger.error(f"Timeout fetching {url}")
        return None
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None


async def _async_get_raw(url: str, params: Optional[Dict] = None,
                         timeout: Optional[aiohttp.ClientTimeout] = None) -> Optional[bytes]:
    """异步 GET 返回原始字节（用于大 JSON 文件如 MITRE STIX）"""
    proxy = _get_proxy()
    _timeout = timeout or aiohttp.ClientTimeout(total=60)
    try:
        async with aiohttp.ClientSession(timeout=_timeout) as session:
            async with session.get(url, params=params, proxy=proxy, ssl=False) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
    except Exception as e:
        logger.error(f"Error fetching raw {url}: {e}")
        return None


# ================================================================
# GDELT API
# ================================================================

GDELT_DOC_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_GEO_BASE = "https://api.gdeltproject.org/api/v2/geo/geo"


async def gdelt_doc_search(query: str, mode: str = "artlist",
                           max_records: int = 25, timespan: str = "7d",
                           sourcelang: str = "english") -> Optional[Dict]:
    """
    GDELT DOC API 搜索
    mode: artlist | tonechart | timelinevol | timelinesourcecountry
    """
    params = {
        "query": query,
        "mode": mode,
        "maxrecords": str(max_records),
        "timespan": timespan,
        "format": "json",
        "sourcelang": sourcelang,
    }
    result = await _async_get(GDELT_DOC_BASE, params=params)
    return result if isinstance(result, dict) else None


async def gdelt_geo_search(query: str, format: str = "geojson",
                           timespan: str = "7d") -> Optional[Dict]:
    """
    GDELT GEO API — 事件地理分布
    """
    params = {
        "query": query,
        "format": format,
        "timespan": timespan,
    }
    result = await _async_get(GDELT_GEO_BASE, params=params)
    return result if isinstance(result, dict) else None


# ================================================================
# World Bank API
# ================================================================

WB_BASE = "https://api.worldbank.org/v2"


async def worldbank_fetch(countries: str, indicator: str,
                          per_page: int = 50, date_range: str = "") -> List[Dict]:
    """
    World Bank Indicators API
    countries: ISO2 逗号分隔, 如 "CHN;USA"
    indicator: 如 "NY.GDP.MKTP.CD"
    date_range: 如 "2015:2024"
    返回数据记录列表（去掉分页元信息）
    """
    url = f"{WB_BASE}/country/{countries}/indicator/{indicator}"
    params: Dict[str, Any] = {
        "format": "json",
        "per_page": str(per_page),
    }
    if date_range:
        params["date"] = date_range

    result = await _async_get(url, params=params)
    # World Bank 返回 [pagination_info, data_array]
    if isinstance(result, list) and len(result) >= 2 and isinstance(result[1], list):
        return result[1]
    return []


# ================================================================
# FRED API
# ================================================================

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"


async def fred_fetch(series_id: str, limit: int = 100,
                     sort_order: str = "desc") -> List[Dict]:
    """
    FRED 经济数据 API
    需要 FRED_API_KEY 环境变量（免费申请: https://fred.stlouisfed.org/docs/api/api_key.html）
    无 Key 时自动跳过并返回空列表。
    """
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        logger.info(f"FRED_API_KEY not set, skipping series {series_id}")
        return []

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "limit": str(limit),
        "sort_order": sort_order,
    }
    result = await _async_get(FRED_BASE, params=params)
    if isinstance(result, dict) and "observations" in result:
        return result["observations"]
    return []


# ================================================================
# IMF Data Mapper API
# ================================================================

IMF_BASE = "https://www.imf.org/external/datamapper/api/v1"


async def imf_fetch(indicator: str, countries: str = "") -> Optional[Dict]:
    """
    IMF Data Mapper API
    indicator: 如 "NGDP_RPCH" (GDP增长率), "PCPIPCH" (通胀率)
    countries: ISO3 逗号分隔, 如 "CHN,USA"
    返回 { "values": { "indicator_id": { "country": { "year": value } } } }
    """
    url = f"{IMF_BASE}/{indicator}"
    if countries:
        url += f"/{countries}"

    result = await _async_get(url)
    if isinstance(result, dict):
        return result
    return None


# ================================================================
# Semantic Scholar API
# ================================================================

SEMANTIC_SCHOLAR_BASE = "https://api.semanticscholar.org/graph/v1"


async def semantic_scholar_search(query: str, limit: int = 20,
                                  fields: str = "title,year,citationCount,authors") -> List[Dict]:
    """
    Semantic Scholar 论文搜索 API（免费，有限速 100 req/5min）
    返回论文列表。
    """
    url = f"{SEMANTIC_SCHOLAR_BASE}/paper/search"
    params = {
        "query": query,
        "limit": str(min(limit, 100)),
        "fields": fields,
    }
    result = await _async_get(url, params=params)
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return []


# ================================================================
# UN Comtrade Preview API
# ================================================================

COMTRADE_BASE = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"


async def comtrade_fetch(reporter_code: str, partner_code: str,
                         period: str = "2023", flow_code: str = "M") -> List[Dict]:
    """
    UN Comtrade Preview API（免费，无需注册，有限速）
    reporter_code / partner_code: UN 国家代码数字
    flow_code: M=进口, X=出口
    """
    params = {
        "reporterCode": reporter_code,
        "partnerCode": partner_code,
        "period": period,
        "flowCode": flow_code,
    }
    result = await _async_get(COMTRADE_BASE, params=params)
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return []


# ================================================================
# 工具函数
# ================================================================

# 常用国家 ISO2 → ISO3 映射
COUNTRY_ISO_MAP = {
    "CHN": "CN", "USA": "US", "RUS": "RU", "GBR": "GB", "DEU": "DE",
    "FRA": "FR", "JPN": "JP", "KOR": "KR", "IND": "IN", "BRA": "BR",
    "AUS": "AU", "CAN": "CA", "ISR": "IL", "IRN": "IR", "SAU": "SA",
    "TUR": "TR", "ZAF": "ZA", "MEX": "MX", "IDN": "ID", "ARE": "AE",
}

# 反向映射 ISO2 → ISO3
COUNTRY_ISO2_TO_ISO3 = {v: k for k, v in COUNTRY_ISO_MAP.items()}

# 中文国家名映射
COUNTRY_CN_MAP = {
    "中国": "CHN", "美国": "USA", "俄罗斯": "RUS", "英国": "GBR",
    "德国": "DEU", "法国": "FRA", "日本": "JPN", "韩国": "KOR",
    "印度": "IND", "巴西": "BRA", "澳大利亚": "AUS", "加拿大": "CAN",
    "以色列": "ISR", "伊朗": "IRN", "沙特": "SAU", "土耳其": "TUR",
    "南非": "ZAF", "墨西哥": "MEX", "印尼": "IDN", "阿联酋": "ARE",
    "台湾": "TWN", "朝鲜": "PRK", "巴基斯坦": "PAK", "波兰": "POL",
    "埃及": "EGY", "乌克兰": "UKR", "泰国": "THA", "越南": "VNM",
    "菲律宾": "PHL", "新加坡": "SGP", "马来西亚": "MYS",
}

# 国家首都坐标映射 (ISO3 → [lat, lng])
# 用于地缘政治技能在地图上标记国家位置
COUNTRY_CAPITAL_COORDS = {
    "CHN": [39.9042, 116.4074],    # 北京
    "USA": [38.9072, -77.0369],    # 华盛顿
    "RUS": [55.7558, 37.6173],     # 莫斯科
    "GBR": [51.5074, -0.1278],     # 伦敦
    "DEU": [52.5200, 13.4050],     # 柏林
    "FRA": [48.8566, 2.3522],      # 巴黎
    "JPN": [35.6762, 139.6503],    # 东京
    "KOR": [37.5665, 126.9780],    # 首尔
    "IND": [28.6139, 77.2090],     # 新德里
    "BRA": [-15.7975, -47.8919],   # 巴西利亚
    "AUS": [-35.2809, 149.1300],   # 堪培拉
    "CAN": [45.4215, -75.6972],    # 渥太华
    "ISR": [31.7683, 35.2137],     # 耶路撒冷
    "IRN": [35.6892, 51.3890],     # 德黑兰
    "SAU": [24.7136, 46.6753],     # 利雅得
    "TUR": [39.9334, 32.8597],     # 安卡拉
    "ZAF": [-25.7479, 28.2293],    # 比勒陀利亚
    "MEX": [19.4326, -99.1332],    # 墨西哥城
    "IDN": [-6.2088, 106.8456],    # 雅加达
    "ARE": [24.4539, 54.3773],     # 阿布扎比
    "TWN": [25.0330, 121.5654],    # 台北
    "PRK": [39.0392, 125.7625],    # 平壤
    "PAK": [33.6844, 73.0479],     # 伊斯兰堡
    "POL": [52.2297, 21.0122],     # 华沙
    "EGY": [30.0444, 31.2357],     # 开罗
    "UKR": [50.4501, 30.5234],     # 基辅
    "THA": [13.7563, 100.5018],    # 曼谷
    "VNM": [21.0278, 105.8342],    # 河内
    "PHL": [14.5995, 120.9842],    # 马尼拉
    "SGP": [1.3521, 103.8198],     # 新加坡
    "MYS": [3.1390, 101.6869],     # 吉隆坡
    "ITA": [41.9028, 12.4964],     # 罗马
    "ESP": [40.4168, -3.7038],     # 马德里
    "NLD": [52.3676, 4.9041],      # 阿姆斯特丹
    "SWE": [59.3293, 18.0686],     # 斯德哥尔摩
    "NOR": [59.9139, 10.7522],     # 奥斯陆
    "FIN": [60.1699, 24.9384],     # 赫尔辛基
    "GRC": [37.9838, 23.7275],     # 雅典
    "ARG": [-34.6037, -58.3816],   # 布宜诺斯艾利斯
    "CHL": [-33.4489, -70.6693],   # 圣地亚哥
    "COL": [4.7110, -74.0721],     # 波哥大
    "NGA": [9.0579, 7.4951],       # 阿布贾
    "KEN": [-1.2921, 36.8219],     # 内罗毕
    "ETH": [9.0250, 38.7469],      # 亚的斯亚贝巴
    "MMR": [19.7633, 96.0785],     # 内比都
    "AFG": [34.5553, 69.2075],     # 喀布尔
    "IRQ": [33.3152, 44.3661],     # 巴格达
    "SYR": [33.5138, 36.2765],     # 大马士革
    "LBY": [32.8872, 13.1913],     # 的黎波里
    "QAT": [25.2854, 51.5310],     # 多哈
    "KWT": [29.3759, 47.9774],     # 科威特城
    "NZL": [-41.2865, 174.7762],   # 惠灵顿
}


def get_country_coords(country_name_or_code: str) -> Optional[List[float]]:
    """
    获取国家首都坐标 [lat, lng]。
    支持中文名、ISO2、ISO3 输入。
    """
    iso3 = resolve_country_code(country_name_or_code)
    coords = COUNTRY_CAPITAL_COORDS.get(iso3)
    if coords:
        return coords
    # 尝试英文名模糊匹配
    name_lower = country_name_or_code.lower().strip()
    _EN_NAME_MAP = {
        "china": "CHN", "united states": "USA", "russia": "RUS",
        "united kingdom": "GBR", "germany": "DEU", "france": "FRA",
        "japan": "JPN", "south korea": "KOR", "india": "IND",
        "brazil": "BRA", "australia": "AUS", "canada": "CAN",
        "israel": "ISR", "iran": "IRN", "saudi arabia": "SAU",
        "turkey": "TUR", "south africa": "ZAF", "mexico": "MEX",
        "indonesia": "IDN", "uae": "ARE", "taiwan": "TWN",
        "north korea": "PRK", "pakistan": "PAK", "poland": "POL",
        "egypt": "EGY", "ukraine": "UKR",
    }
    iso3_from_en = _EN_NAME_MAP.get(name_lower)
    if iso3_from_en:
        return COUNTRY_CAPITAL_COORDS.get(iso3_from_en)
    return None


def resolve_country_code(name_or_code: str) -> str:
    """将国家名/ISO2/ISO3 统一解析为 ISO3"""
    upper = name_or_code.upper().strip()
    # 已经是 ISO3
    if upper in COUNTRY_ISO_MAP:
        return upper
    # ISO2
    if upper in COUNTRY_ISO2_TO_ISO3:
        return COUNTRY_ISO2_TO_ISO3[upper]
    # 中文名
    if name_or_code in COUNTRY_CN_MAP:
        return COUNTRY_CN_MAP[name_or_code]
    # 返回原值
    return upper


def countries_for_worldbank(countries_str: str) -> str:
    """将逗号分隔的国家代码转为 World Bank 格式 (;分隔的 ISO2)"""
    codes = [c.strip() for c in countries_str.split(",")]
    iso2_list = []
    for c in codes:
        iso3 = resolve_country_code(c)
        iso2 = COUNTRY_ISO_MAP.get(iso3, c)
        iso2_list.append(iso2)
    return ";".join(iso2_list)


def safe_float(val: Any, default: float = 0.0) -> float:
    """安全浮点数转换"""
    if val is None or val == "" or val == "." or val == "n/a":
        return default
    try:
        f = float(val)
        import math
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


# ================================================================
# ACLED 冲突事件 API
# ================================================================

ACLED_BASE = "https://api.acleddata.com/acled/read"


async def acled_fetch(country: str = "", event_type: str = "",
                      limit: int = 100, days: int = 30) -> List[Dict]:
    """
    ACLED API - 武装冲突事件数据库
    需要 ACLED_API_KEY + ACLED_EMAIL 环境变量（免费申请: https://acleddata.com）
    无 Key 时自动跳过并返回空列表。
    """
    api_key = os.environ.get("ACLED_API_KEY")
    email = os.environ.get("ACLED_EMAIL")
    if not api_key or not email:
        logger.info("ACLED_API_KEY or ACLED_EMAIL not set, skipping ACLED fetch")
        return []

    from datetime import datetime, timedelta
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    params: Dict[str, Any] = {
        "key": api_key,
        "email": email,
        "event_date": f"{start_date}|{end_date}",
        "event_date_where": "BETWEEN",
        "limit": str(limit),
    }
    if country:
        params["country"] = country
    if event_type:
        params["event_type"] = event_type

    result = await _async_get(ACLED_BASE, params=params)
    if isinstance(result, dict) and "data" in result:
        events = result["data"]
        return [
            {
                "event_date": e.get("event_date", ""),
                "event_type": e.get("event_type", ""),
                "sub_event_type": e.get("sub_event_type", ""),
                "country": e.get("country", ""),
                "location": e.get("location", ""),
                "fatalities": int(safe_float(e.get("fatalities", 0))),
                "notes": (e.get("notes", "") or "")[:300],
                "source": e.get("source", ""),
            }
            for e in events
        ]
    return []


# ================================================================
# YouTube Data API v3
# ================================================================

YOUTUBE_SEARCH_BASE = "https://www.googleapis.com/youtube/v3/search"


async def youtube_search(query: str, max_results: int = 10,
                         order: str = "relevance") -> List[Dict]:
    """
    YouTube Data API v3 - 视频搜索
    需要 YOUTUBE_API_KEY 环境变量（从 Google Cloud Console 免费申请）
    无 Key 时自动跳过并返回空列表。
    """
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        logger.info("YOUTUBE_API_KEY not set, skipping YouTube search")
        return []

    params = {
        "key": api_key,
        "q": query,
        "part": "snippet",
        "type": "video",
        "maxResults": str(min(max_results, 50)),
        "order": order,
    }

    result = await _async_get(YOUTUBE_SEARCH_BASE, params=params)
    if isinstance(result, dict) and "items" in result:
        videos = []
        for item in result["items"]:
            snippet = item.get("snippet", {})
            videos.append({
                "video_id": item.get("id", {}).get("videoId", ""),
                "title": snippet.get("title", ""),
                "description": (snippet.get("description", "") or "")[:300],
                "channel": snippet.get("channelTitle", ""),
                "published_at": snippet.get("publishedAt", ""),
            })
        return videos
    return []


# ================================================================
# IMF COFER 外汇储备构成 API
# ================================================================

IMF_COFER_BASE = "https://data-api.imf.org/v1/data/COFER"


async def imf_cofer_fetch() -> Dict[str, Any]:
    """
    IMF COFER API - 官方外汇储备货币构成
    免费无 Key。返回各货币占全球外汇储备比例。
    """
    # 查询 World 数据, 所有指标
    url = f"{IMF_COFER_BASE}/Q..?format=json&startPeriod=2015&detail=dataonly"

    result = await _async_get(url, timeout=aiohttp.ClientTimeout(total=45))

    if not result:
        # 备用: 使用 IMF Data Mapper
        logger.info("IMF COFER primary API failed, trying Data Mapper fallback")
        fallback = await _async_get(
            "https://www.imf.org/external/datamapper/api/v1/RCOFER_USD"
        )
        if isinstance(fallback, dict):
            return {"source": "IMF_DataMapper_fallback", "data": fallback}
        return {}

    # 解析 SDMX JSON 结构
    try:
        datasets = result.get("dataSets", [{}])
        if not datasets:
            return {}
        series = datasets[0].get("series", {})
        structure = result.get("structure", {})
        dimensions = structure.get("dimensions", {}).get("series", [])

        # 提取指标名映射
        indicator_map = {}
        if dimensions:
            for dim in dimensions:
                if dim.get("id") == "INDICATOR":
                    for val in dim.get("values", []):
                        indicator_map[str(val.get("id", ""))] = val.get("name", "")

        parsed: Dict[str, Any] = {"indicators": {}}
        for key, series_data in series.items():
            obs = series_data.get("observations", {})
            if obs:
                # 取最新观测
                latest_key = max(obs.keys())
                latest_val = obs[latest_key]
                if isinstance(latest_val, list) and latest_val:
                    parsed["indicators"][key] = {
                        "value": safe_float(latest_val[0]),
                    }

        parsed["source"] = "IMF_COFER"
        return parsed
    except Exception as e:
        logger.error(f"Failed to parse IMF COFER data: {e}")
        return {}


# ================================================================
# OECD SDMX REST API
# ================================================================

OECD_BASE = "https://sdmx.oecd.org/public/rest/data"


async def oecd_rd_fetch(countries: str = "CHN+USA+JPN+KOR+DEU",
                        indicator: str = "GERD_GDP") -> List[Dict]:
    """
    OECD SDMX API - 研发支出数据
    免费无 Key。获取 GERD 占 GDP 比例等研发指标。
    """
    # OECD MSTI 数据集
    url = f"{OECD_BASE}/OECD.STI.PIE,DSD_MSTI@DF_MSTI,1.0/{countries}.A.{indicator}"
    headers = {
        "Accept": "application/vnd.sdmx.data+json;charset=utf-8;version=2.0.0",
        "User-Agent": "GeopoliticalAnalysis/1.0",
    }

    result = await _async_get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=45))

    if not result or not isinstance(result, dict):
        logger.warning("OECD SDMX API returned empty or non-JSON, trying fallback")
        # 备用 URL 格式
        fallback_url = f"https://stats.oecd.org/SDMX-JSON/data/MSTI_PUB/{countries}.GERD_GDP/all?startTime=2015"
        result = await _async_get(fallback_url, timeout=aiohttp.ClientTimeout(total=45))
        if not result or not isinstance(result, dict):
            return []

    try:
        datasets = result.get("dataSets", [{}])
        if not datasets:
            return []
        series = datasets[0].get("series", {})
        structure = result.get("structure", {})

        # 提取时间维度
        time_periods = []
        obs_dims = structure.get("dimensions", {}).get("observation", [])
        for dim in obs_dims:
            if dim.get("id") == "TIME_PERIOD":
                time_periods = [v.get("id", "") for v in dim.get("values", [])]

        # 提取国家维度
        country_values = []
        series_dims = structure.get("dimensions", {}).get("series", [])
        for dim in series_dims:
            if dim.get("id") == "REF_AREA":
                country_values = [v.get("id", "") for v in dim.get("values", [])]

        records = []
        for key, series_data in series.items():
            key_parts = key.split(":")
            country_idx = int(key_parts[0]) if key_parts else 0
            country_code = country_values[country_idx] if country_idx < len(country_values) else ""

            obs = series_data.get("observations", {})
            for time_idx, val_arr in obs.items():
                time_idx_int = int(time_idx)
                year = time_periods[time_idx_int] if time_idx_int < len(time_periods) else ""
                value = safe_float(val_arr[0]) if isinstance(val_arr, list) and val_arr else 0

                records.append({
                    "country": country_code,
                    "year": year,
                    "indicator": indicator,
                    "value": round(value, 3),
                })

        return records
    except Exception as e:
        logger.error(f"Failed to parse OECD data: {e}")
        return []


# ================================================================
# SIPRI 军费 Excel 数据
# ================================================================

SIPRI_MILEX_URL = "https://milex.sipri.org/sipri_milex/sipri_milex.xls"
SIPRI_CSV_FALLBACK = "https://raw.githubusercontent.com/datasets/military-expenditure/master/data/data.csv"


async def sipri_milex_fetch() -> Dict[str, Any]:
    """
    下载 SIPRI MILEX 军费数据并解析。
    优先尝试 GitHub 上的 CSV 备用源。
    """
    # 使用 GitHub 上的结构化 CSV 数据（更易解析）
    raw = await _async_get(SIPRI_CSV_FALLBACK, timeout=aiohttp.ClientTimeout(total=45))

    if isinstance(raw, str) and raw.strip():
        try:
            reader = csv.DictReader(io.StringIO(raw))
            records: Dict[str, List[Dict]] = {}
            for row in reader:
                country = row.get("Country Name", row.get("Entity", ""))
                year = row.get("Year", "")
                value = safe_float(row.get("Value", row.get("Military expenditure (% of GDP)", 0)))
                if country and year:
                    if country not in records:
                        records[country] = []
                    records[country].append({"year": year, "value": value})

            return {"source": "SIPRI_GitHub_CSV", "countries": records}
        except Exception as e:
            logger.error(f"Failed to parse SIPRI CSV: {e}")

    # 尝试 SIPRI 直接下载 (Excel 格式, 需 openpyxl)
    logger.info("SIPRI CSV fallback failed, trying direct SIPRI download")
    raw_bytes = await _async_get_raw(SIPRI_MILEX_URL, timeout=aiohttp.ClientTimeout(total=60))
    if raw_bytes:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if len(rows) > 1:
                headers = [str(h) if h else "" for h in rows[0]]
                records = {}
                for row in rows[1:]:
                    country = str(row[0]) if row[0] else ""
                    if not country:
                        continue
                    records[country] = []
                    for i, val in enumerate(row[1:], 1):
                        if i < len(headers) and val is not None:
                            records[country].append({
                                "year": headers[i],
                                "value": safe_float(val),
                            })
                return {"source": "SIPRI_Excel", "countries": records}
        except ImportError:
            logger.warning("openpyxl not installed, cannot parse SIPRI Excel")
        except Exception as e:
            logger.error(f"Failed to parse SIPRI Excel: {e}")

    return {}


# ================================================================
# GlobalFirepower 军力排名
# ================================================================

GFP_BASE = "https://www.globalfirepower.com"


async def globalfirepower_fetch(country: str = "") -> Dict[str, Any]:
    """
    抓取 GlobalFirepower 军力排名页面。
    解析排名表格获取国家军力排名和子项指标。
    """
    url = f"{GFP_BASE}/countries-listing.php"
    raw = await _async_get(url, timeout=aiohttp.ClientTimeout(total=30))

    if not isinstance(raw, str):
        return {}

    try:
        rankings = []
        # 解析 HTML 提取排名数据
        # GFP 页面结构: 每个国家有 class="picTrans" 的链接
        country_pattern = re.compile(
            r'<a\s+href="([^"]*-military-strength\.php)"[^>]*>.*?'
            r'<span\s+class="countryName"[^>]*>([^<]+)</span>.*?'
            r'<span\s+class="pwrIndx"[^>]*>([^<]+)</span>',
            re.DOTALL
        )

        matches = country_pattern.findall(raw)
        if not matches:
            # 备用解析模式
            simple_pattern = re.compile(
                r'<span class="countryName[^"]*">([^<]+)</span>.*?'
                r'<span class="pwrIndx[^"]*">([^<]+)</span>',
                re.DOTALL
            )
            simple_matches = simple_pattern.findall(raw)
            for i, (name, index) in enumerate(simple_matches, 1):
                name = name.strip()
                entry = {
                    "rank": i,
                    "country": name,
                    "power_index": safe_float(index.strip()),
                }
                if country and country.lower() in name.lower():
                    return {"target_country": entry, "total_countries": len(simple_matches)}
                rankings.append(entry)
        else:
            for i, (href, name, index) in enumerate(matches, 1):
                name = name.strip()
                entry = {
                    "rank": i,
                    "country": name,
                    "power_index": safe_float(index.strip()),
                }
                if country and country.lower() in name.lower():
                    return {"target_country": entry, "total_countries": len(matches)}
                rankings.append(entry)

        result: Dict[str, Any] = {
            "rankings": rankings[:50],
            "total_countries": len(rankings),
            "source": "GlobalFirepower",
        }

        if country:
            # 未在排名中找到指定国家
            result["target_country"] = None
            result["note"] = f"Country '{country}' not found in rankings"

        return result

    except Exception as e:
        logger.error(f"Failed to parse GlobalFirepower page: {e}")
        return {}


# ================================================================
# UN 大会投票记录
# ================================================================

UN_VOTES_CSV_URL = "https://raw.githubusercontent.com/dgrtwo/un-general-assembly-votes/master/rawvotes.csv"


async def un_votes_fetch(country_code: str = "", session: int = 0) -> List[Dict]:
    """
    从 GitHub 获取 UN 大会投票 CSV 数据。
    免费，无需 Key。
    country_code: ISO2 国家代码 (如 US, CN, RU)
    session: UN 大会届次号 (如 76, 77), 0 表示所有
    """
    raw = await _async_get(UN_VOTES_CSV_URL, timeout=aiohttp.ClientTimeout(total=45))

    if not isinstance(raw, str) or not raw.strip():
        return []

    try:
        reader = csv.DictReader(io.StringIO(raw))
        records = []
        for row in reader:
            cc = row.get("ccode", row.get("country", ""))
            sess = row.get("session", "")
            vote = row.get("vote", "")
            rcid = row.get("rcid", "")
            country_name = row.get("Country", row.get("country", ""))

            # 过滤条件
            if country_code and cc.upper() != country_code.upper():
                if country_name.upper() != country_code.upper():
                    continue
            if session and str(sess) != str(session):
                continue

            records.append({
                "session": sess,
                "rcid": rcid,
                "country_code": cc,
                "country": country_name,
                "vote": vote,
            })

            if len(records) >= 500:  # 限制返回量
                break

        return records
    except Exception as e:
        logger.error(f"Failed to parse UN votes CSV: {e}")
        return []


# ================================================================
# NATO 新闻
# ================================================================

NATO_NEWS_RSS = "https://www.nato.int/cps/en/natohq/news.rss"
NATO_NEWS_API = "https://www.nato.int/cps/en/natohq/news.htm"


async def nato_news_fetch(query: str = "", max_items: int = 10) -> List[Dict]:
    """
    抓取 NATO 官网新闻 RSS feed。
    免费，无需 Key。
    """
    raw = await _async_get(NATO_NEWS_RSS, timeout=aiohttp.ClientTimeout(total=30))

    if not isinstance(raw, str) or not raw.strip():
        return []

    try:
        news_items = []

        # 解析 RSS XML
        item_pattern = re.compile(
            r'<item>(.*?)</item>', re.DOTALL
        )
        title_pattern = re.compile(r'<title><!\[CDATA\[(.*?)\]\]></title>', re.DOTALL)
        title_pattern_alt = re.compile(r'<title>(.*?)</title>', re.DOTALL)
        link_pattern = re.compile(r'<link>(.*?)</link>', re.DOTALL)
        pubdate_pattern = re.compile(r'<pubDate>(.*?)</pubDate>', re.DOTALL)
        desc_pattern = re.compile(r'<description><!\[CDATA\[(.*?)\]\]></description>', re.DOTALL)

        items = item_pattern.findall(raw)
        for item_xml in items:
            # 提取标题
            title_match = title_pattern.search(item_xml)
            if not title_match:
                title_match = title_pattern_alt.search(item_xml)
            title = title_match.group(1).strip() if title_match else ""

            # 按 query 过滤
            if query and query.lower() not in title.lower():
                desc_match = desc_pattern.search(item_xml)
                desc = desc_match.group(1).strip() if desc_match else ""
                if query.lower() not in desc.lower():
                    continue

            link_match = link_pattern.search(item_xml)
            link = link_match.group(1).strip() if link_match else ""

            pubdate_match = pubdate_pattern.search(item_xml)
            pubdate = pubdate_match.group(1).strip() if pubdate_match else ""

            news_items.append({
                "title": title,
                "url": link,
                "date": pubdate,
                "source": "NATO",
            })

            if len(news_items) >= max_items:
                break

        return news_items
    except Exception as e:
        logger.error(f"Failed to parse NATO RSS: {e}")
        return []


# ================================================================
# Cloudflare Radar API
# ================================================================

CLOUDFLARE_RADAR_BASE = "https://api.cloudflare.com/client/v4/radar"


async def cloudflare_radar_fetch(metric: str = "attacks",
                                  location: str = "") -> Dict[str, Any]:
    """
    Cloudflare Radar API - 网络攻击趋势。
    免费无 Key (公共端点)。
    metric: attacks | traffic | dns
    location: ISO2 国家代码 (可选)
    """
    if metric == "attacks":
        url = f"{CLOUDFLARE_RADAR_BASE}/attacks/layer3/summary"
    elif metric == "dns":
        url = f"{CLOUDFLARE_RADAR_BASE}/dns/summary"
    else:
        url = f"{CLOUDFLARE_RADAR_BASE}/http/summary/http_protocol"

    params: Dict[str, str] = {"format": "json"}
    if location:
        params["location"] = location.upper()

    result = await _async_get(url, timeout=aiohttp.ClientTimeout(total=30))

    if isinstance(result, dict):
        # Cloudflare Radar API 结构: {"success": true, "result": {...}}
        if result.get("success"):
            return {
                "metric": metric,
                "location": location or "global",
                "data": result.get("result", {}),
                "source": "Cloudflare_Radar",
            }
        # 如果没有 success 字段，直接返回
        return {
            "metric": metric,
            "location": location or "global",
            "data": result,
            "source": "Cloudflare_Radar",
        }
    return {}


# ================================================================
# USGS 矿产商品数据 API
# ================================================================

USGS_MINERAL_BASE = "https://minerals.usgs.gov/minerals/pubs/commodity"
USGS_API_BASE = "https://minerals.usgs.gov/api"


async def usgs_mineral_fetch(commodity: str = "") -> Dict[str, Any]:
    """
    USGS 矿产商品数据 API。
    免费无 Key。尝试获取矿产产量/储量数据。
    commodity: 矿产名称如 'rare_earths', 'lithium', 'cobalt'
    """
    # 使用 USGS Mineral Commodity Summaries API
    commodity_map = {
        "稀土": "Rare earth",
        "锂": "Lithium",
        "钴": "Cobalt",
        "铜": "Copper",
        "镍": "Nickel",
        "铁矿石": "Iron ore",
        "铝": "Aluminum",
        "锡": "Tin",
        "钨": "Tungsten",
        "铂": "Platinum",
        "rare_earths": "Rare earth",
        "rare earth": "Rare earth",
    }

    # 中文转英文
    commodity_en = commodity_map.get(commodity, commodity) if commodity else ""

    # 数据源列表（按优先级尝试）
    data_sources = [
        "https://raw.githubusercontent.com/datasets/mineral-production/master/data/mineral-production.csv",
        "https://raw.githubusercontent.com/datasets/world-resources-institute/master/data/data.csv",
        "https://pkgstore.datahub.io/core/world-resources-institute/data_csv/data/data.csv",
    ]

    for url in data_sources:
        raw = await _async_get(url, timeout=aiohttp.ClientTimeout(total=45))

        if isinstance(raw, str) and raw.strip() and not raw.startswith("<!"):
            try:
                reader = csv.DictReader(io.StringIO(raw))
                records: Dict[str, List[Dict]] = {}
                for row in reader:
                    mineral = row.get("Commodity", row.get("commodity", row.get("Mineral", "")))
                    country = row.get("Country", row.get("country", row.get("Entity", "")))
                    year = row.get("Year", row.get("year", ""))
                    value = safe_float(row.get("Value", row.get("production", row.get("value", 0))))

                    if commodity_en and commodity_en.lower() not in mineral.lower():
                        continue

                    if not mineral:
                        continue

                    if mineral not in records:
                        records[mineral] = []
                    records[mineral].append({
                        "country": country,
                        "year": year,
                        "value": value,
                    })

                if records:
                    return {
                        "source": "USGS_GitHub_CSV",
                        "commodity_filter": commodity_en or "all",
                        "minerals": records,
                    }
            except Exception as e:
                logger.warning(f"Failed to parse USGS mineral CSV from {url}: {e}")
                continue

    # 如果所有数据源都失败，返回静态稀土数据（来自 USGS MCS 2024 报告）
    logger.info("Using static USGS rare earth data as fallback")
    static_rare_earth = {
        "Rare earth elements": [
            {"country": "China", "year": "2023", "value": 210000, "unit": "metric tons"},
            {"country": "United States", "year": "2023", "value": 43000, "unit": "metric tons"},
            {"country": "Australia", "year": "2023", "value": 8000, "unit": "metric tons"},
            {"country": "Myanmar", "year": "2023", "value": 38000, "unit": "metric tons"},
            {"country": "India", "year": "2023", "value": 2900, "unit": "metric tons"},
            {"country": "Russia", "year": "2023", "value": 2600, "unit": "metric tons"},
            {"country": "Thailand", "year": "2023", "value": 7100, "unit": "metric tons"},
            {"country": "Vietnam", "year": "2023", "value": 4300, "unit": "metric tons"},
            {"country": "Brazil", "year": "2023", "value": 100, "unit": "metric tons"},
        ],
        "Lithium": [
            {"country": "Australia", "year": "2023", "value": 86000, "unit": "metric tons LCE"},
            {"country": "Chile", "year": "2023", "value": 44000, "unit": "metric tons LCE"},
            {"country": "China", "year": "2023", "value": 33000, "unit": "metric tons LCE"},
            {"country": "Argentina", "year": "2023", "value": 9600, "unit": "metric tons LCE"},
        ],
        "Cobalt": [
            {"country": "Democratic Republic of Congo", "year": "2023", "value": 130000, "unit": "metric tons"},
            {"country": "Russia", "year": "2023", "value": 8900, "unit": "metric tons"},
            {"country": "Australia", "year": "2023", "value": 5900, "unit": "metric tons"},
            {"country": "Philippines", "year": "2023", "value": 4800, "unit": "metric tons"},
            {"country": "China", "year": "2023", "value": 2200, "unit": "metric tons"},
        ],
    }

    if commodity_en:
        filtered = {}
        for k, v in static_rare_earth.items():
            if commodity_en.lower() in k.lower():
                filtered[k] = v
        if filtered:
            return {
                "source": "USGS_MCS_2024_Static",
                "commodity_filter": commodity_en,
                "minerals": filtered,
                "note": "使用 USGS Mineral Commodity Summaries 2024 静态数据",
            }

    return {
        "source": "USGS_MCS_2024_Static",
        "commodity_filter": commodity_en or "all",
        "minerals": static_rare_earth,
        "note": "使用 USGS Mineral Commodity Summaries 2024 静态数据",
    }


# ================================================================
# NewsAPI 国际新闻聚合
# ================================================================

NEWSAPI_BASE = "https://newsapi.org/v2/everything"


async def newsapi_search(query: str, language: str = "en",
                         sort_by: str = "relevancy", page_size: int = 20) -> List[Dict]:
    """
    NewsAPI.org 新闻聚合 — 需 NEWSAPI_KEY 环境变量
    免费层: 100次/天, 仅过去30天
    无 Key 时自动跳过并返回空列表。
    """
    api_key = os.environ.get("NEWSAPI_KEY")
    if not api_key:
        logger.info("NEWSAPI_KEY not set, skipping NewsAPI search")
        return []

    params = {
        "q": query,
        "language": language,
        "sortBy": sort_by,
        "pageSize": str(min(page_size, 100)),
        "apiKey": api_key,
    }

    result = await _async_get(NEWSAPI_BASE, params=params)
    if isinstance(result, dict) and result.get("status") == "ok":
        articles = result.get("articles", [])
        return [
            {
                "title": a.get("title", ""),
                "description": (a.get("description", "") or "")[:500],
                "source": a.get("source", {}).get("name", ""),
                "author": a.get("author", ""),
                "url": a.get("url", ""),
                "published_at": a.get("publishedAt", ""),
                "content": (a.get("content", "") or "")[:1000],
            }
            for a in articles
        ]
    return []


# ================================================================
# Wikipedia REST API
# ================================================================

WIKIPEDIA_API_BASE = "https://{lang}.wikipedia.org/api/rest_v1"
WIKIPEDIA_SEARCH_BASE = "https://{lang}.wikipedia.org/w/api.php"


async def wikipedia_article_fetch(title: str, language: str = "en") -> Dict[str, Any]:
    """
    Wikipedia REST API — 获取文章摘要和结构化数据。
    免费无 Key。
    """
    base = WIKIPEDIA_API_BASE.format(lang=language)
    encoded_title = quote_plus(title.replace(" ", "_"))
    url = f"{base}/page/summary/{encoded_title}"

    result = await _async_get(url, timeout=aiohttp.ClientTimeout(total=20))
    if isinstance(result, dict) and "title" in result:
        return {
            "title": result.get("title", ""),
            "description": result.get("description", ""),
            "extract": result.get("extract", ""),
            "extract_html": result.get("extract_html", ""),
            "thumbnail_url": result.get("thumbnail", {}).get("source", ""),
            "page_url": result.get("content_urls", {}).get("desktop", {}).get("page", ""),
            "wikidata_id": result.get("wikibase_item", ""),
            "language": language,
            "source": "Wikipedia",
        }
    return {}


async def wikipedia_search(query: str, limit: int = 10, language: str = "en") -> List[Dict]:
    """
    Wikipedia Search API — 搜索文章列表。
    免费无 Key。
    """
    base = WIKIPEDIA_SEARCH_BASE.format(lang=language)
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": str(min(limit, 50)),
        "format": "json",
        "utf8": "1",
    }

    result = await _async_get(base, params=params)
    if isinstance(result, dict):
        search_results = result.get("query", {}).get("search", [])
        return [
            {
                "title": r.get("title", ""),
                "snippet": re.sub(r"<[^>]+>", "", r.get("snippet", "")),
                "word_count": r.get("wordcount", 0),
                "page_id": r.get("pageid", 0),
                "timestamp": r.get("timestamp", ""),
                "url": f"https://{language}.wikipedia.org/wiki/{quote_plus(r.get('title', '').replace(' ', '_'))}",
                "source": "Wikipedia",
            }
            for r in search_results
        ]
    return []


# ================================================================
# Marine Cadastre AIS 船舶追踪数据
# ================================================================

MARINE_CADASTRE_BASE = "https://marinecadastre.gov/ais"


async def marine_cadastre_ais_fetch(year: int = 2023, month: int = 1,
                                     zone: str = "01") -> Dict[str, Any]:
    """
    Marine Cadastre AIS 船舶追踪 — 免费无 Key。
    由于原始数据为大型 CSV 文件（数GB），此函数返回元数据摘要和下载链接，
    不直接下载完整数据集。
    """
    download_url = (
        f"https://coast.noaa.gov/htdata/CMSP/AISDataHandler/{year}/"
        f"AIS_{year}_{month:02d}_Zone{zone}.zip"
    )

    proxy = _get_proxy()
    file_info: Dict[str, Any] = {"exists": False, "size_mb": 0}

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.head(download_url, proxy=proxy, ssl=False,
                                     allow_redirects=True) as resp:
                if resp.status == 200:
                    content_length = resp.headers.get("Content-Length", "0")
                    size_mb = round(int(content_length) / (1024 * 1024), 1)
                    file_info = {"exists": True, "size_mb": size_mb}
                else:
                    file_info = {"exists": False, "status": resp.status}
    except Exception as e:
        logger.warning(f"Marine Cadastre HEAD request failed: {e}")
        file_info = {"exists": False, "error": str(e)}

    return {
        "source": "Marine_Cadastre",
        "year": year,
        "month": month,
        "zone": zone,
        "download_url": download_url,
        "file_info": file_info,
        "description": (
            f"AIS 船舶追踪数据 {year}年{month}月 Zone{zone}。"
            "数据包含船舶MMSI、位置、速度、航向、船舶类型等字段。"
        ),
        "data_fields": [
            "MMSI", "BaseDateTime", "LAT", "LON", "SOG", "COG",
            "Heading", "VesselName", "IMO", "CallSign", "VesselType",
            "Status", "Length", "Width", "Draft", "Cargo",
        ],
        "coverage": "美国海域及周边",
        "update_frequency": "月度",
        "note": "完整数据集为大型CSV文件，建议通过download_url下载后本地处理",
    }


# ================================================================
# Sentinel Hub OAuth + Catalog API
# ================================================================

SENTINEL_HUB_AUTH_URL = "https://services.sentinel-hub.com/auth/realms/main/protocol/openid-connect/token"
SENTINEL_HUB_CATALOG_URL = "https://services.sentinel-hub.com/api/v1/catalog/1.0.0/search"


async def sentinel_hub_catalog_search(bbox: List[float], date_from: str,
                                       date_to: str, max_cloud: float = 30.0) -> Dict[str, Any]:
    """
    Sentinel Hub Catalog API — 需 SENTINEL_HUB_CLIENT_ID 和 SENTINEL_HUB_CLIENT_SECRET 环境变量。
    搜索 Sentinel-2 卫星影像元数据。
    无 Key 时返回空结果 + 提示信息。
    """
    client_id = os.environ.get("SENTINEL_HUB_CLIENT_ID")
    client_secret = os.environ.get("SENTINEL_HUB_CLIENT_SECRET")

    if not client_id or not client_secret:
        logger.info("SENTINEL_HUB credentials not set, skipping Sentinel Hub search")
        return {
            "scenes": [],
            "total": 0,
            "note": "需要设置 SENTINEL_HUB_CLIENT_ID 和 SENTINEL_HUB_CLIENT_SECRET 环境变量",
            "source": "Sentinel_Hub",
        }

    # Step 1: OAuth token
    proxy = _get_proxy()
    access_token = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            auth_data = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
            async with session.post(SENTINEL_HUB_AUTH_URL, data=auth_data,
                                     proxy=proxy, ssl=False) as resp:
                if resp.status == 200:
                    token_data = await resp.json()
                    access_token = token_data.get("access_token")
                else:
                    text = await resp.text()
                    logger.warning(f"Sentinel Hub auth failed: {resp.status} - {text[:200]}")
                    return {"scenes": [], "total": 0, "error": "认证失败", "source": "Sentinel_Hub"}
    except Exception as e:
        logger.error(f"Sentinel Hub auth error: {e}")
        return {"scenes": [], "total": 0, "error": str(e), "source": "Sentinel_Hub"}

    if not access_token:
        return {"scenes": [], "total": 0, "error": "获取token失败", "source": "Sentinel_Hub"}

    # Step 2: Catalog search
    search_body = {
        "bbox": bbox,
        "datetime": f"{date_from}T00:00:00Z/{date_to}T23:59:59Z",
        "collections": ["sentinel-2-l2a"],
        "limit": 20,
        "filter": f"eo:cloud_cover < {max_cloud}",
        "filter-lang": "cql2-text",
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(SENTINEL_HUB_CATALOG_URL, json=search_body,
                                     headers=headers, proxy=proxy, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    features = data.get("features", [])
                    scenes = []
                    for f in features:
                        props = f.get("properties", {})
                        scenes.append({
                            "id": f.get("id", ""),
                            "datetime": props.get("datetime", ""),
                            "cloud_cover": props.get("eo:cloud_cover", None),
                            "bbox": f.get("bbox", []),
                            "platform": props.get("platform", ""),
                            "instrument": props.get("instruments", []),
                        })
                    return {
                        "scenes": scenes,
                        "total": data.get("context", {}).get("matched", len(scenes)),
                        "source": "Sentinel_Hub",
                    }
                else:
                    text = await resp.text()
                    logger.warning(f"Sentinel Hub catalog search failed: {resp.status}")
                    return {"scenes": [], "total": 0, "error": text[:200], "source": "Sentinel_Hub"}
    except Exception as e:
        logger.error(f"Sentinel Hub catalog error: {e}")
        return {"scenes": [], "total": 0, "error": str(e), "source": "Sentinel_Hub"}


# ================================================================
# NASA Earthdata CMR API
# ================================================================

NASA_CMR_BASE = "https://cmr.earthdata.nasa.gov/search"


async def nasa_earthdata_search(query: str = "", bbox: Optional[List[float]] = None,
                                 collection: str = "MODIS", max_results: int = 10) -> List[Dict]:
    """
    NASA CMR (Common Metadata Repository) 搜索 — 可选 NASA_EARTHDATA_TOKEN 环境变量。
    CMR 搜索端点本身无需认证，但 Token 可提高速率限制。
    """
    url = f"{NASA_CMR_BASE}/granules.json"

    params: Dict[str, Any] = {
        "page_size": str(min(max_results, 100)),
        "sort_key": "-start_date",
    }

    if query:
        params["keyword"] = query
    if collection:
        params["short_name"] = collection
    if bbox and len(bbox) == 4:
        params["bounding_box"] = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"

    headers: Dict[str, str] = {"Accept": "application/json"}
    token = os.environ.get("NASA_EARTHDATA_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    result = await _async_get(url, params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=30))

    if isinstance(result, dict):
        feed = result.get("feed", {})
        entries = feed.get("entry", [])
        return [
            {
                "id": e.get("id", ""),
                "title": e.get("title", ""),
                "dataset_id": e.get("dataset_id", ""),
                "time_start": e.get("time_start", ""),
                "time_end": e.get("time_end", ""),
                "granule_size": safe_float(e.get("granule_size", 0)),
                "cloud_cover": safe_float(e.get("cloud_cover", -1)),
                "day_night_flag": e.get("day_night_flag", ""),
                "links": [
                    {"href": lnk.get("href", ""), "type": lnk.get("type", "")}
                    for lnk in (e.get("links", []) or [])[:3]
                ],
                "source": "NASA_Earthdata",
            }
            for e in entries
        ]
    return []


# ================================================================
# USGS M2M API (Earth Explorer)
# ================================================================

USGS_M2M_BASE = "https://m2m.cr.usgs.gov/api/api/json/stable"


async def usgs_m2m_search(dataset: str = "landsat_ot_c2_l2", bbox: Optional[List[float]] = None,
                           date_from: str = "", date_to: str = "",
                           max_results: int = 10) -> List[Dict]:
    """
    USGS Earth Explorer M2M API — 需 USGS_M2M_TOKEN 环境变量。
    搜索 Landsat 等卫星数据。
    无 Token 时返回空列表。
    """
    token = os.environ.get("USGS_M2M_TOKEN")
    if not token:
        logger.info("USGS_M2M_TOKEN not set, skipping USGS M2M search")
        return []

    headers = {
        "X-Auth-Token": token,
        "Content-Type": "application/json",
    }

    search_body: Dict[str, Any] = {
        "datasetName": dataset,
        "maxResults": min(max_results, 100),
        "startingNumber": 1,
        "sceneFilter": {},
    }

    if bbox and len(bbox) == 4:
        search_body["sceneFilter"]["spatialFilter"] = {
            "filterType": "mbr",
            "lowerLeft": {"latitude": bbox[1], "longitude": bbox[0]},
            "upperRight": {"latitude": bbox[3], "longitude": bbox[2]},
        }

    if date_from or date_to:
        search_body["sceneFilter"]["acquisitionFilter"] = {
            "start": date_from or "2020-01-01",
            "end": date_to or "2030-12-31",
        }

    url = f"{USGS_M2M_BASE}/scene-search"
    proxy = _get_proxy()

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(url, json=search_body, headers=headers,
                                     proxy=proxy, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("errorCode"):
                        logger.warning(f"USGS M2M error: {data.get('errorMessage', '')}")
                        return []
                    results = data.get("data", {}).get("results", [])
                    return [
                        {
                            "entity_id": r.get("entityId", ""),
                            "display_id": r.get("displayId", ""),
                            "acquisition_date": r.get("temporalCoverage", {}).get("startDate", ""),
                            "cloud_cover": safe_float(r.get("cloudCover", -1)),
                            "browse_url": r.get("browse", [{}])[0].get("browsePath", "")
                            if r.get("browse") else "",
                            "spatial_coverage": r.get("spatialCoverage", {}),
                            "dataset": dataset,
                            "source": "USGS_Explorer",
                        }
                        for r in results
                    ]
                else:
                    text = await resp.text()
                    logger.warning(f"USGS M2M search failed: {resp.status} - {text[:200]}")
                    return []
    except Exception as e:
        logger.error(f"USGS M2M search error: {e}")
        return []


# ================================================================
# Hugging Face Dataset Hub API
# ================================================================

HUGGINGFACE_API_BASE = "https://huggingface.co/api/datasets"


async def huggingface_dataset_info(dataset_name: str) -> Dict[str, Any]:
    """
    HuggingFace Hub API — 获取数据集元数据。
    免费无 Key。
    注意: 不要对 dataset_name 进行 URL 编码，HuggingFace API 期望原始的斜杠。
    """
    # 不要使用 quote_plus，HuggingFace API 路径中的斜杠不应被编码
    url = f"{HUGGINGFACE_API_BASE}/{dataset_name}"

    result = await _async_get(url, timeout=aiohttp.ClientTimeout(total=20))
    if isinstance(result, dict) and "id" in result:
        return {
            "id": result.get("id", ""),
            "author": result.get("author", ""),
            "description": (result.get("description", "") or "")[:2000],
            "tags": result.get("tags", []),
            "downloads": result.get("downloads", 0),
            "likes": result.get("likes", 0),
            "card_data": result.get("cardData", {}),
            "last_modified": result.get("lastModified", ""),
            "private": result.get("private", False),
            "citation": (result.get("citation", "") or "")[:500],
            "url": f"https://huggingface.co/datasets/{dataset_name}",
            "source": "HuggingFace",
        }
    return {}


# ================================================================
# Google Earth Engine STAC API (免费，无需认证)
# ================================================================

GEE_STAC_BASE = "https://earthengine-stac.storage.googleapis.com"


async def google_earth_engine_search(
    bbox: List[float],
    date_from: str = "",
    date_to: str = "",
    collection: str = "COPERNICUS/S2_SR_HARMONIZED",
    max_results: int = 20,
) -> Dict[str, Any]:
    """
    Google Earth Engine STAC Catalog 搜索 — 免费，无需 API Key。

    通过 GEE 的公开 STAC (SpatioTemporal Asset Catalog) 端点查询卫星影像元数据。
    支持 Sentinel-2、Landsat、MODIS 等多种数据集。

    常用 collection:
    - COPERNICUS/S2_SR_HARMONIZED: Sentinel-2 地表反射率
    - LANDSAT/LC08/C02/T1_L2: Landsat 8 Collection 2
    - MODIS/006/MOD09GA: MODIS 地表反射率

    Args:
        bbox: 边界框 [west, south, east, north]
        date_from: 开始日期 YYYY-MM-DD
        date_to: 结束日期 YYYY-MM-DD
        collection: GEE collection ID
        max_results: 最大返回数量

    Returns:
        包含 scenes 列表的字典
    """
    # GEE STAC catalog.json 获取 collection 信息
    catalog_url = f"{GEE_STAC_BASE}/catalog/catalog.json"

    try:
        # 首先尝试获取 collection 的 STAC 信息
        # 由于 GEE STAC 是静态托管的，我们直接构造 collection URL
        collection_safe = collection.replace("/", "_")
        collection_url = f"{GEE_STAC_BASE}/catalog/{collection_safe}.json"

        result = await _async_get(collection_url, timeout=aiohttp.ClientTimeout(total=15))

        if isinstance(result, dict):
            # 提取 collection 元数据
            extent = result.get("extent", {})
            spatial = extent.get("spatial", {}).get("bbox", [[]])
            temporal = extent.get("temporal", {}).get("interval", [[]])

            # 构建模拟的 scenes（STAC catalog 不提供单个场景搜索，只提供元数据）
            scenes = [{
                "id": result.get("id", collection),
                "title": result.get("title", collection),
                "description": (result.get("description", "") or "")[:500],
                "bbox": spatial[0] if spatial else [],
                "temporal_start": temporal[0][0] if temporal and temporal[0] else "",
                "temporal_end": temporal[0][1] if temporal and len(temporal[0]) > 1 else "",
                "gee_type": result.get("gee:type", ""),
                "license": result.get("license", ""),
                "providers": [p.get("name", "") for p in result.get("providers", [])],
                "keywords": result.get("keywords", [])[:10],
                "links": [
                    {"rel": link.get("rel", ""), "href": link.get("href", "")}
                    for link in result.get("links", [])[:5]
                ],
            }]

            return {
                "scenes": scenes,
                "total": 1,
                "collection": collection,
                "note": "GEE STAC 提供 collection 级元数据，具体场景需通过 GEE Python API 查询",
                "source": "Google_Earth_Engine",
            }

        return {
            "scenes": [],
            "total": 0,
            "note": f"Collection {collection} 未找到",
            "source": "Google_Earth_Engine",
        }

    except Exception as e:
        logger.warning(f"GEE STAC search error: {e}")
        return {
            "scenes": [],
            "total": 0,
            "error": str(e),
            "source": "Google_Earth_Engine",
        }


# ================================================================
# 台湾国土测绘中心 WMTS API (免费，无需认证)
# ================================================================

TAIWAN_NLSC_WMTS_BASE = "https://wmts.nlsc.gov.tw/wmts"
TAIWAN_NLSC_97_WMTS_BASE = "https://wmts.nlsc.gov.tw/97/wmts"


async def taiwan_nlsc_wmts_capabilities() -> Dict[str, Any]:
    """
    台湾国土测绘中心 WMTS GetCapabilities — 免费，无需 API Key。

    获取可用图层列表，包括正射影像、电子地图、地形图等。

    Returns:
        包含图层列表的字典
    """
    url = f"{TAIWAN_NLSC_WMTS_BASE}?SERVICE=WMTS&REQUEST=GetCapabilities"

    try:
        proxy = _get_proxy()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(url, proxy=proxy, ssl=False) as resp:
                if resp.status == 200:
                    text = await resp.text()

                    # 解析 XML 获取图层信息
                    layers = []

                    # 简单的 XML 解析（避免依赖 lxml）
                    import re

                    # 查找所有 Layer 块
                    layer_pattern = r'<Layer>(.*?)</Layer>'
                    layer_matches = re.findall(layer_pattern, text, re.DOTALL)

                    for layer_xml in layer_matches[:20]:  # 限制数量
                        # 提取 Identifier
                        id_match = re.search(r'<ows:Identifier>(.*?)</ows:Identifier>', layer_xml)
                        title_match = re.search(r'<ows:Title>(.*?)</ows:Title>', layer_xml)
                        abstract_match = re.search(r'<ows:Abstract>(.*?)</ows:Abstract>', layer_xml)

                        if id_match:
                            layer_id = id_match.group(1)
                            # 过滤出卫星/正射影像相关图层
                            if any(kw in layer_id.lower() for kw in ['photo', 'ortho', 'image', 'spot', 'satellite', 'emap']):
                                layers.append({
                                    "id": layer_id,
                                    "title": title_match.group(1) if title_match else layer_id,
                                    "abstract": abstract_match.group(1)[:200] if abstract_match else "",
                                    "wmts_url": f"{TAIWAN_NLSC_WMTS_BASE}/{layer_id}/{{TileMatrixSet}}/{{TileMatrix}}/{{TileRow}}/{{TileCol}}",
                                })

                    # 添加已知的重要图层
                    known_layers = [
                        {
                            "id": "PHOTO2",
                            "title": "正射影像(通用版)",
                            "abstract": "台湾全岛航拍正射影像，定期更新",
                            "wmts_url": f"{TAIWAN_NLSC_WMTS_BASE}/PHOTO2/EPSG:3857/{{z}}/{{y}}/{{x}}",
                        },
                        {
                            "id": "EMAP",
                            "title": "通用版电子地图",
                            "abstract": "台湾通用电子地图底图",
                            "wmts_url": f"{TAIWAN_NLSC_WMTS_BASE}/EMAP/EPSG:3857/{{z}}/{{y}}/{{x}}",
                        },
                        {
                            "id": "PHOTO_MIX",
                            "title": "混合图(正射影像+注记)",
                            "abstract": "正射影像叠加地名注记",
                            "wmts_url": f"{TAIWAN_NLSC_WMTS_BASE}/PHOTO_MIX/EPSG:3857/{{z}}/{{y}}/{{x}}",
                        },
                    ]

                    # 合并去重
                    existing_ids = {l["id"] for l in layers}
                    for kl in known_layers:
                        if kl["id"] not in existing_ids:
                            layers.append(kl)

                    return {
                        "layers": layers,
                        "total": len(layers),
                        "wmts_base": TAIWAN_NLSC_WMTS_BASE,
                        "note": "免费开放服务，无需申请即可使用",
                        "source": "Taiwan_NLSC",
                    }
                else:
                    text = await resp.text()
                    logger.warning(f"Taiwan NLSC WMTS failed: {resp.status}")
                    return {
                        "layers": [],
                        "total": 0,
                        "error": f"HTTP {resp.status}",
                        "source": "Taiwan_NLSC",
                    }
    except Exception as e:
        logger.error(f"Taiwan NLSC WMTS error: {e}")
        return {
            "layers": [],
            "total": 0,
            "error": str(e),
            "source": "Taiwan_NLSC",
        }


async def taiwan_nlsc_tile_info(
    bbox: List[float],
    layer: str = "PHOTO2",
    zoom: int = 14,
) -> Dict[str, Any]:
    """
    台湾国土测绘中心 WMTS 影像信息 — 免费，无需 API Key。

    根据 bbox 计算对应的 WMTS 瓦片地址，返回可访问的瓦片 URL。

    Args:
        bbox: 边界框 [west, south, east, north]，WGS84 坐标
        layer: 图层 ID，默认 PHOTO2 (正射影像)
        zoom: 缩放级别 (0-19)

    Returns:
        包含瓦片 URL 和预览信息的字典
    """
    import math

    if not bbox or len(bbox) != 4:
        return {
            "tiles": [],
            "error": "需要有效的 bbox 参数",
            "source": "Taiwan_NLSC",
        }

    west, south, east, north = bbox

    # 检查是否在台湾范围内
    taiwan_bbox = [119.0, 21.5, 122.5, 25.5]
    if west > taiwan_bbox[2] or east < taiwan_bbox[0] or south > taiwan_bbox[3] or north < taiwan_bbox[1]:
        return {
            "tiles": [],
            "note": "查询区域不在台湾范围内，NLSC 仅覆盖台湾地区",
            "taiwan_bbox": taiwan_bbox,
            "source": "Taiwan_NLSC",
        }

    # 计算 WMTS 瓦片坐标 (Web Mercator / EPSG:3857)
    def lat_lon_to_tile(lat, lon, zoom):
        n = 2 ** zoom
        x = int((lon + 180.0) / 360.0 * n)
        y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
        return x, y

    # 计算覆盖 bbox 的瓦片范围
    x1, y1 = lat_lon_to_tile(north, west, zoom)
    x2, y2 = lat_lon_to_tile(south, east, zoom)

    # 确保顺序正确
    min_x, max_x = min(x1, x2), max(x1, x2)
    min_y, max_y = min(y1, y2), max(y1, y2)

    # 限制瓦片数量
    max_tiles = 9
    tiles = []

    for y in range(min_y, min(max_y + 1, min_y + 3)):
        for x in range(min_x, min(max_x + 1, min_x + 3)):
            if len(tiles) >= max_tiles:
                break
            tile_url = f"{TAIWAN_NLSC_WMTS_BASE}/{layer}/EPSG:3857/{zoom}/{y}/{x}"
            tiles.append({
                "x": x,
                "y": y,
                "z": zoom,
                "url": tile_url,
            })

    return {
        "tiles": tiles,
        "total_tiles": (max_x - min_x + 1) * (max_y - min_y + 1),
        "layer": layer,
        "zoom": zoom,
        "bbox": bbox,
        "wmts_base": TAIWAN_NLSC_WMTS_BASE,
        "note": f"返回 {len(tiles)} 个瓦片 URL，可直接在浏览器中查看",
        "source": "Taiwan_NLSC",
    }

# ============================================================
# --- end inlined from _shared/geopolitical_common.py ---
# ============================================================

from loguru import logger


class USGSExplorerSkill:
    """USGS 卫星数据搜索技能"""

    @property
    def name(self) -> str:
        return "usgs_explorer"

    @property
    def description(self) -> str:
        return "通过 USGS M2M API 搜索 Landsat 卫星场景数据"

    @property
    def category(self) -> str:
        return "geopolitical_analysis"

    @property
    def dependencies(self) -> List[str]:
        return []

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "bbox": {
                "type": "array",
                "required": False,
                "description": "边界框 [west, south, east, north]",
            },
            "date_from": {
                "type": "string",
                "required": False,
                "description": "开始日期 (YYYY-MM-DD)",
            },
            "date_to": {
                "type": "string",
                "required": False,
                "description": "结束日期 (YYYY-MM-DD)",
            },
            "dataset": {
                "type": "string",
                "required": False,
                "default": "landsat_ot_c2_l2",
                "description": "USGS 数据集名称",
            },
            "max_results": {
                "type": "integer",
                "required": False,
                "default": 10,
                "description": "最大结果数",
            },
        }

    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "scenes": "array",
            "total_hits": "integer",
            "download_urls": "array",
        }

    async def execute(self, context: dict) -> dict:
        start_ts = datetime.now()
        params = context or {}

        bbox = params.get("bbox")
        date_from = params.get("date_from", "")
        date_to = params.get("date_to", "")
        dataset = params.get("dataset", "landsat_ot_c2_l2")
        max_results = min(params.get("max_results", 10), 100)

        # 智能默认日期：如果没有提供，默认查询最近90天
        if not date_from or not date_to:
            from datetime import timedelta
            date_to = datetime.now().strftime("%Y-%m-%d")
            date_from = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
            logger.info(f"[usgs_explorer] 使用默认日期范围: {date_from} ~ {date_to}")

        # 智能默认 bbox：如果没有提供，使用美国本土或从 query 推断
        if not bbox:
            # 常见地区的边界框
            region_bboxes = {
                "北京": [115.4167, 39.4167, 117.5, 41.0833],
                "上海": [120.85, 30.7, 122.2, 31.9],
                "usa": [-125.0, 24.0, -66.0, 49.0],
                "california": [-124.4, 32.5, -114.1, 42.0],
                "texas": [-106.6, 25.8, -93.5, 36.5],
            }
            query = (params.get("query") or "").lower()
            for region, region_bbox in region_bboxes.items():
                if region.lower() in query:
                    bbox = region_bbox
                    logger.info(f"[usgs_explorer] 从查询中识别地区: {region} -> bbox={bbox}")
                    break

            # 如果仍然没有 bbox，使用美国加州作为默认（Landsat 数据丰富）
            if not bbox:
                bbox = [-122.5, 37.0, -121.5, 38.5]  # 旧金山湾区
                logger.info(f"[usgs_explorer] 使用默认 bbox (旧金山湾区): {bbox}")

        try:
            # 尝试 M2M API
            scenes = await usgs_m2m_search(
                dataset=dataset,
                bbox=bbox,
                date_from=date_from,
                date_to=date_to,
                max_results=max_results,
            )

            method = "M2M_API"

            # 如果 M2M API 返回空 (无 Token), 尝试 Playwright 降级或使用静态信息
            if not scenes:
                logger.info("USGS M2M returned empty, trying Playwright fallback")
                scenes = await self._playwright_fallback(bbox, date_from, date_to, dataset)
                method = "Playwright_fallback" if scenes else "static_info"

                # 如果 Playwright 也失败，提供静态 USGS 信息
                if not scenes:
                    scenes = self._get_static_usgs_info(dataset, bbox, date_from, date_to)
                    method = "static_info"

            # 提取下载 URL
            download_urls = [
                s.get("browse_url", "") for s in scenes if s.get("browse_url")
            ]

            elapsed_ms = (datetime.now() - start_ts).total_seconds() * 1000

            return {
                "scenes": scenes,
                "total_hits": len(scenes),
                "download_urls": download_urls,
                "dataset": dataset,
                "method": method,
                "bbox": bbox,
                "date_range": f"{date_from} ~ {date_to}" if date_from else "all",
                "data_source_types": {
                    "USGS_Explorer": DATA_SOURCE_TYPES["USGS_Explorer"],
                },
                "disclaimer": DISCLAIMER,
                "execution_time_ms": elapsed_ms,
                "for_llm": {"total_hits": len(scenes), "dataset": dataset, "method": method},
            }

        except Exception as e:
            logger.error(f"USGS Explorer 查询失败: {e}", exc_info=True)
            elapsed_ms = (datetime.now() - start_ts).total_seconds() * 1000
            return {"error": f"USGS Explorer 查询失败: {str(e)}", "for_llm": {"error": str(e)}}

    async def _playwright_fallback(
        self, bbox, date_from: str, date_to: str, dataset: str
    ) -> List[Dict]:
        """Playwright 降级方案: 爬取 earthexplorer.usgs.gov"""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("playwright not installed, cannot use USGS fallback")
            return []

        scenes = []
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                page = await browser.new_page()

                # 访问 EarthExplorer 搜索页面
                url = "https://earthexplorer.usgs.gov/"
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(3000)

                # 尝试提取页面上的数据集信息
                # 注: EarthExplorer 是一个复杂的 SPA，完整爬取较困难
                # 这里仅提供基本的页面状态检查
                title = await page.title()
                logger.info(f"USGS EarthExplorer page loaded: {title}")

                # 返回提示信息
                scenes.append({
                    "entity_id": "fallback_notice",
                    "display_id": "请访问 earthexplorer.usgs.gov 进行交互式搜索",
                    "acquisition_date": "",
                    "cloud_cover": -1,
                    "browse_url": "https://earthexplorer.usgs.gov/",
                    "dataset": dataset,
                    "source": "USGS_Playwright_Fallback",
                    "note": "M2M API Token 未配置，请设置 USGS_M2M_TOKEN 或直接访问 EarthExplorer 网站",
                })

                await browser.close()

        except Exception as e:
            logger.warning(f"USGS Playwright fallback failed: {e}")

        return scenes

    def _get_static_usgs_info(self, dataset: str, bbox: List, date_from: str, date_to: str) -> List[Dict]:
        """提供 USGS 静态信息，用于无 Token 时的 fallback"""
        dataset_info = {
            "landsat_ot_c2_l2": {
                "name": "Landsat Collection 2 Level-2 Science Products",
                "description": "地表反射率和地表温度产品",
                "resolution": "30m",
                "revisit": "16 days",
                "available_since": "1982-present",
            },
            "landsat_ot_c2_l1": {
                "name": "Landsat Collection 2 Level-1 Products",
                "description": "辐射校正的星载影像",
                "resolution": "30m (15m panchromatic)",
                "revisit": "16 days",
                "available_since": "1972-present",
            },
        }

        info = dataset_info.get(dataset, {
            "name": dataset,
            "description": "USGS 卫星数据集",
            "resolution": "varies",
            "revisit": "varies",
        })

        return [{
            "entity_id": "static_info",
            "display_id": info["name"],
            "acquisition_date": "",
            "cloud_cover": -1,
            "browse_url": "https://earthexplorer.usgs.gov/",
            "dataset": dataset,
            "source": "USGS_Static_Info",
            "info": info,
            "note": (
                f"USGS M2M API Token (USGS_M2M_TOKEN) 未配置。\n"
                f"数据集: {info['name']}\n"
                f"分辨率: {info.get('resolution', 'N/A')}\n"
                f"重访周期: {info.get('revisit', 'N/A')}\n"
                f"查询范围: bbox={bbox}, 日期={date_from} ~ {date_to}\n"
                f"请访问 https://earthexplorer.usgs.gov/ 进行交互式搜索，"
                f"或申请 USGS M2M API Token: https://ers.cr.usgs.gov/"
            ),
        }]


def _main():
    """直接执行入口: python3 script.py --param1 value1
    也支持 JSON stdin: echo '{"param1": "v1"}' | python3 script.py
    """
    import argparse
    import asyncio
    import json
    import sys

    params = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read().strip()
            if raw:
                params = json.loads(raw)
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Run USGSExplorerSkill directly")
    parser.add_argument("--bbox", type=str, dest="bbox")
    args = parser.parse_args()
    for k, v in vars(args).items():
        if v is not None:
            params[k] = v

    async def run():
        skill = USGSExplorerSkill()
        result = await skill.execute(params)
        out = result if isinstance(result, dict) else {"data": str(result)}
        print(json.dumps(out, ensure_ascii=False, default=str, indent=2))

    asyncio.run(run())


if __name__ == "__main__":
    _main()
