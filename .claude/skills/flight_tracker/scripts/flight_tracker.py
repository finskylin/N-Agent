"""
航班轨迹追踪技能

提供航班和船舶的实时轨迹追踪能力，支持：
- ADS-B Exchange API（航班 ICAO 码、呼号查询、范围搜索）
- FlightRadar24（航班号查询、范围搜索）
- FlightAware（航班号查询）
- ChinaPorts（船舶 MMSI/IMO 查询、范围搜索）

支持两种查询模式：
1. 标识符查询：通过航班号、ICAO码、MMSI等查询特定目标
2. 范围搜索：通过地名或经纬度搜索区域内的所有飞机/船舶
"""

from typing import List, Dict, Any, Optional, Tuple
from playwright.async_api import async_playwright
import asyncio
import re
import os
import aiohttp
import math
from loguru import logger
# Standalone script — no cross-layer imports


# 常用地点经纬度（用于范围搜索）
LOCATION_COORDINATES = {
    # 中国大陆
    "北京": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737),
    "广州": (23.1291, 113.2644),
    "深圳": (22.5431, 114.0579),
    "成都": (30.5728, 104.0668),
    "重庆": (29.4316, 106.9123),
    "杭州": (30.2741, 120.1551),
    "南京": (32.0603, 118.7969),
    "武汉": (30.5928, 114.3055),
    "西安": (34.3416, 108.9398),
    "厦门": (24.4798, 118.0894),
    "青岛": (36.0671, 120.3826),
    "大连": (38.9140, 121.6147),
    "天津": (39.0842, 117.2009),
    "海南": (19.2000, 109.7000),
    "三亚": (18.2528, 109.5119),
    # 台湾
    "台北": (25.0330, 121.5654),
    "台湾": (23.6978, 120.9605),
    "高雄": (22.6273, 120.3014),
    "台中": (24.1477, 120.6736),
    # 港澳
    "香港": (22.3193, 114.1694),
    "澳门": (22.1987, 113.5439),
    # 日韩
    "东京": (35.6762, 139.6503),
    "首尔": (37.5665, 126.9780),
    "大阪": (34.6937, 135.5023),
    # 东南亚
    "新加坡": (1.3521, 103.8198),
    "曼谷": (13.7563, 100.5018),
    "马尼拉": (14.5995, 120.9842),
    "吉隆坡": (3.1390, 101.6869),
    "雅加达": (-6.2088, 106.8456),
    "河内": (21.0285, 105.8542),
    # 美国
    "纽约": (40.7128, -74.0060),
    "洛杉矶": (34.0522, -118.2437),
    "旧金山": (37.7749, -122.4194),
    "芝加哥": (41.8781, -87.6298),
    # 欧洲
    "伦敦": (51.5074, -0.1278),
    "巴黎": (48.8566, 2.3522),
    "法兰克福": (50.1109, 8.6821),
    "阿姆斯特丹": (52.3676, 4.9041),
    # 海峡/海域
    "台湾海峡": (24.5, 119.5),
    "南海": (15.0, 115.0),
    "东海": (30.0, 125.0),
    "黄海": (35.0, 123.0),
    "渤海": (38.5, 120.0),
    "马六甲海峡": (2.5, 101.5),
}


# IATA (2字母) → ICAO (3字母) 航空公司代码映射
# ADS-B callsign 使用 ICAO 3字母代码，航班号使用 IATA 2字母代码
# 例如: CA1831 (IATA) → CCA1831 (ICAO callsign)
IATA_TO_ICAO_AIRLINE = {
    # 中国大陆
    "CA": "CCA",   # 中国国际航空
    "MU": "CES",   # 中国东方航空
    "CZ": "CSN",   # 中国南方航空
    "HU": "CHH",   # 海南航空
    "ZH": "CSZ",   # 深圳航空
    "MF": "CXA",   # 厦门航空
    "SC": "CDG",   # 山东航空
    "FM": "CSH",   # 上海航空
    "3U": "CSC",   # 四川航空
    "GS": "GCR",   # 天津航空
    "KN": "CUA",   # 中国联合航空
    "TV": "TBA",   # 西藏航空
    "JD": "CBJ",   # 首都航空
    "BK": "OKA",   # 奥凯航空
    "QW": "CQH",   # 青岛航空
    "Y8": "YZR",   # 扬子江快运
    "8L": "LKE",   # 祥鹏航空
    "PN": "CHB",   # 西部航空
    "G5": "HXA",   # 华夏航空
    "AQ": "CSC",   # 九元航空 (注: 与川航相同 ICAO, 实际为 AQ9)
    "9C": "CQH",   # 春秋航空
    "EU": "UEA",   # 成都航空
    "GJ": "CDC",   # 长龙航空
    "GT": "GUL",   # 桂林航空
    "DZ": "EPA",   # 东海航空
    # 台湾
    "CI": "CAL",   # 中华航空
    "BR": "EVA",   # 长荣航空
    "B7": "UIA",   # 立荣航空
    "IT": "TTW",   # 台湾虎航
    "AE": "MDA",   # 华信航空
    "DA": "APJ",   # 星宇航空
    # 香港/澳门
    "CX": "CPA",   # 国泰航空
    "HX": "CRK",   # 香港航空
    "KA": "HDA",   # 港龙航空
    "UO": "HKE",   # 香港快运
    "NX": "AMU",   # 澳门航空
    # 日本
    "NH": "ANA",   # 全日空
    "JL": "JAL",   # 日本航空
    "MM": "APJ",   # 乐桃航空
    "GK": "JJP",   # 捷星日本
    "BC": "SKY",   # 天马航空
    "IJ": "SJO",   # Spring Japan
    # 韩国
    "KE": "KAL",   # 大韩航空
    "OZ": "AAR",   # 韩亚航空
    "BX": "ABL",   # 釜山航空
    "TW": "TWB",   # 德威航空
    "ZE": "ESR",   # 易斯达航空
    "7C": "JJA",   # 济州航空
    "LJ": "JNA",   # 真航空
    # 东南亚
    "SQ": "SIA",   # 新加坡航空
    "TR": "TGW",   # 酷航
    "TG": "THA",   # 泰国国际航空
    "FD": "AIQ",   # 亚洲航空(泰)
    "VN": "HVN",   # 越南航空
    "PR": "PAL",   # 菲律宾航空
    "5J": "CEB",   # 宿务太平洋
    "GA": "GIA",   # 印尼鹰航
    "AK": "AXM",   # 亚洲航空
    "MH": "MAS",   # 马来西亚航空
    # 中东
    "EK": "UAE",   # 阿联酋航空
    "QR": "QTR",   # 卡塔尔航空
    "EY": "ETD",   # 阿提哈德航空
    "TK": "THY",   # 土耳其航空
    "SV": "SVA",   # 沙特航空
    "EI": "EIN",   # 爱尔兰航空
    # 欧洲
    "BA": "BAW",   # 英国航空
    "LH": "DLH",   # 汉莎航空
    "AF": "AFR",   # 法国航空
    "KL": "KLM",   # 荷兰皇家航空
    "LX": "SWR",   # 瑞士航空
    "OS": "AUA",   # 奥地利航空
    "SK": "SAS",   # 北欧航空
    "AY": "FIN",   # 芬兰航空
    "IB": "IBE",   # 伊比利亚航空
    "TP": "TAP",   # 葡萄牙航空
    "SU": "AFL",   # 俄罗斯航空
    "FR": "RYR",   # 瑞安航空
    "U2": "EZY",   # 易捷航空
    "W6": "WZZ",   # 维兹航空
    # 北美
    "AA": "AAL",   # 美国航空
    "DL": "DAL",   # 达美航空
    "UA": "UAL",   # 联合航空
    "WN": "SWA",   # 西南航空
    "AC": "ACA",   # 加拿大航空
    "AS": "ASA",   # 阿拉斯加航空
    "B6": "JBU",   # 捷蓝航空
    "NK": "NKS",   # 精神航空
    "F9": "FFT",   # 边疆航空
    "WS": "WJA",   # 西捷航空
    "AM": "AMX",   # 墨西哥航空
    # 南美
    "LA": "LAN",   # 南美航空
    "G3": "GLO",   # 高尔航空
    "AV": "AVA",   # 哥伦比亚航空
    # 大洋洲
    "QF": "QFA",   # 澳洲航空
    "JQ": "JST",   # 捷星航空
    "VA": "VOZ",   # 维珍澳大利亚
    "NZ": "ANZ",   # 新西兰航空
    # 非洲
    "ET": "ETH",   # 埃塞俄比亚航空
    "SA": "SAA",   # 南非航空
    "MS": "MSR",   # 埃及航空
    # 印度
    "AI": "AIC",   # 印度航空
    "6E": "IGO",   # 靛蓝航空
    "UK": "UKA",   # Vistara
    "SG": "SEJ",   # SpiceJet
    # 货运
    "FX": "FDX",   # 联邦快递
    "5X": "UPS",   # UPS
    "CV": "CLX",   # 卢森堡货运
    "PO": "PAC",   # 极地货运
}


class FlightTrackerSkill:
    """航班和船舶轨迹追踪技能"""

    name = "flight_tracker"
    description = "航班和船舶实时轨迹追踪技能。支持两种模式：1) 标识符查询（航班号如CA1831/MU5678、ICAO码如71BD54、MMSI等）；2) 范围搜索（输入地名如'台北周边'或经纬度，搜索区域内所有飞机/船舶）。数据源：ADS-B Exchange、FlightRadar24、ChinaPorts。"

    @property
    def input_schema(self) -> Dict[str, Any]:
        """定义输入参数 Schema"""
        return {
            "query": {
                "type": "string",
                "description": "查询内容。支持：1) 标识符（航班号 CA1831、ICAO码 a43c7a、MMSI 412345678）；2) 地名范围搜索（如 '台北周边'、'上海附近飞机'、'台湾海峡船舶'）；3) 经纬度（如 '25.03,121.56'）"
            },
            "tracking_type": {
                "type": "string",
                "description": "追踪类型：flight/ship/auto（自动检测），默认 auto"
            },
            "search_mode": {
                "type": "string",
                "description": "搜索模式：identifier（标识符查询）/ area（范围搜索）/ auto（自动检测），默认 auto"
            },
            "radius_km": {
                "type": "integer",
                "description": "范围搜索半径（公里），默认 200km，最大 500km"
            },
            "include_trace": {
                "type": "boolean",
                "description": "是否包含历史轨迹数据，默认 true（仅标识符查询有效）"
            },
            "max_results": {
                "type": "integer",
                "description": "最大返回结果数量，默认 50"
            }
        }

    # 航班追踪网站配置
    FLIGHT_TRACKING_SITES = {
        "adsb_exchange": {
            "url": "https://globe.adsbexchange.com/?icao={query}",
            "search_url": "https://globe.adsbexchange.com/?find={query}",
            "description": "ADS-B Exchange 航班追踪（无过滤）",
            "requires_proxy": True,
        },
        "flightradar24": {
            "url": "https://www.flightradar24.com/data/flights/{query}",
            "search_url": "https://www.flightradar24.com/data/flights?search={query}",
            "description": "FlightRadar24 航班追踪",
            "requires_proxy": True,
        },
        "flightaware": {
            "url": "https://flightaware.com/live/flight/{query}",
            "search_url": "https://flightaware.com/live/findflight?origin=&destination=&searchterm={query}",
            "description": "FlightAware 航班追踪",
            "requires_proxy": True,
        },
        "planefinder": {
            "url": "https://planefinder.net/flight/{query}",
            "search_url": "https://planefinder.net/search?q={query}",
            "description": "Plane Finder 航班追踪",
            "requires_proxy": True,
        },
    }

    # 船舶追踪网站配置
    SHIP_TRACKING_SITES = {
        "chinaports": {
            "url": "https://ship.chinaports.com/",
            "search_url": "https://ship.chinaports.com/ShipTrack/shipDetail?mmsi={query}",
            "description": "中国港口网船舶追踪",
            "requires_proxy": False,
        },
        "marinetraffic": {
            "url": "https://www.marinetraffic.com/",
            "search_url": "https://www.marinetraffic.com/en/ais/index/search/all/keyword:{query}",
            "description": "Marine Traffic 船舶追踪",
            "requires_proxy": True,
        },
        "shipxy": {
            "url": "https://www.shipxy.com/",
            "search_url": "https://www.shipxy.com/ship/{query}",
            "description": "船讯网 AIS 船舶追踪",
            "requires_proxy": False,
        },
    }

    async def execute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行航班/船舶追踪查询

        支持两种模式：
        1. 标识符查询：通过航班号、ICAO码、MMSI等查询特定目标
        2. 范围搜索：通过地名或经纬度搜索区域内的所有飞机/船舶
        """
        query = params.get("query", "")
        if not query:
            return {"error": "Query is required", "for_llm": {"error": "缺少必需参数 query"}}

        tracking_type = params.get("tracking_type", "auto")
        search_mode = params.get("search_mode", "auto")
        radius_km = min(params.get("radius_km", 200), 500)  # 最大 500km
        include_trace = params.get("include_trace", True)
        max_results = params.get("max_results", 50)

        # 代理配置
        proxy_server = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")

        # 自动检测搜索模式
        if search_mode == "auto":
            search_mode = self._detect_search_mode(query)

        # 自动检测追踪类型
        if tracking_type == "auto":
            tracking_type = self._detect_tracking_type(query)

        logger.info(f"[FlightTracker] Query: {query}, Type: {tracking_type}, Mode: {search_mode}")

        results = []

        if search_mode == "area":
            # 范围搜索模式：只获取位置（markers），不获取轨迹（tracks）
            # 轨迹仅在标识符查询时获取（如 "780172 查询轨迹"）
            area_include_trace = False
            coordinates = self._parse_location(query)
            if coordinates:
                lat, lon = coordinates
                logger.info(f"[FlightTracker] Area search: ({lat}, {lon}), radius: {radius_km}km, include_trace={area_include_trace}")

                if tracking_type == "flight":
                    results = await self._search_flights_in_area(lat, lon, radius_km, proxy_server, max_results, area_include_trace)
                elif tracking_type == "ship":
                    results = await self._search_ships_in_area(lat, lon, radius_km, proxy_server, max_results)
                else:
                    # 同时搜索飞机和船舶
                    flight_task = self._search_flights_in_area(lat, lon, radius_km, proxy_server, max_results // 2, area_include_trace)
                    ship_task = self._search_ships_in_area(lat, lon, radius_km, proxy_server, max_results // 2)
                    flight_results, ship_results = await asyncio.gather(flight_task, ship_task)
                    results = flight_results + ship_results
            else:
                return {"error": f"无法解析位置: {query}", "for_llm": {"error": f"无法解析位置: {query}，请使用地名（如'台北'）或经纬度（如'25.03,121.56'）"}}
        else:
            # 标识符查询模式
            results = await self._search_tracking_sites(
                query=query,
                tracking_type=tracking_type,
                proxy_server=proxy_server,
                include_trace=include_trace,
            )

        # 限制返回数量
        if len(results) > max_results:
            results = results[:max_results]

        # 构建 UI 组件
        components = self._build_ui_components(results, tracking_type, query)

        # 返回结果
        return {
            "results": results,
            "components": components,
            "tracking_type": tracking_type,
            "search_mode": search_mode,
            "query": query,
            "total_count": len(results),
            "for_llm": {
                "query": query,
                "tracking_type": tracking_type,
                "search_mode": search_mode,
                "total_count": len(results),
                "message": f"找到 {len(results)} 个{('航班' if tracking_type == 'flight' else '船舶' if tracking_type == 'ship' else '目标')}",
            },
        }

    def _detect_search_mode(self, query: str) -> str:
        """自动检测搜索模式"""
        query_lower = query.lower()

        # 范围搜索关键词
        area_keywords = ["周边", "附近", "区域", "范围", "海域", "空域", "上空", "nearby", "around", "area"]
        for kw in area_keywords:
            if kw in query_lower:
                return "area"

        # 检查是否是已知地名
        for location in LOCATION_COORDINATES.keys():
            if location in query:
                return "area"

        # 检查是否是经纬度格式
        coord_pattern = r'^-?\d+\.?\d*\s*[,，]\s*-?\d+\.?\d*$'
        if re.match(coord_pattern, query.strip()):
            return "area"

        # 默认为标识符查询
        return "identifier"

    def _parse_location(self, query: str) -> Optional[Tuple[float, float]]:
        """解析位置，返回 (纬度, 经度)"""
        query_clean = query.strip()

        # 1. 尝试解析经纬度格式
        coord_patterns = [
            r'^(-?\d+\.?\d*)\s*[,，]\s*(-?\d+\.?\d*)$',  # 25.03,121.56
            r'(\d+\.?\d*)[°]?\s*([NS])\s*[,，]?\s*(\d+\.?\d*)[°]?\s*([EW])',  # 25.03N, 121.56E
        ]

        for pattern in coord_patterns:
            match = re.match(pattern, query_clean, re.IGNORECASE)
            if match:
                groups = match.groups()
                if len(groups) == 2:
                    return (float(groups[0]), float(groups[1]))
                elif len(groups) == 4:
                    lat = float(groups[0]) * (-1 if groups[1].upper() == 'S' else 1)
                    lon = float(groups[2]) * (-1 if groups[3].upper() == 'W' else 1)
                    return (lat, lon)

        # 2. 从地名词典查找
        for location, coords in LOCATION_COORDINATES.items():
            if location in query_clean:
                return coords

        # 3. 尝试提取地名（去掉范围搜索关键词）
        clean_query = query_clean
        for kw in ["周边", "附近", "区域", "范围", "海域", "空域", "上空", "飞机", "船舶", "航班"]:
            clean_query = clean_query.replace(kw, "").strip()

        for location, coords in LOCATION_COORDINATES.items():
            if location in clean_query or clean_query in location:
                return coords

        return None

    # ==================== 范围搜索方法 ====================

    async def _search_flights_in_area(
        self,
        lat: float,
        lon: float,
        radius_km: int,
        proxy_server: str = None,
        max_results: int = 50,
        include_trace: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        搜索指定区域内的所有飞机

        使用 ADS-B Exchange API 和 FlightRadar24 API 并行搜索
        """
        results = []

        # 并行调用两个数据源
        adsb_task = self._search_adsb_area(lat, lon, radius_km, proxy_server, include_trace)
        fr24_task = self._search_fr24_area(lat, lon, radius_km, proxy_server)

        adsb_results, fr24_results = await asyncio.gather(adsb_task, fr24_task, return_exceptions=True)

        # 处理 ADS-B 结果
        if isinstance(adsb_results, list):
            results.extend(adsb_results)
            logger.info(f"[ADSB Area] Found {len(adsb_results)} aircraft")
        elif isinstance(adsb_results, Exception):
            logger.error(f"[ADSB Area] Error: {adsb_results}")

        # 处理 FR24 结果（去重）
        if isinstance(fr24_results, list):
            existing_icao = {r.get("flight_data", {}).get("icao_hex", "").upper() for r in results}
            for fr24_result in fr24_results:
                icao = fr24_result.get("flight_data", {}).get("icao_hex", "").upper()
                if icao and icao not in existing_icao:
                    results.append(fr24_result)
                    existing_icao.add(icao)
            logger.info(f"[FR24 Area] Found {len(fr24_results)} aircraft, added unique ones")
        elif isinstance(fr24_results, Exception):
            logger.error(f"[FR24 Area] Error: {fr24_results}")

        # 按距离排序
        results.sort(key=lambda x: x.get("flight_data", {}).get("distance_km", 9999))

        return results[:max_results]

    async def _search_adsb_area(
        self,
        lat: float,
        lon: float,
        radius_km: int,
        proxy_server: str = None,
        include_trace: bool = True,
    ) -> List[Dict[str, Any]]:
        """使用 ADS-B Exchange API 搜索区域内飞机"""
        results = []

        # 转换为海里（1海里 = 1.852公里）
        radius_nm = int(radius_km / 1.852)

        api_url = f"https://api.adsb.one/v2/point/{lat}/{lon}/{radius_nm}"

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, headers=headers, proxy=proxy_server, timeout=30) as response:
                    if response.status == 200:
                        data = await response.json()
                        aircraft_list = data.get("ac", [])

                        for ac in aircraft_list:
                            flight_data = self._parse_adsb_aircraft(ac)

                            # 计算距离
                            ac_lat = ac.get("lat")
                            ac_lon = ac.get("lon")
                            if ac_lat and ac_lon:
                                distance = self._haversine_distance(lat, lon, ac_lat, ac_lon)
                                flight_data["distance_km"] = round(distance, 1)
                                flight_data["distance_nm"] = round(distance / 1.852, 1)

                            # 添加方位
                            if ac.get("dir") is not None:
                                flight_data["bearing"] = f"{ac.get('dir'):.0f}°"

                            display_name = flight_data.get("flight_number") or flight_data.get("callsign") or flight_data.get("icao_hex") or "Unknown"

                            results.append({
                                "title": f"航班 {display_name}",
                                "link": f"https://globe.adsbexchange.com/?icao={flight_data.get('icao_hex', '').lower()}",
                                "snippet": self._format_flight_summary(flight_data),
                                "source": "ADS-B Exchange",
                                "is_tracking_site": True,
                                "flight_data": flight_data,
                                "tracking_type": "flight",
                                "search_type": "area",
                            })

                        logger.info(f"[ADSB Area] API returned {len(aircraft_list)} aircraft")

                        # 并行获取所有飞机的轨迹（限制前 20 架，避免过多请求）
                        if include_trace and results:
                            trace_targets = [
                                r for r in results
                                if r.get("flight_data", {}).get("icao_hex")
                            ][:20]

                            async def fetch_trace_for(result):
                                icao = result["flight_data"]["icao_hex"]
                                trace = await self._fetch_adsb_trace(session, icao, headers, proxy_server)
                                if trace:
                                    result["flight_data"]["trace"] = trace

                            trace_tasks = [fetch_trace_for(r) for r in trace_targets]
                            await asyncio.gather(*trace_tasks, return_exceptions=True)
                            traced = sum(1 for r in trace_targets if r.get("flight_data", {}).get("trace"))
                            logger.info(f"[ADSB Area] Fetched traces: {traced}/{len(trace_targets)}")

                    else:
                        logger.warning(f"[ADSB Area] API returned status {response.status}")

        except Exception as e:
            logger.error(f"[ADSB Area] Error: {e}")

        return results

    async def _search_fr24_area(
        self,
        lat: float,
        lon: float,
        radius_km: int,
        proxy_server: str = None,
    ) -> List[Dict[str, Any]]:
        """使用 FlightRadar24 API 搜索区域内飞机"""
        results = []

        # 计算边界框
        # 1度纬度约111公里，1度经度在赤道约111公里，随纬度增加而减少
        lat_delta = radius_km / 111.0
        lon_delta = radius_km / (111.0 * math.cos(math.radians(lat)))

        bounds = {
            "lat_north": lat + lat_delta,
            "lat_south": lat - lat_delta,
            "lon_west": lon - lon_delta,
            "lon_east": lon + lon_delta,
        }

        # FR24 数据 API
        api_url = f"https://data-cloud.flightradar24.com/zones/fcgi/feed.js?bounds={bounds['lat_north']:.4f},{bounds['lat_south']:.4f},{bounds['lon_west']:.4f},{bounds['lon_east']:.4f}&faa=1&satellite=1&mlat=1&flarm=1&adsb=1&gnd=0&air=1&vehicles=0&estimated=1&maxage=14400&gliders=0&stats=0"

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://www.flightradar24.com/",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, headers=headers, proxy=proxy_server, timeout=30) as response:
                    if response.status == 200:
                        data = await response.json()

                        # FR24 返回的数据格式：key 是飞机 ID，value 是数组
                        for key, value in data.items():
                            # 跳过元数据字段
                            if key in ["full_count", "version", "stats"]:
                                continue

                            if isinstance(value, list) and len(value) >= 14:
                                try:
                                    flight_data = {
                                        "icao_hex": value[0] if value[0] else "",
                                        "callsign": (value[16] or value[13] or "").strip() if len(value) > 16 else "",
                                        "registration": value[9] if len(value) > 9 else None,
                                        "aircraft": value[8] if len(value) > 8 else None,
                                        "position": {
                                            "lat": value[1],
                                            "lon": value[2],
                                        } if value[1] and value[2] else None,
                                        "heading": f"{value[3]}°" if value[3] else None,
                                        "altitude": f"{value[4]} ft" if value[4] else None,
                                        "speed": f"{value[5]} kts" if value[5] else None,
                                        "origin": value[11] if len(value) > 11 else None,
                                        "destination": value[12] if len(value) > 12 else None,
                                        "status": "地面" if value[14] else "飞行中" if value[4] else "未知",
                                        "source": "FlightRadar24",
                                    }

                                    # 计算距离
                                    if value[1] and value[2]:
                                        distance = self._haversine_distance(lat, lon, value[1], value[2])
                                        flight_data["distance_km"] = round(distance, 1)

                                    display_name = flight_data.get("flight_number") or flight_data.get("callsign") or flight_data.get("icao_hex") or "Unknown"

                                    results.append({
                                        "title": f"航班 {display_name}",
                                        "link": f"https://www.flightradar24.com/{key}",
                                        "snippet": self._format_flight_summary(flight_data),
                                        "source": "FlightRadar24",
                                        "is_tracking_site": True,
                                        "flight_data": flight_data,
                                        "tracking_type": "flight",
                                        "search_type": "area",
                                    })
                                except Exception as e:
                                    logger.debug(f"[FR24 Area] Parse error for {key}: {e}")

                        logger.info(f"[FR24 Area] API returned {len(results)} aircraft")
                    else:
                        logger.warning(f"[FR24 Area] API returned status {response.status}")

        except Exception as e:
            logger.error(f"[FR24 Area] Error: {e}")

        return results

    async def _search_ships_in_area(
        self,
        lat: float,
        lon: float,
        radius_km: int,
        proxy_server: str = None,
        max_results: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        搜索指定区域内的所有船舶

        使用 ChinaPorts Playwright 抓取
        """
        results = []

        # 计算边界框
        lat_delta = radius_km / 111.0
        lon_delta = radius_km / (111.0 * math.cos(math.radians(lat)))

        bounds = {
            "min_lat": lat - lat_delta,
            "max_lat": lat + lat_delta,
            "min_lon": lon - lon_delta,
            "max_lon": lon + lon_delta,
        }

        try:
            # 使用 ChinaPorts 搜索
            chinaports_results = await self._search_chinaports_area(bounds, proxy_server)
            results.extend(chinaports_results)
            logger.info(f"[Ship Area] Found {len(chinaports_results)} ships from ChinaPorts")

        except Exception as e:
            logger.error(f"[Ship Area] Error: {e}")

        # 计算距离并排序
        for result in results:
            ship_data = result.get("ship_data", {})
            position = ship_data.get("position")
            if position:
                ship_lat = position.get("lat")
                ship_lon = position.get("lon")
                if ship_lat and ship_lon:
                    distance = self._haversine_distance(lat, lon, ship_lat, ship_lon)
                    ship_data["distance_km"] = round(distance, 1)

        results.sort(key=lambda x: x.get("ship_data", {}).get("distance_km", 9999))

        return results[:max_results]

    async def _search_chinaports_area(
        self,
        bounds: Dict[str, float],
        proxy_server: str = None,
    ) -> List[Dict[str, Any]]:
        """使用 ChinaPorts 搜索区域内船舶"""
        results = []

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox"],
                )

                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    viewport={"width": 1920, "height": 1080},
                )

                page = await context.new_page()

                # 访问 ChinaPorts 地图页面，带边界参数
                center_lat = (bounds["min_lat"] + bounds["max_lat"]) / 2
                center_lon = (bounds["min_lon"] + bounds["max_lon"]) / 2
                url = f"https://ship.chinaports.com/ShipTrack/map?lat={center_lat}&lon={center_lon}&zoom=8"

                logger.info(f"[ChinaPorts] Fetching: {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)

                # 尝试从页面提取船舶数据
                ship_data_list = await page.evaluate("""
                    () => {
                        const ships = [];

                        // 尝试从高德地图标记中提取
                        if (window.AMap && window.AMap.bindbindbindbindShip) {
                            // 高德地图方式
                        }

                        // 尝试从页面元素中提取
                        const markers = document.querySelectorAll('[class*="ship"], [class*="marker"], [class*="vessel"]');
                        markers.forEach(marker => {
                            const text = marker.innerText || marker.getAttribute('title') || '';
                            if (text) {
                                ships.push({
                                    name: text.substring(0, 50),
                                    raw: text
                                });
                            }
                        });

                        // 尝试从全局变量中提取
                        if (window.shipData || window.vessels) {
                            const data = window.shipData || window.vessels;
                            if (Array.isArray(data)) {
                                data.forEach(s => ships.push(s));
                            }
                        }

                        return ships;
                    }
                """)

                # 处理提取的数据
                for ship in ship_data_list:
                    if isinstance(ship, dict):
                        results.append({
                            "title": f"船舶 {ship.get('name', 'Unknown')}",
                            "link": "https://ship.chinaports.com/",
                            "snippet": f"MMSI: {ship.get('mmsi', 'N/A')} | 类型: {ship.get('type', 'N/A')}",
                            "source": "ChinaPorts",
                            "is_tracking_site": True,
                            "ship_data": ship,
                            "tracking_type": "ship",
                            "search_type": "area",
                        })

                await browser.close()

        except Exception as e:
            logger.error(f"[ChinaPorts Area] Error: {e}")

        return results

    def _haversine_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """计算两点间的距离（公里）- Haversine 公式"""
        R = 6371  # 地球半径（公里）

        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)

        a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        return R * c

    def _detect_tracking_type(self, query: str) -> str:
        """自动检测追踪类型"""
        query_lower = query.lower()

        # 航班关键词
        flight_keywords = [
            "航班", "flight", "飞机", "航线", "航空",
            "ads-b", "adsb", "flightradar", "fr24", "icao"
        ]

        # 航班号模式 (如 CA123, MU5678)
        flight_number_pattern = r'\b[A-Z]{2,3}\d{1,4}\b'

        # ICAO 24位地址码模式 (6位十六进制)
        icao_hex_pattern = r'\b[0-9a-fA-F]{6}\b'

        # 船舶关键词
        ship_keywords = [
            "船舶", "ship", "vessel", "轮船", "货轮",
            "mmsi", "imo", "港口", "marinetraffic"
        ]

        # MMSI 模式 (9位数字)
        mmsi_pattern = r'\b\d{9}\b'

        # 检测航班
        for kw in flight_keywords:
            if kw in query_lower:
                return "flight"

        if re.search(flight_number_pattern, query.upper()):
            return "flight"

        if re.search(icao_hex_pattern, query_lower):
            # 排除航班号格式
            if not re.search(r'^[A-Z]{2,3}\d{1,4}$', query.upper()):
                return "flight"

        # 检测船舶
        for kw in ship_keywords:
            if kw in query_lower:
                return "ship"

        if re.search(r'mmsi[=:\s]*\d{9}', query_lower):
            return "ship"

        # 默认航班
        return "flight"

    async def _search_tracking_sites(
        self,
        query: str,
        tracking_type: str,
        proxy_server: str = None,
        include_trace: bool = True,
    ) -> List[Dict[str, Any]]:
        """搜索航班/船舶追踪网站"""
        results = []

        # 提取查询目标
        query_targets = self._extract_query_targets(query, tracking_type)

        if not query_targets:
            query_targets = [{"type": "query", "value": query}]

        logger.info(f"[FlightTracker] Query targets: {query_targets}")

        if tracking_type == "flight":
            results = await self._fetch_flight_data(query_targets, proxy_server, include_trace)
        elif tracking_type == "ship":
            results = await self._fetch_ship_data(query_targets, proxy_server)

        # 添加追踪网站链接
        sites = self.FLIGHT_TRACKING_SITES if tracking_type == "flight" else self.SHIP_TRACKING_SITES

        for target in query_targets:
            target_value = target["value"]
            for site_name, site_config in sites.items():
                try:
                    search_url = site_config.get("search_url", site_config["url"]).format(query=target_value)
                    results.append({
                        "title": f"{site_config['description']} - {target_value}",
                        "link": search_url,
                        "snippet": f"点击查看 {target_value} 在 {site_name} 上的实时追踪信息",
                        "source": site_name,
                        "is_tracking_site": True,
                        "is_link_only": True,
                        "query_target": target_value,
                        "tracking_type": tracking_type,
                    })
                except Exception as e:
                    logger.warning(f"Failed to add tracking site {site_name}: {e}")

        return results

    def _extract_query_targets(self, query: str, tracking_type: str) -> List[Dict[str, str]]:
        """提取查询目标"""
        targets = []
        seen = set()

        if tracking_type == "flight":
            # 提取 ICAO 码
            icao_codes = self._extract_all_icao_hex(query)
            for icao in icao_codes:
                if icao not in seen:
                    seen.add(icao)
                    targets.append({"type": "icao", "value": icao})

            # 提取航班号
            flight_numbers = self._extract_all_flight_numbers(query)
            for fn in flight_numbers:
                if fn not in seen:
                    seen.add(fn)
                    targets.append({"type": "flight_number", "value": fn})

            # 提取呼号
            callsigns = self._extract_all_callsigns(query)
            for cs in callsigns:
                if cs not in seen:
                    seen.add(cs)
                    targets.append({"type": "callsign", "value": cs})

        elif tracking_type == "ship":
            # 提取 MMSI
            mmsi_codes = self._extract_all_mmsi(query)
            for mmsi in mmsi_codes:
                if mmsi not in seen:
                    seen.add(mmsi)
                    targets.append({"type": "mmsi", "value": mmsi})

            # 提取 IMO
            imo_codes = self._extract_all_imo(query)
            for imo in imo_codes:
                if imo not in seen:
                    seen.add(imo)
                    targets.append({"type": "imo", "value": imo})

        return targets

    # ==================== 航班数据获取方法 ====================

    async def _fetch_flight_data(
        self,
        targets: List[Dict[str, str]],
        proxy_server: str = None,
        include_trace: bool = True,
    ) -> List[Dict[str, Any]]:
        """获取航班数据"""
        results = []

        async def fetch_single(target):
            target_type = target["type"]
            target_value = target["value"]
            flight_data = None

            if target_type == "icao":
                flight_data = await self._fetch_adsbexchange_data(
                    target_value, proxy_server, include_trace, query_type="icao"
                )
            elif target_type == "flight_number":
                # 优先 ADS-B（快速 API 调用，通过 IATA→ICAO callsign 转换）
                flight_data = await self._fetch_adsbexchange_data(
                    target_value, proxy_server, include_trace, query_type="flight_number"
                )
                # ADS-B 无结果时，降级到 FR24 / FlightAware（Playwright，较慢）
                if not flight_data or flight_data.get("error"):
                    logger.info(f"[FlightTracker] ADS-B no result for {target_value}, trying FR24...")
                    flight_data = await self._fetch_flightradar24_data(target_value, proxy_server)
                if not flight_data or flight_data.get("error"):
                    flight_data = await self._fetch_flightaware_data(target_value, proxy_server)
            elif target_type == "callsign":
                flight_data = await self._fetch_adsbexchange_data(
                    target_value, proxy_server, include_trace, query_type="callsign"
                )
            else:
                flight_data = await self._fetch_adsbexchange_data(
                    target_value, proxy_server, include_trace
                )

            return {"target": target, "data": flight_data}

        # 并行查询
        tasks = [fetch_single(t) for t in targets]
        fetch_results = await asyncio.gather(*tasks, return_exceptions=True)

        for fetch_result in fetch_results:
            if isinstance(fetch_result, Exception):
                logger.error(f"Flight fetch failed: {fetch_result}")
                continue

            target = fetch_result["target"]
            flight_data = fetch_result["data"]
            target_value = target["value"]

            if flight_data and not flight_data.get("error"):
                display_name = flight_data.get("flight_number") or flight_data.get("callsign") or target_value
                summary = self._format_flight_summary(flight_data)

                results.append({
                    "title": f"航班 {display_name} 实时追踪数据",
                    "link": flight_data.get("source_url", ""),
                    "snippet": summary,
                    "source": flight_data.get("source", "flight_tracker"),
                    "is_tracking_site": True,
                    "flight_data": flight_data,
                    "query_target": target_value,
                    "tracking_type": "flight",
                })
                logger.info(f"✅ Got flight data for {target_value}")

        return results

    def _flight_number_to_callsigns(self, flight_number: str) -> List[str]:
        """
        将航班号（IATA格式）转为可能的 ADS-B callsign 列表

        航班号使用 IATA 2字母代码 (CA1831)，
        ADS-B callsign 使用 ICAO 3字母代码 (CCA1831)。

        返回多个候选 callsign（ICAO转换 + 原始航班号），因为有些航空公司
        ADS-B 中也使用 IATA 代码。

        Args:
            flight_number: 航班号，如 "CA1831", "MU5678"

        Returns:
            候选 callsign 列表，如 ["CCA1831", "CA1831"]
        """
        flight_number = flight_number.upper().strip()
        # 匹配航班号格式：2字母 + 数字 (CA1831) 或 1字母1数字 + 数字 (3U1234) 或 3字母 + 数字 (CCA1831)
        match = re.match(r'^([A-Z]{2,3}|\d[A-Z])(\d{1,4})$', flight_number)
        if not match:
            return [flight_number]

        airline_code = match.group(1)
        number = match.group(2)

        candidates = []

        # 1. IATA→ICAO 转换
        icao_code = IATA_TO_ICAO_AIRLINE.get(airline_code)
        if icao_code:
            candidates.append(f"{icao_code}{number}")

        # 2. 原始航班号也作为候选（有些 ADS-B 数据直接用 IATA 代码）
        candidates.append(flight_number)

        # 3. 如果 airline_code 本身就是 3 字母（可能已经是 ICAO），直接用
        if len(airline_code) == 3 and flight_number not in candidates:
            candidates.append(flight_number)

        return candidates

    # 反向映射缓存（ICAO 3字母 → IATA 2字母），启动时自动构建
    _ICAO_TO_IATA_AIRLINE = None

    @classmethod
    def _get_icao_to_iata_map(cls) -> Dict[str, str]:
        """获取 ICAO→IATA 反向映射（懒加载）"""
        if cls._ICAO_TO_IATA_AIRLINE is None:
            cls._ICAO_TO_IATA_AIRLINE = {v: k for k, v in IATA_TO_ICAO_AIRLINE.items()}
        return cls._ICAO_TO_IATA_AIRLINE

    def _callsign_to_flight_number(self, callsign: str) -> Optional[str]:
        """
        将 ADS-B callsign (ICAO格式) 转为航班号 (IATA格式)

        例如: CCA1831 → CA1831, CES5678 → MU5678

        如果无法转换（未知 ICAO 代码），返回 None
        """
        callsign = callsign.upper().strip()
        if not callsign:
            return None

        icao_to_iata = self._get_icao_to_iata_map()

        # 尝试 3字母前缀匹配
        if len(callsign) >= 4:
            prefix3 = callsign[:3]
            rest = callsign[3:]
            if prefix3 in icao_to_iata and rest.isdigit():
                return f"{icao_to_iata[prefix3]}{rest}"

        # 尝试 2字母前缀匹配（部分 callsign 直接用 IATA 代码）
        if len(callsign) >= 3:
            prefix2 = callsign[:2]
            rest = callsign[2:]
            if prefix2 in IATA_TO_ICAO_AIRLINE and rest.isdigit():
                return callsign  # 已经是 IATA 格式

        return None

    async def _fetch_adsbexchange_data(
        self,
        query: str,
        proxy_server: str = None,
        include_trace: bool = True,
        query_type: str = "auto",
    ) -> Optional[Dict[str, Any]]:
        """
        从 ADS-B Exchange API 获取航班数据

        Args:
            query: 查询值（ICAO hex / callsign / 航班号）
            proxy_server: 代理服务器
            include_trace: 是否获取轨迹
            query_type: 查询类型提示 ("icao" / "callsign" / "flight_number" / "auto")
        """
        try:
            # 判断查询类型
            is_icao = query_type == "icao" or bool(re.match(r'^[0-9a-fA-F]{6}$', query))

            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/json",
            }

            proxy = proxy_server if proxy_server else None

            async with aiohttp.ClientSession() as session:
                flight_data = None

                if is_icao:
                    # ICAO hex 查询
                    api_url = f"https://api.adsb.one/v2/icao/{query.upper()}"
                    logger.info(f"[ADS-B] Fetching by ICAO: {api_url}")
                    flight_data = await self._adsb_api_call(session, api_url, headers, proxy, include_trace)

                elif query_type == "flight_number":
                    # 航班号查询：转为 callsign 候选列表依次尝试
                    callsign_candidates = self._flight_number_to_callsigns(query)
                    logger.info(f"[ADS-B] Flight number {query} → callsign candidates: {callsign_candidates}")

                    for callsign in callsign_candidates:
                        api_url = f"https://api.adsb.one/v2/callsign/{callsign}"
                        logger.info(f"[ADS-B] Trying callsign: {api_url}")
                        flight_data = await self._adsb_api_call(session, api_url, headers, proxy, include_trace)
                        if flight_data:
                            # 记录匹配的航班号
                            flight_data["flight_number"] = query
                            break

                else:
                    # callsign 或通用查询
                    api_url = f"https://api.adsb.one/v2/callsign/{query.upper()}"
                    logger.info(f"[ADS-B] Fetching by callsign: {api_url}")
                    flight_data = await self._adsb_api_call(session, api_url, headers, proxy, include_trace)

                if flight_data:
                    flight_data["source"] = "ADS-B Exchange"
                    icao = flight_data.get("icao_hex", query)
                    flight_data["source_url"] = f"https://globe.adsbexchange.com/?icao={icao}"
                    return flight_data

                return {"error": "No data found", "source": "ADS-B Exchange"}

        except Exception as e:
            logger.error(f"[ADS-B] Error: {e}")
            return {"error": str(e), "source": "ADS-B Exchange"}

    async def _adsb_api_call(
        self,
        session: aiohttp.ClientSession,
        api_url: str,
        headers: Dict,
        proxy: str = None,
        include_trace: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """执行单次 ADS-B API 调用并解析结果"""
        try:
            async with session.get(api_url, headers=headers, proxy=proxy, timeout=30) as response:
                if response.status == 200:
                    data = await response.json()
                    aircraft_list = data.get("ac", [])

                    if aircraft_list:
                        ac = aircraft_list[0]
                        flight_data = self._parse_adsb_aircraft(ac)

                        # 获取轨迹数据
                        if include_trace and flight_data.get("icao_hex"):
                            trace = await self._fetch_adsb_trace(
                                session, flight_data["icao_hex"], headers, proxy
                            )
                            if trace:
                                flight_data["trace"] = trace

                        return flight_data
        except Exception as e:
            logger.warning(f"[ADS-B] API call failed for {api_url}: {e}")

        return None

    def _parse_adsb_aircraft(self, ac: Dict) -> Dict[str, Any]:
        """解析 ADS-B 飞机数据"""
        # 解析位置
        lat = ac.get("lat")
        lon = ac.get("lon")
        position = {"lat": lat, "lon": lon} if lat and lon else None

        # 解析高度
        altitude = ac.get("alt_baro") or ac.get("alt_geom")
        if altitude and altitude != "ground":
            altitude = f"{altitude} ft"
        elif altitude == "ground":
            altitude = "地面"

        # 解析速度
        speed = ac.get("gs")
        if speed:
            speed = f"{speed} kts"

        # 解析航向
        heading = ac.get("track") or ac.get("true_heading")
        if heading:
            heading = f"{heading}°"

        # 解析垂直速率
        vertical_rate = ac.get("baro_rate") or ac.get("geom_rate")
        if vertical_rate:
            vertical_rate = f"{vertical_rate} ft/min"

        # 解析状态
        on_ground = ac.get("ground", False)
        if on_ground:
            status = "地面"
        elif altitude and "ground" not in str(altitude).lower():
            status = "飞行中"
        else:
            status = "未知"

        # 军机识别: dbFlags bit 0 = military
        db_flags = ac.get("dbFlags", 0) or 0
        is_military = bool(db_flags & 1)
        military_operator = ac.get("ownOp") or "" if is_military else ""

        # 尝试从 callsign 反推航班号（ICAO→IATA）
        callsign = (ac.get("flight") or "").strip()
        flight_number = self._callsign_to_flight_number(callsign) if callsign else None

        return {
            "icao_hex": ac.get("hex", "").upper(),
            "callsign": callsign,
            "flight_number": flight_number,
            "registration": ac.get("r"),
            "aircraft": ac.get("t"),
            "aircraft_desc": ac.get("desc"),
            "operator": ac.get("ownOp"),
            "country": ac.get("cou"),
            "altitude": altitude,
            "speed": speed,
            "heading": heading,
            "vertical_rate": vertical_rate,
            "squawk": ac.get("squawk"),
            "position": position,
            "status": status,
            "emergency": ac.get("emergency"),
            "category": ac.get("category"),
            "is_military": is_military,
            "military_operator": military_operator,
            "db_flags": db_flags,
        }

    async def _fetch_adsb_trace(
        self,
        session: aiohttp.ClientSession,
        icao_hex: str,
        headers: Dict,
        proxy: str = None,
    ) -> Optional[List[Dict]]:
        """获取 ADS-B 轨迹数据（globe.adsbexchange.com trace_full）"""
        try:
            # globe.adsbexchange.com trace 路径: /data/traces/{后两位小写}/{icao小写}.json
            icao_lower = icao_hex.lower()
            suffix = icao_lower[-2:]
            trace_url = f"https://globe.adsbexchange.com/data/traces/{suffix}/trace_full_{icao_lower}.json"

            trace_headers = {
                **headers,
                "Referer": f"https://globe.adsbexchange.com/?icao={icao_lower}",
            }

            async with session.get(trace_url, headers=trace_headers, proxy=proxy, timeout=30) as response:
                if response.status == 200:
                    data = await response.json()
                    trace_data = data.get("trace", [])

                    # 解析轨迹点: [相对时间, lat, lon, alt, speed, heading, ...]
                    trace_points = []
                    base_ts = data.get("timestamp", 0)
                    for point in trace_data:
                        if len(point) >= 4 and point[1] is not None and point[2] is not None:
                            trace_points.append({
                                "timestamp": int(base_ts + point[0]),
                                "lat": point[1],
                                "lon": point[2],
                                "alt": point[3] if point[3] != "ground" else 0,
                                "speed": point[4] if len(point) > 4 and point[4] is not None else None,
                                "heading": point[5] if len(point) > 5 and point[5] is not None else None,
                            })

                    # 保留最近 200 个点（轨迹需要足够密度画线）
                    trace_points = trace_points[-200:]
                    logger.info(f"[ADS-B] Trace fetched: {icao_hex}, {len(trace_points)} points")
                    return trace_points if trace_points else None
                else:
                    logger.warning(f"[ADS-B] Trace API returned {response.status} for {icao_hex}")

        except Exception as e:
            logger.warning(f"[ADS-B] Trace fetch failed for {icao_hex}: {e}")

        return None

    async def _fetch_flightradar24_data(
        self,
        flight_number: str,
        proxy_server: str = None,
    ) -> Optional[Dict[str, Any]]:
        """从 FlightRadar24 抓取航班数据（增强版：反检测 + Cookie 同意处理）"""
        try:
            async with async_playwright() as p:
                # 启动参数 - 增强反检测
                launch_args = {
                    "headless": True,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                }
                if proxy_server:
                    launch_args["proxy"] = {"server": proxy_server}

                browser = await p.chromium.launch(**launch_args)

                # 创建上下文 - 模拟真实用户
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                )

                page = await context.new_page()

                # 注入反检测脚本
                await page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [
                            {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
                            {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
                        ]
                    });
                """)

                url = f"https://www.flightradar24.com/data/flights/{flight_number.lower()}"
                logger.info(f"[FR24] Fetching: {url}")

                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)

                # 处理 Cookie 同意弹窗
                cookie_selectors = [
                    'button:has-text("Agree and close")',
                    'button:has-text("Accept")',
                    'button:has-text("Accept all")',
                    '[class*="agree"]',
                    '[class*="consent"] button',
                ]
                for selector in cookie_selectors:
                    try:
                        btn = await page.query_selector(selector)
                        if btn:
                            await btn.click()
                            logger.info(f"[FR24] Clicked cookie consent: {selector}")
                            await page.wait_for_timeout(2000)
                            break
                    except:
                        pass

                # 等待数据加载
                await page.wait_for_timeout(2000)

                # 提取航班数据
                flight_data = await page.evaluate("""
                    () => {
                        const data = {
                            flight_number: null,
                            aircraft: null,
                            origin: null,
                            origin_city: null,
                            destination: null,
                            destination_city: null,
                            status: null,
                            departure_time: null,
                            arrival_time: null,
                            flights: [],
                        };

                        // 获取航班号
                        const h1 = document.querySelector('h1');
                        if (h1) data.flight_number = h1.innerText.trim();

                        // 从表格中提取航班历史
                        const tables = document.querySelectorAll('table');
                        for (const table of tables) {
                            const rows = table.querySelectorAll('tbody tr');
                            for (const row of rows) {
                                const text = row.innerText || '';

                                // 提取机场代码（3字母代码如 PEK, XMN）
                                const airportMatches = text.match(/\\b([A-Z]{3})\\b/g);
                                if (airportMatches && airportMatches.length >= 2) {
                                    if (!data.origin) data.origin = airportMatches[0];
                                    if (!data.destination) data.destination = airportMatches[1];
                                }

                                // 提取城市名
                                const cityMatch = text.match(/FROM\\s+([^T]+)\\s+TO\\s+(.+)/i);
                                if (cityMatch) {
                                    data.origin_city = cityMatch[1].trim();
                                    data.destination_city = cityMatch[2].trim();
                                }

                                // 提取状态
                                if (text.includes('Scheduled')) data.status = 'Scheduled';
                                else if (text.includes('En route') || text.includes('Airborne')) data.status = 'En Route';
                                else if (text.includes('Landed')) data.status = 'Landed';
                                else if (text.includes('Delayed')) data.status = 'Delayed';

                                // 提取时间
                                const timeMatch = text.match(/STD\\s+(\\d{2}:\\d{2}).*?STA\\s+(\\d{2}:\\d{2})/);
                                if (timeMatch) {
                                    data.departure_time = timeMatch[1];
                                    data.arrival_time = timeMatch[2];
                                }

                                // 保存航班记录
                                if (data.origin && data.destination) {
                                    data.flights.push({
                                        origin: data.origin,
                                        destination: data.destination,
                                        status: data.status,
                                        departure_time: data.departure_time,
                                        arrival_time: data.arrival_time,
                                    });
                                    break;  // 只取第一条有效记录
                                }
                            }
                            if (data.origin) break;
                        }

                        return data;
                    }
                """)

                await browser.close()

                if flight_data and (flight_data.get("origin") or flight_data.get("destination")):
                    flight_data["source"] = "FlightRadar24"
                    flight_data["source_url"] = url
                    flight_data["flight_number"] = flight_number
                    logger.info(f"[FR24] Success: {flight_data.get('origin')} -> {flight_data.get('destination')}")
                    return flight_data

                return {"error": "No data found", "source": "FlightRadar24"}

        except Exception as e:
            logger.error(f"[FR24] Error: {e}")
            return {"error": str(e), "source": "FlightRadar24"}

    async def _fetch_flightaware_data(
        self,
        flight_number: str,
        proxy_server: str = None,
    ) -> Optional[Dict[str, Any]]:
        """从 FlightAware 抓取航班数据（增强版：反检测 + Cookie 处理）"""
        try:
            async with async_playwright() as p:
                # 启动参数 - 增强反检测
                launch_args = {
                    "headless": True,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                }
                if proxy_server:
                    launch_args["proxy"] = {"server": proxy_server}

                browser = await p.chromium.launch(**launch_args)

                # 创建上下文 - 模拟真实用户
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                )

                page = await context.new_page()

                # 注入反检测脚本
                await page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    window.chrome = { runtime: {} };
                """)

                url = f"https://flightaware.com/live/flight/{flight_number}"
                logger.info(f"[FlightAware] Fetching: {url}")

                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)

                # 处理 Cookie 同意弹窗
                cookie_selectors = [
                    'button:has-text("Accept")',
                    'button:has-text("I Accept")',
                    'button:has-text("Agree")',
                    '[class*="accept"]',
                    '[class*="consent"] button',
                ]
                for selector in cookie_selectors:
                    try:
                        btn = await page.query_selector(selector)
                        if btn:
                            await btn.click()
                            logger.info(f"[FlightAware] Clicked cookie consent")
                            await page.wait_for_timeout(2000)
                            break
                    except:
                        pass

                # 等待数据加载
                await page.wait_for_timeout(2000)

                flight_data = await page.evaluate("""
                    () => {
                        const data = {
                            flight_number: null,
                            aircraft: null,
                            origin: null,
                            origin_city: null,
                            destination: null,
                            destination_city: null,
                            departure_time: null,
                            arrival_time: null,
                            status: null,
                        };

                        // 获取航班号
                        const flightEl = document.querySelector('h1, .flightPageIdent, [class*="flightIdent"]');
                        if (flightEl) data.flight_number = flightEl.innerText.trim();

                        // 获取起飞机场
                        const originEl = document.querySelector('.flightPageSummaryOrigin, [class*="origin"], [class*="departure"]');
                        if (originEl) {
                            const codeEl = originEl.querySelector('.flightPageSummaryAirportCode, strong, [class*="code"]');
                            data.origin = codeEl?.innerText?.trim() || '';

                            // 提取城市名
                            const cityText = originEl.innerText || '';
                            const cityMatch = cityText.match(/([A-Z]{3})\\s*([^\\n]+)/);
                            if (cityMatch) {
                                data.origin = cityMatch[1];
                                data.origin_city = cityMatch[2].trim();
                            }
                        }

                        // 获取到达机场
                        const destEl = document.querySelector('.flightPageSummaryDestination, [class*="destination"], [class*="arrival"]');
                        if (destEl) {
                            const codeEl = destEl.querySelector('.flightPageSummaryAirportCode, strong, [class*="code"]');
                            data.destination = codeEl?.innerText?.trim() || '';

                            const cityText = destEl.innerText || '';
                            const cityMatch = cityText.match(/([A-Z]{3})\\s*([^\\n]+)/);
                            if (cityMatch) {
                                data.destination = cityMatch[1];
                                data.destination_city = cityMatch[2].trim();
                            }
                        }

                        // 获取状态
                        const statusEl = document.querySelector('.flightPageStatus, [class*="status"], [class*="State"]');
                        if (statusEl) data.status = statusEl.innerText.trim();

                        // 获取机型
                        const aircraftEl = document.querySelector('[class*="aircraft"], [class*="plane"]');
                        if (aircraftEl) data.aircraft = aircraftEl.innerText.trim();

                        return data;
                    }
                """)

                await browser.close()

                if flight_data and (flight_data.get("origin") or flight_data.get("destination")):
                    flight_data["source"] = "FlightAware"
                    flight_data["source_url"] = url
                    flight_data["flight_number"] = flight_number
                    logger.info(f"[FlightAware] Success: {flight_data.get('origin')} -> {flight_data.get('destination')}")
                    return flight_data

                return {"error": "No data found", "source": "FlightAware"}

        except Exception as e:
            logger.error(f"[FlightAware] Error: {e}")
            return {"error": str(e), "source": "FlightAware"}

    # ==================== 船舶数据获取方法 ====================

    async def _fetch_ship_data(
        self,
        targets: List[Dict[str, str]],
        proxy_server: str = None,
    ) -> List[Dict[str, Any]]:
        """获取船舶数据"""
        results = []

        async def fetch_single(target):
            target_type = target["type"]
            target_value = target["value"]
            ship_data = await self._fetch_chinaports_ship_data(target_value, target_type, proxy_server)
            return {"target": target, "data": ship_data}

        tasks = [fetch_single(t) for t in targets]
        fetch_results = await asyncio.gather(*tasks, return_exceptions=True)

        for fetch_result in fetch_results:
            if isinstance(fetch_result, Exception):
                logger.error(f"Ship fetch failed: {fetch_result}")
                continue

            target = fetch_result["target"]
            ship_data = fetch_result["data"]
            target_value = target["value"]

            if ship_data and not ship_data.get("error"):
                display_name = ship_data.get("ship_name") or ship_data.get("mmsi") or target_value
                summary = self._format_ship_summary(ship_data)

                results.append({
                    "title": f"船舶 {display_name} 实时追踪数据",
                    "link": ship_data.get("source_url", ""),
                    "snippet": summary,
                    "source": ship_data.get("source", "ship_tracker"),
                    "is_tracking_site": True,
                    "ship_data": ship_data,
                    "query_target": target_value,
                    "tracking_type": "ship",
                })
                logger.info(f"✅ Got ship data for {target_value}")

        return results

    async def _fetch_chinaports_ship_data(
        self,
        identifier: str,
        id_type: str,
        proxy_server: str = None,
    ) -> Optional[Dict[str, Any]]:
        """从中国港口网获取船舶数据"""
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox"],
                )
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    viewport={"width": 1920, "height": 1080},
                )
                page = await context.new_page()

                # 根据标识符类型构建 URL
                if id_type == "mmsi":
                    url = f"https://ship.chinaports.com/ShipTrack/shipDetail?mmsi={identifier}"
                elif id_type == "imo":
                    url = f"https://ship.chinaports.com/ShipTrack/shipDetail?imo={identifier}"
                else:
                    url = f"https://ship.chinaports.com/ShipTrack/search?keyword={identifier}"

                logger.info(f"[ChinaPorts] Fetching: {url}")

                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)

                ship_data = await page.evaluate("""
                    () => {
                        const data = {
                            ship_name: null,
                            mmsi: null,
                            imo: null,
                            ship_type: null,
                            flag: null,
                            destination: null,
                            eta: null,
                            speed: null,
                            heading: null,
                            status: null,
                            position: null,
                        };

                        // 尝试提取船舶信息
                        const infoItems = document.querySelectorAll('.ship-info-item, [class*="info-row"], tr');
                        infoItems.forEach(item => {
                            const text = item.innerText.toLowerCase();
                            const value = item.querySelector('td:last-child, span:last-child, .value');

                            if (text.includes('船名') || text.includes('name')) {
                                data.ship_name = value?.innerText?.trim();
                            }
                            if (text.includes('mmsi')) {
                                data.mmsi = value?.innerText?.trim();
                            }
                            if (text.includes('imo')) {
                                data.imo = value?.innerText?.trim();
                            }
                            if (text.includes('船型') || text.includes('type')) {
                                data.ship_type = value?.innerText?.trim();
                            }
                            if (text.includes('船旗') || text.includes('flag')) {
                                data.flag = value?.innerText?.trim();
                            }
                            if (text.includes('目的') || text.includes('destination')) {
                                data.destination = value?.innerText?.trim();
                            }
                            if (text.includes('eta') || text.includes('预计')) {
                                data.eta = value?.innerText?.trim();
                            }
                            if (text.includes('航速') || text.includes('speed')) {
                                data.speed = value?.innerText?.trim();
                            }
                        });

                        return data;
                    }
                """)

                await browser.close()

                if ship_data and (ship_data.get("ship_name") or ship_data.get("mmsi")):
                    ship_data["source"] = "ChinaPorts"
                    ship_data["source_url"] = url
                    return ship_data

                return {"error": "No data found", "source": "ChinaPorts"}

        except Exception as e:
            logger.error(f"[ChinaPorts] Error: {e}")
            return {"error": str(e), "source": "ChinaPorts"}

    # ==================== 标识符提取方法 ====================

    def _extract_all_flight_numbers(self, query: str) -> List[str]:
        """提取所有航班号"""
        patterns = [
            r'\b([A-Z]{2,3})[\s-]?(\d{1,4})\b',
        ]
        query_upper = query.upper()
        flight_numbers = []
        seen = set()

        for pattern in patterns:
            for match in re.finditer(pattern, query_upper):
                airline = match.group(1)
                number = match.group(2)
                fn = f"{airline}{number}"
                if fn not in seen:
                    seen.add(fn)
                    flight_numbers.append(fn)

        return flight_numbers

    def _extract_all_icao_hex(self, query: str) -> List[str]:
        """提取所有 ICAO 24位地址码"""
        query_lower = query.lower()
        icao_codes = []
        seen = set()

        # 模式1: icao=xxx
        for match in re.finditer(r'icao[=:\s]?([0-9a-fA-F]{6})\b', query_lower):
            code = match.group(1).lower()
            if code not in seen:
                seen.add(code)
                icao_codes.append(code)

        # 模式2: 直接的6位十六进制
        parts = re.split(r'[,;\s]+', query_lower)
        for part in parts:
            part = part.strip()
            if re.match(r'^[0-9a-f]{6}$', part) and part not in seen:
                # 排除航班号格式
                if not re.match(r'^[a-z]{2,3}\d{1,4}$', part):
                    seen.add(part)
                    icao_codes.append(part)

        return icao_codes

    def _extract_all_callsigns(self, query: str) -> List[str]:
        """提取所有呼号"""
        query_upper = query.upper()
        callsigns = []
        seen = set()

        patterns = [
            r'\b([A-Z]{3}\d{1,4})\b',  # CCA1831
            r'\b([A-Z]-[A-Z0-9]{4,5})\b',  # B-1234
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, query_upper):
                cs = match.group(1)
                if cs not in seen:
                    seen.add(cs)
                    callsigns.append(cs)

        return callsigns

    def _extract_all_mmsi(self, query: str) -> List[str]:
        """提取所有 MMSI"""
        mmsi_codes = []
        seen = set()

        for match in re.finditer(r'mmsi[=:\s]?(\d{9})\b', query.lower()):
            code = match.group(1)
            if code not in seen:
                seen.add(code)
                mmsi_codes.append(code)

        # 检查船舶上下文
        if re.search(r'船|ship|vessel|mmsi|imo|港', query.lower()):
            for part in re.split(r'[,;\s]+', query):
                if re.match(r'^\d{9}$', part.strip()) and part not in seen:
                    seen.add(part)
                    mmsi_codes.append(part)

        return mmsi_codes

    def _extract_all_imo(self, query: str) -> List[str]:
        """提取所有 IMO"""
        imo_codes = []
        seen = set()

        for match in re.finditer(r'IMO[=:\s-]?(\d{7})\b', query.upper()):
            code = match.group(1)
            if code not in seen:
                seen.add(code)
                imo_codes.append(code)

        return imo_codes

    # ==================== 格式化方法 ====================

    def _format_flight_summary(self, flight_data: Dict[str, Any]) -> str:
        """格式化航班摘要"""
        parts = []

        if flight_data.get("is_military"):
            mil_op = flight_data.get("military_operator", "")
            parts.append(f"[军机] {mil_op}" if mil_op else "[军机]")
        if flight_data.get("flight_number"):
            parts.append(f"航班号: {flight_data['flight_number']}")
        if flight_data.get("callsign"):
            parts.append(f"呼号: {flight_data['callsign']}")
        if flight_data.get("icao_hex"):
            parts.append(f"ICAO: {flight_data['icao_hex']}")
        if flight_data.get("registration"):
            parts.append(f"注册号: {flight_data['registration']}")
        if flight_data.get("aircraft"):
            parts.append(f"机型: {flight_data['aircraft']}")
        if flight_data.get("aircraft_desc"):
            parts.append(f"({flight_data['aircraft_desc']})")
        if flight_data.get("origin") and flight_data.get("destination"):
            parts.append(f"航线: {flight_data['origin']} → {flight_data['destination']}")
        if flight_data.get("status"):
            parts.append(f"状态: {flight_data['status']}")
        if flight_data.get("altitude"):
            parts.append(f"高度: {flight_data['altitude']}")
        if flight_data.get("speed"):
            parts.append(f"速度: {flight_data['speed']}")

        return " | ".join(parts) if parts else "暂无数据"

    def _format_ship_summary(self, ship_data: Dict[str, Any]) -> str:
        """格式化船舶摘要"""
        parts = []

        if ship_data.get("ship_name"):
            parts.append(f"船名: {ship_data['ship_name']}")
        if ship_data.get("mmsi"):
            parts.append(f"MMSI: {ship_data['mmsi']}")
        if ship_data.get("imo"):
            parts.append(f"IMO: {ship_data['imo']}")
        if ship_data.get("ship_type"):
            parts.append(f"类型: {ship_data['ship_type']}")
        if ship_data.get("destination"):
            parts.append(f"目的港: {ship_data['destination']}")
        if ship_data.get("status"):
            parts.append(f"状态: {ship_data['status']}")

        return " | ".join(parts) if parts else "暂无数据"

    # ==================== UI 组件构建 ====================

    def _build_ui_components(
        self,
        results: List[Dict[str, Any]],
        tracking_type: str,
        query: str,
    ) -> List[Dict[str, Any]]:
        """构建 UI 组件"""
        components = []

        # 分离有数据的结果和纯链接
        data_results = [r for r in results if not r.get("is_link_only")]
        link_results = [r for r in results if r.get("is_link_only")]

        if tracking_type == "flight":
            # map 场景原则：
            # 有经纬度 → map_bindary_view 的 markers (poi/layer) + trace 轨迹线
            # 无经纬度但有 trace → 用 trace 最后一个点作为位置 + 轨迹线
            # 无经纬度无 trace → flight_tracker_card，由 hook_manager scene_context 自动降级为 overlay
            if data_results:
                markers = []
                tracks = []  # 轨迹线数据（字段名与 map_bindary / 前端 MapBindaryView 一致）
                for result in data_results:
                    flight_data = result.get("flight_data", {})
                    position = flight_data.get("position")
                    trace = flight_data.get("trace", [])

                    # 如果没有实时位置但有 trace，用 trace 最后一个有效点补位置
                    if (not position or not position.get("lat") or not position.get("lon")) and trace:
                        last_point = trace[-1]
                        if last_point.get("lat") and last_point.get("lon"):
                            position = {"lat": last_point["lat"], "lon": last_point["lon"]}
                            flight_data["position"] = position
                            logger.info(f"[UI] Position fallback from trace: {position}")

                    if position and position.get("lat") and position.get("lon"):
                        heading_raw = flight_data.get("heading", "0")
                        heading_val = float(str(heading_raw).replace("°", "")) if heading_raw else None
                        flight_id = flight_data.get("icao_hex") or flight_data.get("callsign")
                        marker = {
                            "id": flight_id,
                            "name": flight_data.get("flight_number") or flight_data.get("callsign") or flight_id,
                            "coordinates": [position["lon"], position["lat"]],  # [lng, lat] 前端标准格式
                            "lat": position["lat"],
                            "lon": position["lon"],
                            "type": "aircraft",
                            "heading": heading_val,
                            "icao_hex": flight_data.get("icao_hex", ""),
                            "flight_number": flight_data.get("flight_number", ""),
                            "callsign": flight_data.get("callsign", ""),
                            "registration": flight_data.get("registration", ""),
                            "aircraft": flight_data.get("aircraft", ""),
                            "aircraft_desc": flight_data.get("aircraft_desc", ""),
                            "operator": flight_data.get("operator", ""),
                            "altitude": flight_data.get("altitude", ""),
                            "speed": flight_data.get("speed", ""),
                            "vertical_rate": flight_data.get("vertical_rate", ""),
                            "squawk": flight_data.get("squawk", ""),
                            "status": flight_data.get("status", ""),
                            "distance_km": flight_data.get("distance_km"),
                            "is_military": flight_data.get("is_military", False),
                            "military_operator": flight_data.get("military_operator", ""),
                        }
                        markers.append(marker)

                        # 构建轨迹线（trace 有数据时）
                        if trace and len(trace) >= 2:
                            trajectory_coords = [
                                [p["lon"], p["lat"]] for p in trace
                                if p.get("lat") and p.get("lon")
                            ]
                            if len(trajectory_coords) >= 2:
                                is_mil = flight_data.get("is_military", False)
                                tracks.append({
                                    "id": f"trace_{flight_id}",
                                    "name": f"{flight_data.get('callsign') or flight_id} 轨迹",
                                    "type": "trajectory",
                                    "coordinates": trajectory_coords,
                                    "style": {
                                        "color": "#ff4d4f" if is_mil else "#1890ff",
                                        "weight": 3 if is_mil else 2,
                                        "opacity": 0.8 if is_mil else 0.7,
                                        "dashArray": "" if is_mil else "5,5",
                                    },
                                })
                    elif flight_data:
                        # 无经纬度无 trace → flight_tracker_card，scene_context 会降级为 overlay
                        components.append({
                            "component": "flight_tracker_card",
                            "priority": len(components) + 10,
                            "data": {
                                "flight_info": flight_data,
                            },
                        })

                if markers:
                    map_data = {
                        "title": f"{len(markers)} 个航班实时位置",
                        "markers": markers,
                    }
                    # 附加轨迹线
                    if tracks:
                        map_data["tracks"] = tracks
                    components.append({
                        "component": "map_bindary_view",
                        "priority": 1,
                        "data": map_data,
                    })

        elif tracking_type == "ship":
            # 船舶地图组件 — 每艘船作为 marker，点击 popup 显示详情
            if data_results:
                markers = []
                for result in data_results:
                    ship_data = result.get("ship_data", {})
                    position = ship_data.get("position")
                    if position and position.get("lat") and (position.get("lon") or position.get("lng")):
                        lon = position.get("lon") or position.get("lng")
                        markers.append({
                            "id": ship_data.get("mmsi") or ship_data.get("ship_name"),
                            "name": ship_data.get("ship_name", ""),
                            "coordinates": [lon, position["lat"]],  # [lng, lat] 前端标准格式
                            "lat": position["lat"],
                            "lon": lon,
                            "type": "ship",
                            "mmsi": ship_data.get("mmsi", ""),
                            "imo": ship_data.get("imo", ""),
                            "ship_name": ship_data.get("ship_name", ""),
                            "ship_type": ship_data.get("ship_type", ""),
                            "flag": ship_data.get("flag", ""),
                            "speed": ship_data.get("speed", ""),
                            "heading": ship_data.get("heading"),
                            "destination": ship_data.get("destination", ""),
                            "eta": ship_data.get("eta", ""),
                            "status": ship_data.get("status", ""),
                        })

                if markers:
                    components.append({
                        "component": "map_bindary_view",
                        "priority": len(data_results) + 1,
                        "data": {
                            "title": f"{len(markers)} 艘船舶实时位置",
                            "markers": markers,
                        },
                    })

        return components


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main(params: Dict[str, Any]) -> Dict[str, Any]:
    """同步入口，供框架调用"""
    skill = FlightTrackerSkill()
    try:
        return asyncio.run(skill.execute(params))
    except Exception as e:
        return {"error": str(e), "for_llm": {"error": str(e)}}


if __name__ == "__main__":
    import sys
    import json as _json
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--query", default="")
        parser.add_argument("--tracking_type", default="auto")
        parser.add_argument("--search_mode", default="auto")
        parser.add_argument("--radius_km", type=int, default=200)
        parser.add_argument("--max_results", type=int, default=50)
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())
    result = main(params)
    print(_json.dumps(result, ensure_ascii=False))
