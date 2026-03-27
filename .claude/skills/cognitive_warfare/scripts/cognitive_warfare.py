"""
cognitive_warfare — 认知域舆论博弈分析技能
基于 GDELT 情感量化引擎，追踪国际媒体报道情感值变化趋势。
无跨层 import，所有配置通过环境变量读取。
"""
import asyncio
import math
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


def safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


async def _gdelt_search(query: str, mode: str, timespan: str, max_records: int = 250) -> Optional[Dict]:
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


async def _youtube_search(query: str, max_results: int = 10) -> List[Dict]:
    youtube_api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not youtube_api_key or not aiohttp:
        return []
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {"part": "snippet", "q": query, "maxResults": max_results,
              "order": "relevance", "type": "video", "key": youtube_api_key}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    items = data.get("items", [])
                    return [{"title": item.get("snippet", {}).get("title", ""),
                             "description": item.get("snippet", {}).get("description", ""),
                             "channel": item.get("snippet", {}).get("channelTitle", ""),
                             "published_at": item.get("snippet", {}).get("publishedAt", ""),
                             "video_id": item.get("id", {}).get("videoId", "")} for item in items]
    except Exception as e:
        logger.warning(f"YouTube search failed: {e}")
    return []


def _fetch_tone_timeline(raw: Optional[Dict]) -> List[Dict]:
    if not raw:
        return []
    timeline = []
    for entry in raw.get("tonechart", []):
        date_val = entry.get("date", entry.get("bin", ""))
        tone_mean = safe_float(entry.get("tonemean", entry.get("y", 0)))
        tone_count = int(safe_float(entry.get("count", entry.get("x", 0))))
        timeline.append({"date": str(date_val), "tone_avg": round(tone_mean, 3), "article_count": tone_count})
    return timeline


def _fetch_narrative_articles(raw: Optional[Dict]) -> List[Dict]:
    if not raw:
        return []
    articles = []
    for art in raw.get("articles", []):
        tone = safe_float(art.get("tone", 0))
        articles.append({
            "title": art.get("title", ""), "url": art.get("url", ""),
            "source": art.get("domain", art.get("source", "")),
            "tone": round(tone, 3), "date": art.get("seendate", art.get("date", "")),
            "language": art.get("language", ""), "source_country": art.get("sourcecountry", ""),
        })
    articles.sort(key=lambda a: abs(a.get("tone", 0)), reverse=True)
    return articles


def _detect_narrative_shift(timeline: List[Dict]) -> List[Dict]:
    if len(timeline) < 3:
        return []
    tones = [e["tone_avg"] for e in timeline]
    n = len(tones)
    mean_tone = sum(tones) / n
    variance = sum((t - mean_tone) ** 2 for t in tones) / n
    std_tone = math.sqrt(variance) if variance > 0 else 0.0
    if std_tone == 0:
        return []
    shift_points = []
    for entry in timeline:
        deviation = (entry["tone_avg"] - mean_tone) / std_tone
        if abs(deviation) >= 2.0:
            shift_points.append({"date": entry["date"], "tone_avg": entry["tone_avg"],
                                  "deviation": round(deviation, 3),
                                  "direction": "positive" if deviation > 0 else "negative",
                                  "article_count": entry.get("article_count", 0)})
    return shift_points


def _calculate_cognitive_index(timeline: List[Dict], articles: List[Dict]) -> int:
    if not timeline:
        return 0
    tones = [e["tone_avg"] for e in timeline]
    n = len(tones)
    mean_tone = sum(tones) / n
    tone_negativity_score = max(0.0, min(100.0, 50.0 - mean_tone * 5.0))
    variance = sum((t - mean_tone) ** 2 for t in tones) / n
    std_tone = math.sqrt(variance) if variance > 0 else 0.0
    volatility_score = max(0.0, min(100.0, std_tone * 20.0))
    total_articles = sum(e.get("article_count", 0) for e in timeline) or len(articles)
    volume_score = min(100.0, math.log10(total_articles + 1) * 37.0) if total_articles > 0 else 0.0
    index = tone_negativity_score * 0.40 + volatility_score * 0.35 + volume_score * 0.25
    return max(0, min(100, int(round(index))))


def _analyze_video_sentiment(videos: List[Dict]) -> Dict:
    if not videos:
        return {}
    negative_keywords = ["war", "conflict", "crisis", "threat", "danger", "attack", "sanctions", "collapse"]
    positive_keywords = ["peace", "cooperation", "growth", "success", "partnership", "progress", "agreement"]
    neg_count = sum(1 for v in videos if any(kw in f"{v.get('title', '')} {v.get('description', '')}".lower() for kw in negative_keywords))
    pos_count = sum(1 for v in videos if any(kw in f"{v.get('title', '')} {v.get('description', '')}".lower() for kw in positive_keywords))
    total = len(videos)
    return {"total_videos": total, "negative_ratio": round(neg_count / total, 2) if total else 0,
            "positive_ratio": round(pos_count / total, 2) if total else 0,
            "neutral_ratio": round((total - neg_count - pos_count) / total, 2) if total else 0}


async def _run_analysis(params: Dict[str, Any]) -> Dict[str, Any]:
    country = (params.get("country") or params.get("query") or "").strip()
    if not country:
        return {"error": "缺少必需参数 country", "for_llm": "Error: missing country parameter"}

    topic = (params.get("topic") or "").strip()
    days = int(params.get("days") or 14)
    compare_with = (params.get("compare_with") or "").strip()
    timespan = f"{days}d"

    query = country
    if topic and not any('\u4e00' <= c <= '\u9fff' for c in topic):
        query = f"{country} {topic}"

    yt_query = f"{country} {topic}" if topic else f"{country} geopolitics"

    tone_raw, artlist_raw, youtube_videos = await asyncio.gather(
        _gdelt_search(query, "tonechart", timespan, max_records=250),
        _gdelt_search(query, "artlist", timespan, max_records=50),
        _youtube_search(yt_query, max_results=10),
        return_exceptions=True,
    )
    if isinstance(tone_raw, Exception):
        tone_raw = None
    if isinstance(artlist_raw, Exception):
        artlist_raw = None
    if isinstance(youtube_videos, Exception):
        youtube_videos = []

    tone_timeline = _fetch_tone_timeline(tone_raw)
    narrative_articles = _fetch_narrative_articles(artlist_raw)
    shift_points = _detect_narrative_shift(tone_timeline)
    cognitive_index = _calculate_cognitive_index(tone_timeline, narrative_articles)
    video_sentiment = _analyze_video_sentiment(youtube_videos)

    # Optional comparison
    comparison = None
    if compare_with:
        compare_query = compare_with
        if topic and not any('\u4e00' <= c <= '\u9fff' for c in topic):
            compare_query = f"{compare_with} {topic}"
        compare_raw = await _gdelt_search(compare_query, "tonechart", timespan, max_records=250)
        compare_timeline = _fetch_tone_timeline(compare_raw) if not isinstance(compare_raw, Exception) else []
        if tone_timeline and compare_timeline:
            tones_a = [e["tone_avg"] for e in tone_timeline]
            tones_b = [e["tone_avg"] for e in compare_timeline]
            mean_a = sum(tones_a) / len(tones_a)
            mean_b = sum(tones_b) / len(tones_b)
            comparison = {
                country: {"stats": {"mean_tone": round(mean_a, 3), "data_points": len(tones_a)}},
                compare_with: {"stats": {"mean_tone": round(mean_b, 3), "data_points": len(tones_b)}},
                "tone_gap": round(mean_a - mean_b, 3),
            }

    for_llm = (
        f"{country} 认知域分析完成：情感数据点 {len(tone_timeline)} 个，"
        f"代表性文章 {len(narrative_articles)} 篇，"
        f"检测到 {len(shift_points)} 个舆论转折点，"
        f"博弈指数 {cognitive_index}。"
        + (f"YouTube视频 {len(youtube_videos)} 条，负面占比 {video_sentiment.get('negative_ratio', 0):.0%}。" if youtube_videos else "")
    )

    return {
        "country": country,
        "topic": topic or None,
        "days": days,
        "tone_timeline": tone_timeline,
        "narrative_articles": narrative_articles,
        "shift_points": shift_points,
        "cognitive_index": cognitive_index,
        "comparison": comparison,
        "youtube_videos": youtube_videos,
        "video_sentiment": video_sentiment,
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
        parser.add_argument("--topic", default="")
        parser.add_argument("--days", type=int, default=14)
        parser.add_argument("--compare_with", default="")
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())

    result = main(params)
    print(_json.dumps(result, ensure_ascii=False))
