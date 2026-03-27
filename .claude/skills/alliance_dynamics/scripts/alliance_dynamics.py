"""
国际联盟关系动态分析技能
基于 GDELT 外交事件数据和世界银行治理指标分析国际联盟和双边关系。
无跨层 import，所有配置通过环境变量读取。
"""
import asyncio
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

DISCLAIMER = "学术研究分析，非实时情报，结论仅供参考"

COUNTRY_EN_MAP = {
    "中国": "China", "美国": "United States", "俄罗斯": "Russia",
    "英国": "United Kingdom", "德国": "Germany", "法国": "France",
    "日本": "Japan", "韩国": "South Korea", "印度": "India",
    "巴西": "Brazil", "澳大利亚": "Australia", "加拿大": "Canada",
    "以色列": "Israel", "伊朗": "Iran", "沙特": "Saudi Arabia",
    "土耳其": "Turkey", "南非": "South Africa", "墨西哥": "Mexico",
    "印尼": "Indonesia", "阿联酋": "UAE",
}

ALLIANCE_EVENT_KEYWORDS = {
    "峰会与会谈": ["summit", "meeting", "talks", "dialogue", "forum", "conference"],
    "协议与条约": ["agreement", "treaty", "pact", "accord", "deal", "memorandum"],
    "制裁与对抗": ["sanction", "embargo", "ban", "restriction", "expel", "retaliation", "condemn"],
    "合作与援助": ["cooperation", "partnership", "alliance", "aid", "assistance", "joint", "collaborative"],
    "军事同盟": ["NATO", "AUKUS", "QUAD", "military alliance", "defense pact", "joint exercise"],
}


def _resolve_country_en(country: str) -> str:
    return COUNTRY_EN_MAP.get(country, country)


def safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


async def _gdelt_search(query: str, mode: str = "artlist", timespan: str = "14d", max_records: int = 50) -> Optional[Dict]:
    if not aiohttp:
        return None
    base_url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {"query": query, "mode": mode, "maxrecords": max_records, "timespan": timespan, "format": "json"}
    url = f"{base_url}?{urlencode(params)}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
    except Exception as e:
        logger.warning(f"GDELT search failed: {e}")
    return None


async def _worldbank_fetch(countries: str, indicator: str, date_range: str = "2018:2024") -> List[Dict]:
    if not aiohttp:
        return []
    url = f"https://api.worldbank.org/v2/country/{countries}/indicator/{indicator}"
    params = {"format": "json", "per_page": 20, "date": date_range}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if isinstance(data, list) and len(data) > 1:
                        return data[1] if isinstance(data[1], list) else []
    except Exception as e:
        logger.warning(f"World Bank fetch failed for {indicator}: {e}")
    return []


async def _nato_news_fetch(query: str, max_items: int = 10) -> List[Dict]:
    if not aiohttp:
        return []
    url = "https://www.nato.int/cps/en/natolive/news.htm"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    # Simple extraction - return empty list if parsing fails
                    return []
    except Exception:
        pass
    return []


def _extract_articles(result: Optional[Dict]) -> List[Dict]:
    if not result:
        return []
    articles = result.get("articles", [])
    return [{"title": a.get("title", ""), "url": a.get("url", ""),
             "source": a.get("domain", ""), "date": a.get("seendate", ""),
             "tone": safe_float(a.get("tone", 0))} for a in articles]


def _detect_alliance_events(news: List[Dict]) -> List[Dict]:
    events = []
    seen_titles = set()
    for article in news:
        title = article.get("title", "")
        if not title or title in seen_titles:
            continue
        title_lower = title.lower()
        matched_category = None
        for category, keywords in ALLIANCE_EVENT_KEYWORDS.items():
            for keyword in keywords:
                if keyword.lower() in title_lower:
                    matched_category = category
                    break
            if matched_category:
                break
        if matched_category:
            seen_titles.add(title)
            events.append({"title": title, "category": matched_category,
                           "url": article.get("url", ""), "source": article.get("source", ""),
                           "date": article.get("date", ""), "tone": article.get("tone", 0)})
    events.sort(key=lambda x: x.get("date", ""), reverse=True)
    return events


def _calculate_relationship_score(news: List[Dict], bilateral_tone: Dict) -> int:
    score = 50.0
    if news:
        tones = [safe_float(n.get("tone", 0)) for n in news]
        avg_news_tone = sum(tones) / len(tones)
        score += max(-25.0, min(25.0, avg_news_tone * 2.5))
    avg_bilateral = safe_float(bilateral_tone.get("avg_tone", 0))
    if avg_bilateral != 0.0:
        score += max(-25.0, min(25.0, avg_bilateral * 2.5))
    return min(100, max(0, round(score)))


async def _run_analysis(params: Dict[str, Any]) -> Dict[str, Any]:
    country = (params.get("country") or params.get("query") or "").strip()
    if not country:
        return {"error": "缺少必填参数 country", "for_llm": "Error: missing country parameter"}

    partner = (params.get("partner") or "").strip()
    topic = (params.get("topic") or "").strip()
    days = int(params.get("days") or 14)
    timespan = f"{days}d"

    country_en = _resolve_country_en(country)
    partner_en = _resolve_country_en(partner) if partner else ""

    # Build GDELT query
    query_parts = [country_en, "(diplomacy OR alliance OR cooperation OR summit)"]
    if partner_en:
        query_parts.insert(1, partner_en)
    if topic and not any('\u4e00' <= c <= '\u9fff' for c in topic):
        query_parts.append(topic)
    query_str = " ".join(query_parts)

    # Build World Bank countries string
    iso2_map = {
        "中国": "CN", "美国": "US", "俄罗斯": "RU", "英国": "GB", "德国": "DE",
        "法国": "FR", "日本": "JP", "韩国": "KR", "印度": "IN",
    }
    wb_codes = [iso2_map.get(country, country[:2].upper())]
    if partner:
        wb_codes.append(iso2_map.get(partner, partner[:2].upper()))
    wb_countries = ";".join(wb_codes)

    # Parallel data fetching
    tasks = [
        _gdelt_search(query_str, mode="artlist", timespan=timespan, max_records=50),
        _worldbank_fetch(wb_countries, "CC.EST", date_range="2018:2024"),
        _worldbank_fetch(wb_countries, "GE.EST", date_range="2018:2024"),
        _worldbank_fetch(wb_countries, "RL.EST", date_range="2018:2024"),
    ]

    if partner_en:
        bilateral_query = f"{country_en} {partner_en} (bilateral OR relations OR diplomacy)"
        tasks.append(_gdelt_search(bilateral_query, mode="tonechart", timespan=timespan, max_records=50))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    news_result = results[0] if not isinstance(results[0], Exception) else None
    diplomacy_news = _extract_articles(news_result)

    # Governance indicators
    governance = {}
    indicator_names = {"CC.EST": "腐败控制", "GE.EST": "政府效能", "RL.EST": "法治水平"}
    for i, (ind, name) in enumerate(indicator_names.items()):
        res = results[i + 1]
        if isinstance(res, Exception) or not isinstance(res, list):
            continue
        for record in res:
            c_name = record.get("country", {}).get("value", "")
            year = record.get("date", "")
            value = safe_float(record.get("value"))
            if c_name and year and value != 0.0:
                if c_name not in governance:
                    governance[c_name] = {}
                if ind not in governance[c_name]:
                    governance[c_name][ind] = {"name": name, "records": []}
                governance[c_name][ind]["records"].append({"year": year, "value": round(value, 3)})

    # Bilateral tone
    bilateral_tone = {}
    if partner_en and len(results) > 4:
        bt_result = results[4]
        if not isinstance(bt_result, Exception) and bt_result:
            tone_data = bt_result.get("tonechart", [])
            if tone_data:
                timeline = [{"date": item.get("date", ""), "tone": safe_float(item.get("tone", 0)),
                             "count": int(safe_float(item.get("count", 0)))} for item in tone_data]
                total_count = sum(max(t["count"], 1) for t in timeline)
                total_tone = sum(t["tone"] * max(t["count"], 1) for t in timeline)
                bilateral_tone = {"avg_tone": round(total_tone / total_count, 3) if total_count > 0 else 0.0,
                                  "tone_timeline": timeline, "article_count": total_count}

    alliance_events = _detect_alliance_events(diplomacy_news)
    relationship_score = _calculate_relationship_score(diplomacy_news, bilateral_tone)
    partner_label = f" 与 {partner}" if partner else ""

    for_llm = (
        f"{country}{partner_label} 联盟关系动态分析完成：关系评分 {relationship_score}/100，"
        f"近 {days} 天发现 {len(diplomacy_news)} 条外交新闻，"
        f"{len(alliance_events)} 个关键外交事件。"
    )

    return {
        "country": country,
        "partner": partner or None,
        "topic": topic or None,
        "days": days,
        "diplomacy_news": diplomacy_news,
        "bilateral_tone": bilateral_tone,
        "alliance_events": alliance_events,
        "governance_indicators": governance,
        "relationship_score": relationship_score,
        "for_llm": for_llm,
        "disclaimer": DISCLAIMER,
    }


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    return asyncio.run(_run_analysis(params))


if __name__ == "__main__":
    import sys
    import json as _json

    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--country", default="")
        parser.add_argument("--partner", default="")
        parser.add_argument("--topic", default="")
        parser.add_argument("--days", type=int, default=14)
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())

    result = main(params)
    print(_json.dumps(result, ensure_ascii=False))
