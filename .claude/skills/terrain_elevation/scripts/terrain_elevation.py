"""
terrain_elevation — 地形海拔查询技能

查询指定位置的海拔高度，支持单点和剖面查询。
数据源: Open-Elevation API (免费)
"""
import os
import time
from typing import Any, Dict, List, Optional

import aiohttp
from loguru import logger


# ---------------------------------------------------------------------------
# 地理工具函数
# ---------------------------------------------------------------------------

async def resolve_location(query: str) -> Optional[Dict[str, Any]]:
    """使用 Nominatim 将地名解析为经纬度"""
    query = query.strip()
    # 检查是否已是坐标
    import re
    coord_match = re.match(r'^(-?\d+\.?\d*)\s*[,，]\s*(-?\d+\.?\d*)$', query)
    if coord_match:
        lat, lng = float(coord_match.group(1)), float(coord_match.group(2))
        return {"name": query, "lat": lat, "lng": lng}

    nominatim_url = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{nominatim_url}/search",
                params={"q": query, "format": "json", "limit": 1},
                headers={"User-Agent": "terrain_elevation_skill/1.0"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if data:
                    return {
                        "name": data[0].get("display_name", query).split(",")[0],
                        "lat": float(data[0]["lat"]),
                        "lng": float(data[0]["lon"]),
                    }
    except Exception as e:
        logger.warning(f"[terrain_elevation] resolve_location failed: {e}")
    return None


def interpolate_points(lat1: float, lng1: float, lat2: float, lng2: float, n: int) -> List[Dict]:
    """在两点之间插值 n 个采样点"""
    points = []
    for i in range(n):
        t = i / max(n - 1, 1)
        points.append({
            "latitude": lat1 + t * (lat2 - lat1),
            "longitude": lng1 + t * (lng2 - lng1),
        })
    return points


async def open_elevation_lookup(points: List[Dict]) -> Optional[List[Dict]]:
    """调用 Open-Elevation API 批量查询海拔"""
    api_url = os.environ.get("OPEN_ELEVATION_URL", "https://api.open-elevation.com/api/v1/lookup")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                api_url,
                json={"locations": points},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                return data.get("results")
    except Exception as e:
        logger.warning(f"[terrain_elevation] open_elevation_lookup failed: {e}")
    return None


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------

async def _single_point_query(location: Dict, start_ts: float) -> Dict[str, Any]:
    """单点海拔查询"""
    lat, lng = location["lat"], location["lng"]
    results = await open_elevation_lookup([{"latitude": lat, "longitude": lng}])

    if not results:
        return {
            "error": "海拔查询失败",
            "for_llm": {"error": "海拔服务暂时不可用，请稍后重试"},
        }

    elevation = results[0].get("elevation", 0)

    map_data = {
        "center": [lng, lat],
        "zoom": 10,
        "markers": [{
            "coordinates": [lng, lat],
            "title": f"{location['name']} ({elevation}m)",
            "description": f"海拔: {elevation} 米",
            "type": "terrain",
            "color": "#8B4513",
        }],
        "legend": [{"color": "#8B4513", "name": "查询点"}],
    }

    chart_data = {
        "title": f"{location['name']} 海拔",
        "x_axis": [location["name"]],
        "series": [
            {"name": "海拔(m)", "data": [elevation], "color": "#8B4513"},
        ],
    }

    data = {
        "location": location,
        "elevation": elevation,
        "chart_data": chart_data,
        "chart": chart_data,
        "map_data": map_data,
        "source": "Open-Elevation",
        "for_llm": {
            "location": location["name"],
            "elevation_meters": elevation,
            "message": f"{location['name']} 海拔 {elevation} 米",
        },
    }
    return data


async def _profile_query(
    start_loc: Dict, end_loc: Dict, n_points: int, start_ts: float
) -> Dict[str, Any]:
    """两点间海拔剖面查询"""
    lat1, lng1 = start_loc["lat"], start_loc["lng"]
    lat2, lng2 = end_loc["lat"], end_loc["lng"]

    points = interpolate_points(lat1, lng1, lat2, lng2, n_points)
    results = await open_elevation_lookup(points)
    if not results:
        return {
            "error": "海拔剖面查询失败",
            "for_llm": {"error": "海拔服务暂时不可用"},
        }

    elevation_profile = []
    for i, r in enumerate(results):
        elevation_profile.append({
            "index": i,
            "lat": r.get("latitude"),
            "lng": r.get("longitude"),
            "elevation": r.get("elevation", 0),
            "distance_pct": round(i / max(n_points - 1, 1) * 100, 1),
        })

    elevations = [p["elevation"] for p in elevation_profile]
    max_elev = max(elevations) if elevations else 0
    min_elev = min(elevations) if elevations else 0
    avg_elev = round(sum(elevations) / len(elevations), 1) if elevations else 0

    chart_data = {
        "title": f"{start_loc['name']} → {end_loc['name']} 海拔剖面",
        "x_axis": [f"{p['distance_pct']}%" for p in elevation_profile],
        "series": [
            {"name": "海拔(m)", "data": elevations, "color": "#8B4513"},
        ],
    }

    map_data = {
        "center": [(lng1 + lng2) / 2, (lat1 + lat2) / 2],
        "zoom": 7,
        "markers": [
            {
                "coordinates": [lng1, lat1],
                "title": f"{start_loc['name']} ({elevations[0] if elevations else '?'}m)",
                "type": "terrain",
                "color": "#4CAF50",
            },
            {
                "coordinates": [lng2, lat2],
                "title": f"{end_loc['name']} ({elevations[-1] if elevations else '?'}m)",
                "type": "terrain",
                "color": "#F44336",
            },
        ],
        "tracks": [{
            "coordinates": [[p["lng"], p["lat"]] for p in elevation_profile],
            "color": "#8B4513",
            "width": 3,
        }],
        "legend": [
            {"color": "#4CAF50", "name": "起点"},
            {"color": "#F44336", "name": "终点"},
            {"color": "#8B4513", "name": "剖面线"},
        ],
    }

    data = {
        "location": start_loc,
        "end_location": end_loc,
        "elevation": elevations[0] if elevations else None,
        "elevation_profile": elevation_profile,
        "summary": {
            "max_elevation": max_elev,
            "min_elevation": min_elev,
            "avg_elevation": avg_elev,
            "total_points": len(elevation_profile),
        },
        "chart_data": chart_data,
        "chart": chart_data,
        "map_data": map_data,
        "source": "Open-Elevation",
        "for_llm": {
            "start": start_loc["name"],
            "end": end_loc["name"],
            "max_elevation_meters": max_elev,
            "min_elevation_meters": min_elev,
            "avg_elevation_meters": avg_elev,
            "message": f"{start_loc['name']}→{end_loc['name']} 剖面: 最高{max_elev}m, 最低{min_elev}m",
        },
    }
    return data


async def _execute(params: Dict[str, Any]) -> Dict[str, Any]:
    """异步执行海拔查询"""
    start_ts = time.time()
    query = (params.get("query") or "").strip()
    end_query = (params.get("end_point") or "").strip()
    n_points = min(max(int(params.get("profile_points", 50)), 5), 100)

    if not query:
        return {
            "error": "缺少必需参数 query",
            "for_llm": {"error": "请提供要查询海拔的地点"},
        }

    location = await resolve_location(query)
    if not location:
        return {
            "error": f"无法解析位置: {query}",
            "for_llm": {"error": f"未能识别地点 \"{query}\""},
        }

    if end_query:
        end_location = await resolve_location(end_query)
        if not end_location:
            return {
                "error": f"无法解析终点位置: {end_query}",
                "for_llm": {"error": f"未能识别终点 \"{end_query}\""},
            }
        return await _profile_query(location, end_location, n_points, start_ts)
    else:
        return await _single_point_query(location, start_ts)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main(params: Dict[str, Any]) -> Dict[str, Any]:
    """同步入口，供框架调用"""
    import asyncio
    try:
        return asyncio.run(_execute(params))
    except Exception as e:
        return {"error": str(e), "for_llm": {"error": str(e)}}


if __name__ == "__main__":
    import sys
    import json as _json
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--query", default="")
        parser.add_argument("--end_point", default="")
        parser.add_argument("--profile_points", type=int, default=50)
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())
    result = main(params)
    print(_json.dumps(result, ensure_ascii=False))
