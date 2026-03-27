"""
RAND Reports Skill
搜索 RAND Corporation 智库研究报告。
数据源：RAND 官网搜索接口（公开）。
无跨层 import，所有配置通过环境变量读取。
"""
import asyncio
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, quote

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

DISCLAIMER = "数据来源 RAND Corporation 公开网站，仅供学术参考"

RAND_SEARCH_URL = "https://www.rand.org/search.html"
RAND_API_URL = "https://api.rand.org/search/rand"


async def _rand_api_search(query: str, limit: int = 10, year_from: Optional[int] = None,
                            year_to: Optional[int] = None) -> List[Dict]:
    """尝试 RAND JSON API 搜索"""
    if not aiohttp:
        return []
    params: Dict[str, Any] = {
        "query": query,
        "rows": limit,
        "start": 0,
        "sort": "score",
        "format": "json",
    }
    if year_from:
        params["dateFrom"] = f"{year_from}-01-01"
    if year_to:
        params["dateTo"] = f"{year_to}-12-31"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(RAND_API_URL, params=params,
                                   headers={"Accept": "application/json"},
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    docs = data.get("response", {}).get("docs", [])
                    results = []
                    for doc in docs:
                        results.append({
                            "title": doc.get("title", ""),
                            "abstract": (doc.get("description", "") or doc.get("abstract", "") or "")[:500],
                            "authors": doc.get("authors", []) if isinstance(doc.get("authors"), list) else [],
                            "date": doc.get("pubdate", doc.get("date", "")),
                            "url": doc.get("url", ""),
                            "pdf_url": doc.get("pdf_url", ""),
                            "topics": doc.get("topics", []) if isinstance(doc.get("topics"), list) else [],
                            "type": doc.get("doctype", "report"),
                        })
                    return results
    except Exception as e:
        logger.warning(f"RAND API search failed: {e}")
    return []


async def _rand_html_search(query: str, limit: int = 10) -> List[Dict]:
    """备用：从 RAND 搜索结果页面抓取"""
    if not aiohttp:
        return []
    params = {"q": query, "pageSize": limit, "type": "research_report"}
    url = f"{RAND_SEARCH_URL}?{urlencode(params)}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    results = _parse_rand_html(html, limit)
                    return results
    except Exception as e:
        logger.warning(f"RAND HTML search failed: {e}")
    return []


def _parse_rand_html(html: str, limit: int) -> List[Dict]:
    """简单正则解析 RAND 搜索结果页面"""
    results = []
    # Match article entries
    pattern = re.compile(
        r'<a[^>]+href="(https://www\.rand\.org/pubs/[^"]+)"[^>]*>\s*<[^>]+>\s*([^<]+)',
        re.I | re.S
    )
    abstract_pattern = re.compile(r'class="[^"]*abstract[^"]*"[^>]*>\s*<p>\s*([^<]{20,})', re.I)
    date_pattern = re.compile(r'<span[^>]*class="[^"]*date[^"]*"[^>]*>\s*([^<]+)', re.I)

    seen_urls = set()
    for m in pattern.finditer(html):
        url = m.group(1).strip()
        title = re.sub(r'\s+', ' ', m.group(2)).strip()
        if url in seen_urls or not title:
            continue
        seen_urls.add(url)
        abstract_m = abstract_pattern.search(html[m.start():m.start() + 2000])
        abstract = abstract_m.group(1).strip() if abstract_m else ""
        date_m = date_pattern.search(html[m.start():m.start() + 1000])
        date = date_m.group(1).strip() if date_m else ""
        results.append({
            "title": title,
            "abstract": abstract[:400],
            "authors": [],
            "date": date,
            "url": url,
            "pdf_url": url.rstrip("/") + ".pdf" if "/pubs/" in url else "",
            "topics": [],
            "type": "research_report",
        })
        if len(results) >= limit:
            break
    return results


async def _run_analysis(params: Dict[str, Any]) -> Dict[str, Any]:
    query = (params.get("query") or "").strip()
    if not query:
        return {"error": "缺少必需参数 query", "for_llm": "Error: missing query parameter"}

    limit = min(int(params.get("limit") or 10), 50)
    year_from = params.get("year_from")
    year_to = params.get("year_to")

    if year_from:
        year_from = int(year_from)
    if year_to:
        year_to = int(year_to)

    # Try API first, fallback to HTML scrape
    reports = await _rand_api_search(query, limit, year_from, year_to)
    source = "RAND API"
    if not reports:
        reports = await _rand_html_search(query, limit)
        source = "RAND HTML"

    for_llm = (
        f"RAND 报告搜索完成：查询='{query}'，找到 {len(reports)} 篇报告（数据源: {source}）。"
        + (f" 最新报告：《{reports[0]['title'][:60]}》" if reports else "")
    )

    return {
        "query": query,
        "total": len(reports),
        "reports": reports,
        "source": source,
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
        parser.add_argument("--query", default="")
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--year_from", type=int, default=0)
        parser.add_argument("--year_to", type=int, default=0)
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())

    result = main(params)
    print(_json.dumps(result, ensure_ascii=False))
