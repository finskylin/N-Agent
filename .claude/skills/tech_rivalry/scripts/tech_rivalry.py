"""
Tech Rivalry Skill
科技竞争力评估技能
基于 Semantic Scholar 学术数据库检索和分析特定技术领域的全球论文产出、
引用影响力和机构排名，结合 World Bank 研发支出数据计算综合创新指数。
无跨层 import，所有配置通过环境变量读取。
"""
import asyncio
from collections import defaultdict
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

TOPIC_ALIASES: Dict[str, str] = {
    "人工智能": "artificial intelligence deep learning",
    "AI": "artificial intelligence machine learning",
    "量子计算": "quantum computing",
    "半导体": "semiconductor technology",
    "芯片": "chip semiconductor integrated circuit",
    "生物技术": "biotechnology genetic engineering",
    "新能源": "renewable energy solar wind",
    "5G": "5G wireless communication",
    "6G": "6G wireless network",
    "机器人": "robotics automation",
    "自动驾驶": "autonomous driving self-driving",
    "大模型": "large language model transformer",
    "深度学习": "deep learning neural network",
    "脑机接口": "brain computer interface neural",
    "核聚变": "nuclear fusion energy",
    "中美芯片": "semiconductor chip China USA",
    "芯片竞争": "semiconductor competition technology",
}

COUNTRY_ISO3_MAP = {
    "中国": "CHN", "美国": "USA", "日本": "JPN", "韩国": "KOR",
    "德国": "DEU", "法国": "FRA", "英国": "GBR", "印度": "IND",
    "俄罗斯": "RUS", "加拿大": "CAN", "澳大利亚": "AUS",
}


def safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _resolve_country_code(country: str) -> str:
    return COUNTRY_ISO3_MAP.get(country.strip(), country.strip().upper())


def _countries_for_worldbank(countries_raw: str) -> str:
    codes = [_resolve_country_code(c.strip()) for c in countries_raw.split(",") if c.strip()]
    return ";".join(codes) if codes else "CHN;USA;JPN;KOR;DEU"


async def _semantic_scholar_search(query: str, limit: int = 20, fields: str = "title,year,citationCount,authors,fieldsOfStudy") -> List[Dict]:
    if not aiohttp:
        return []
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {"query": query, "limit": limit, "fields": fields}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    return data.get("data", [])
    except Exception as e:
        logger.warning(f"Semantic Scholar search failed: {e}")
    return []


async def _worldbank_fetch(countries: str, indicator: str, date_range: str = "2015:2024", per_page: int = 100) -> List[Dict]:
    if not aiohttp:
        return []
    url = f"https://api.worldbank.org/v2/country/{countries}/indicator/{indicator}"
    params = {"format": "json", "per_page": per_page, "date": date_range}
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


async def _oecd_rd_fetch(countries: str = "CHN+USA+JPN+KOR+DEU") -> List[Dict]:
    if not aiohttp:
        return []
    url = f"https://stats.oecd.org/SDMX-JSON/data/MSTI_PUB/GERD_GDP.{countries}.../all"
    params = {"startTime": "2015", "endTime": "2023", "format": "json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    series = data.get("dataSets", [{}])[0].get("series", {})
                    structure = data.get("structure", {})
                    dims = structure.get("dimensions", {}).get("series", [])
                    time_dims = structure.get("dimensions", {}).get("observation", [{}])[0].get("values", [])
                    country_dim = next((d for d in dims if d.get("id") == "LOCATION"), None)
                    records = []
                    for series_key, series_val in series.items():
                        obs = series_val.get("observations", {})
                        idx_parts = series_key.split(":")
                        country_code = ""
                        if country_dim:
                            c_idx = int(idx_parts[dims.index(country_dim)]) if country_dim in dims else 0
                            country_vals = country_dim.get("values", [])
                            if c_idx < len(country_vals):
                                country_code = country_vals[c_idx].get("id", "")
                        for t_idx, obs_val in obs.items():
                            t_idx_int = int(t_idx)
                            year = time_dims[t_idx_int]["id"] if t_idx_int < len(time_dims) else ""
                            value = obs_val[0] if obs_val else None
                            if value is not None and country_code:
                                records.append({"country": country_code, "year": year, "gerd_gdp": round(safe_float(value), 3)})
                    return records
    except Exception as e:
        logger.warning(f"OECD R&D fetch failed: {e}")
    return []


async def _fetch_papers(topic: str, max_papers: int) -> List[Dict]:
    fields = "title,year,citationCount,authors,fieldsOfStudy"
    papers = await _semantic_scholar_search(topic, limit=max_papers, fields=fields)
    if not papers:
        simplified = topic.split()[0] if " " in topic else topic
        if simplified != topic:
            papers = await _semantic_scholar_search(simplified, limit=max_papers, fields=fields)
    if not papers:
        for fallback in ["technology research 2024", "computer science artificial intelligence"]:
            papers = await _semantic_scholar_search(fallback, limit=max_papers, fields=fields)
            if papers:
                break
    return papers


async def _fetch_rd_expenditure(countries_raw: str) -> Dict[str, Any]:
    wb_countries = _countries_for_worldbank(countries_raw)
    records = await _worldbank_fetch(wb_countries, "GB.XPD.RSDV.GD.ZS")
    if not records:
        return {}
    country_rd: Dict[str, Dict] = {}
    for rec in records:
        country_code = rec.get("countryiso3code") or rec.get("country", {}).get("id", "")
        country_name = rec.get("country", {}).get("value", country_code)
        year = rec.get("date", "")
        value = safe_float(rec.get("value"), -1.0)
        if value < 0:
            continue
        if country_code not in country_rd or year > country_rd[country_code].get("year", ""):
            country_rd[country_code] = {
                "country": country_name,
                "country_code": country_code,
                "year": year,
                "rd_pct_gdp": round(value, 3),
            }
    return country_rd


def _analyze_by_institution(papers: List[Dict]) -> List[Dict]:
    inst_stats: Dict[str, Dict] = defaultdict(lambda: {"paper_count": 0, "citation_sum": 0, "authors": set(), "years": []})
    for paper in papers:
        authors = paper.get("authors") or []
        citation_count = int(paper.get("citationCount", 0) or 0)
        year = paper.get("year")
        for author in authors:
            affiliations = author.get("affiliations") or []
            if not affiliations:
                continue
            inst_name = affiliations[0] if isinstance(affiliations[0], str) else str(affiliations[0])
            entry = inst_stats[inst_name]
            entry["paper_count"] += 1
            entry["citation_sum"] += citation_count
            entry["authors"].add(author.get("name", "Unknown"))
            if year:
                entry["years"].append(year)

    if not inst_stats:
        for paper in papers:
            authors = paper.get("authors") or []
            if not authors:
                continue
            first_author = authors[0].get("name", "Unknown")
            citation_count = int(paper.get("citationCount", 0) or 0)
            year = paper.get("year")
            entry = inst_stats[first_author]
            entry["paper_count"] += 1
            entry["citation_sum"] += citation_count
            entry["authors"].add(first_author)
            if year:
                entry["years"].append(year)

    ranking = []
    for inst_name, stats in inst_stats.items():
        avg_citation = round(stats["citation_sum"] / stats["paper_count"], 1) if stats["paper_count"] > 0 else 0
        ranking.append({
            "institution": inst_name,
            "paper_count": stats["paper_count"],
            "citation_sum": stats["citation_sum"],
            "avg_citation": avg_citation,
            "author_count": len(stats["authors"]),
            "year_range": f"{min(stats['years'])}-{max(stats['years'])}" if stats["years"] else "N/A",
        })
    ranking.sort(key=lambda x: (x["citation_sum"], x["paper_count"]), reverse=True)
    return ranking[:30]


def _calculate_innovation_index(papers: List[Dict], institution_ranking: List[Dict],
                                  rd_expenditure: Dict[str, Any]) -> Dict[str, Any]:
    total_papers = len(papers)
    total_citations = sum(int(p.get("citationCount", 0) or 0) for p in papers)
    avg_citation = round(total_citations / total_papers, 2) if total_papers > 0 else 0
    paper_score = min(total_papers * 5, 100)
    citation_score = min(avg_citation * 2, 100)
    rd_values = [v.get("rd_pct_gdp", 0) for v in rd_expenditure.values()]
    rd_avg = round(sum(rd_values) / len(rd_values), 3) if rd_values else 0
    rd_score = min(rd_avg * 25, 100)
    weights = {"paper": 0.35, "citation": 0.40, "rd": 0.25}
    composite = round(paper_score * weights["paper"] + citation_score * weights["citation"] + rd_score * weights["rd"], 2)
    country_scores = {}
    for code, info in rd_expenditure.items():
        rd_pct = info.get("rd_pct_gdp", 0)
        country_scores[code] = {
            "country": info.get("country", code),
            "rd_pct_gdp": rd_pct,
            "rd_score": round(min(rd_pct * 25, 100), 2),
        }
    return {
        "composite_score": composite,
        "paper_score": round(paper_score, 2),
        "citation_score": round(citation_score, 2),
        "rd_score": round(rd_score, 2),
        "total_papers": total_papers,
        "total_citations": total_citations,
        "avg_citation_per_paper": avg_citation,
        "rd_avg_pct_gdp": rd_avg,
        "top_institutions": len(institution_ranking),
        "country_scores": country_scores,
        "weights": weights,
    }


def _summarize_papers(papers: List[Dict]) -> List[Dict]:
    result = []
    for p in papers:
        authors = p.get("authors") or []
        author_names = [a.get("name", "") for a in authors[:5]]
        result.append({
            "title": p.get("title", ""),
            "year": p.get("year"),
            "citationCount": int(p.get("citationCount", 0) or 0),
            "authors": author_names,
            "fieldsOfStudy": p.get("fieldsOfStudy") or [],
        })
    result.sort(key=lambda x: x["citationCount"], reverse=True)
    return result


async def _run_analysis(params: Dict[str, Any]) -> Dict[str, Any]:
    topic_raw = (params.get("topic") or "").strip()
    if not topic_raw:
        return {"error": "缺少必需参数 topic", "for_llm": "Error: missing topic parameter"}

    countries_raw = (params.get("countries") or "CHN,USA,JPN,KOR,DEU").strip()
    max_papers = min(int(params.get("max_papers") or 20), 100)

    topic_en = TOPIC_ALIASES.get(topic_raw, topic_raw)

    oecd_codes = "+".join(_resolve_country_code(c.strip()) for c in countries_raw.split(",") if c.strip())

    papers, rd_expenditure, oecd_rd_data = await asyncio.gather(
        _fetch_papers(topic_en, max_papers),
        _fetch_rd_expenditure(countries_raw),
        _oecd_rd_fetch(oecd_codes),
        return_exceptions=True,
    )
    if isinstance(papers, Exception):
        papers = []
    if isinstance(rd_expenditure, Exception):
        rd_expenditure = {}
    if isinstance(oecd_rd_data, Exception):
        oecd_rd_data = []

    institution_ranking = _analyze_by_institution(papers)
    innovation_index = _calculate_innovation_index(papers, institution_ranking, rd_expenditure)
    papers_summary = _summarize_papers(papers)

    for_llm = (
        f"科技竞争力评估完成：领域={topic_raw}，检索论文 {len(papers)} 篇，"
        f"识别机构 {len(institution_ranking)} 个，"
        f"综合创新指数 {innovation_index.get('composite_score', 0)}。"
    )

    return {
        "topic": topic_raw,
        "topic_query": topic_en,
        "paper_count": len(papers),
        "papers": papers_summary,
        "institution_ranking": institution_ranking,
        "rd_expenditure": rd_expenditure,
        "oecd_rd_data": oecd_rd_data,
        "innovation_index": innovation_index,
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
        parser.add_argument("--topic", default="")
        parser.add_argument("--countries", default="CHN,USA,JPN,KOR,DEU")
        parser.add_argument("--max_papers", type=int, default=20)
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())

    result = main(params)
    print(_json.dumps(result, ensure_ascii=False))
