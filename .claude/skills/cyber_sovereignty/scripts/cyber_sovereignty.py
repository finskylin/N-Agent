"""
数字主权与网络威胁态势分析技能
整合 MITRE ATT&CK 公开威胁框架和 GDELT 网络安全新闻，
分析 APT 组织攻击技术、网络安全事件趋势和数字主权政策动态。

本技能仅用于防御性安全研究和学术分析，所有数据源均为公开可获取的信息。
MITRE ATT&CK 是由美国非营利组织 MITRE 维护的公共知识库。
无跨层 import，所有配置通过环境变量读取。
"""
import asyncio
import json
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

# MITRE ATT&CK STIX JSON URL (公开知识库)
MITRE_ATTACK_URL = (
    "https://raw.githubusercontent.com/mitre/cti/master/"
    "enterprise-attack/enterprise-attack.json"
)

# 模块级缓存: 避免重复下载 ~50MB 的 STIX JSON
_mitre_cache: Optional[Dict[str, Any]] = None

COUNTRY_EN_MAP = {
    "中国": "China", "美国": "United States", "俄罗斯": "Russia",
    "英国": "United Kingdom", "德国": "Germany", "法国": "France",
    "日本": "Japan", "韩国": "South Korea", "印度": "India",
    "以色列": "Israel", "伊朗": "Iran", "朝鲜": "North Korea",
    "越南": "Vietnam", "巴基斯坦": "Pakistan", "土耳其": "Turkey",
    "乌克兰": "Ukraine", "巴西": "Brazil", "加拿大": "Canada",
    "澳大利亚": "Australia", "沙特": "Saudi Arabia",
}

COUNTRY_ISO2_MAP = {
    "中国": "CN", "美国": "US", "俄罗斯": "RU", "英国": "GB",
    "德国": "DE", "法国": "FR", "日本": "JP", "韩国": "KR",
    "印度": "IN", "以色列": "IL", "伊朗": "IR", "朝鲜": "KP",
    "乌克兰": "UA", "澳大利亚": "AU", "加拿大": "CA",
}

THREAT_CATEGORY_KEYWORDS = {
    "勒索软件": [
        "ransomware", "ransom", "encrypt", "decryptor", "lockbit",
        "blackcat", "conti", "ryuk", "revil",
    ],
    "数据泄露": [
        "data breach", "data leak", "exposed", "credential",
        "personal data", "database", "records", "compromised",
    ],
    "国家级威胁": [
        "state-sponsored", "nation-state", "apt", "advanced persistent",
        "espionage", "cyber espionage", "intelligence",
    ],
    "关键基础设施": [
        "critical infrastructure", "power grid", "water system",
        "pipeline", "scada", "ics", "industrial control",
    ],
    "数字主权政策": [
        "data sovereignty", "gdpr", "data localization", "privacy",
        "regulation", "compliance", "digital sovereignty", "cyber law",
    ],
    "供应链攻击": [
        "supply chain", "solarwinds", "software supply",
        "third party", "vendor", "dependency",
    ],
}


def safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _resolve_country_en(country: str) -> str:
    return COUNTRY_EN_MAP.get(country, country)


def _resolve_country_iso2(country: str) -> str:
    return COUNTRY_ISO2_MAP.get(country, country.upper()[:2])


async def _gdelt_search(query: str, mode: str = "artlist", timespan: str = "7d", max_records: int = 50) -> Optional[Dict]:
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


async def _cloudflare_radar_fetch(location: str = "") -> Dict[str, Any]:
    cloudflare_token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not cloudflare_token or not aiohttp:
        return {}
    url = "https://api.cloudflare.com/client/v4/radar/attacks/layer3/timeseries"
    headers = {"Authorization": f"Bearer {cloudflare_token}", "Content-Type": "application/json"}
    params: Dict[str, Any] = {"dateRange": "7d", "format": "json"}
    if location:
        params["location"] = location
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    result = data.get("result", {})
                    serie = result.get("serie_0", {})
                    timestamps = result.get("timestamps", [])
                    values = serie.get("DDoS", serie.get("total", []))
                    if timestamps and values:
                        return {
                            "location": location or "global",
                            "data_points": len(timestamps),
                            "timeline": [{"ts": t, "value": v} for t, v in zip(timestamps[:20], values[:20])],
                        }
    except Exception as e:
        logger.warning(f"Cloudflare Radar fetch failed: {e}")
    return {}


async def _fetch_mitre_attack() -> Optional[Dict[str, Any]]:
    global _mitre_cache
    if _mitre_cache is not None:
        return _mitre_cache
    if not aiohttp:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(MITRE_ATTACK_URL, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    return None
                raw_bytes = await resp.read()
    except Exception as e:
        logger.warning(f"MITRE ATT&CK download failed: {e}")
        return None

    try:
        stix_bundle = json.loads(raw_bytes)
    except json.JSONDecodeError as e:
        logger.warning(f"MITRE STIX JSON parse failed: {e}")
        return None

    objects = stix_bundle.get("objects", [])
    intrusion_sets: List[Dict] = []
    for obj in objects:
        if obj.get("type") != "intrusion-set":
            continue
        if obj.get("revoked", False):
            continue
        description = obj.get("description", "")
        aliases = obj.get("aliases", [])
        intrusion_sets.append({
            "name": obj.get("name", ""),
            "description": description[:500] if description else "",
            "aliases": aliases[:10],
            "created": obj.get("created", ""),
            "modified": obj.get("modified", ""),
        })

    attack_patterns: List[Dict] = []
    for obj in objects:
        if obj.get("type") != "attack-pattern":
            continue
        if obj.get("revoked", False):
            continue
        description = obj.get("description", "")
        ext_refs = obj.get("external_references", [])
        mitre_id = ""
        mitre_url = ""
        for ref in ext_refs:
            if ref.get("source_name") == "mitre-attack":
                mitre_id = ref.get("external_id", "")
                mitre_url = ref.get("url", "")
                break
        attack_patterns.append({
            "name": obj.get("name", ""),
            "mitre_id": mitre_id,
            "mitre_url": mitre_url,
            "description": description[:300] if description else "",
        })

    parsed = {"intrusion_sets": intrusion_sets, "attack_patterns": attack_patterns}
    _mitre_cache = parsed
    return parsed


def _filter_by_country(apt_groups: List[Dict], country_en: str) -> List[Dict]:
    if not country_en:
        return apt_groups
    country_lower = country_en.lower()
    matched = []
    for group in apt_groups:
        description = (group.get("description", "") or "").lower()
        aliases = [a.lower() for a in group.get("aliases", [])]
        name_lower = (group.get("name", "") or "").lower()
        if (country_lower in description or country_lower in name_lower
                or any(country_lower in alias for alias in aliases)):
            matched.append(group)
    return matched


def _extract_news(result: Optional[Dict]) -> List[Dict]:
    if not result:
        return []
    articles = result.get("articles", [])
    return [{
        "title": a.get("title", ""),
        "url": a.get("url", ""),
        "source": a.get("domain", a.get("source", "")),
        "date": a.get("seendate", ""),
        "language": a.get("language", ""),
        "tone": safe_float(a.get("tone", 0)),
    } for a in articles]


def _assess_threat_landscape(apt_groups: List[Dict], attack_techniques: List[Dict],
                              cyber_news: List[Dict], country_en: str) -> Dict[str, Any]:
    category_counts: Dict[str, int] = {cat: 0 for cat in THREAT_CATEGORY_KEYWORDS}
    uncategorized = 0
    for article in cyber_news:
        title_lower = (article.get("title", "") or "").lower()
        matched = False
        for category, keywords in THREAT_CATEGORY_KEYWORDS.items():
            for keyword in keywords:
                if keyword in title_lower:
                    category_counts[category] += 1
                    matched = True
                    break
            if matched:
                break
        if not matched:
            uncategorized += 1

    negative_news = sum(1 for a in cyber_news if safe_float(a.get("tone", 0)) < -2.0)
    negative_ratio = round(negative_news / len(cyber_news), 2) if cyber_news else 0.0

    top_categories = sorted(category_counts.items(), key=lambda x: x[1], reverse=True)
    primary_threats = [{"category": cat, "count": count} for cat, count in top_categories if count > 0]

    country_label = country_en or "全球"
    summary_parts = [f"{country_label}网络安全态势分析："]
    if apt_groups:
        group_names = [g.get("name", "") for g in apt_groups[:5]]
        summary_parts.append(f"关联 APT 组织 {len(apt_groups)} 个（包括 {', '.join(group_names)}）。")
    if primary_threats:
        top = primary_threats[0]
        summary_parts.append(f"当前主要威胁类型为「{top['category']}」（相关报道 {top['count']} 篇）。")
    if cyber_news:
        summary_parts.append(f"近期共发现 {len(cyber_news)} 条网络安全相关报道。")
    if not apt_groups and not cyber_news:
        summary_parts.append("暂无足够数据进行详细评估。")

    return {
        "country": country_en or "全球",
        "total_apt_groups": len(apt_groups),
        "total_techniques": len(attack_techniques),
        "total_news": len(cyber_news),
        "negative_sentiment_ratio": negative_ratio,
        "threat_categories": category_counts,
        "primary_threats": primary_threats[:5],
        "uncategorized_news": uncategorized,
        "summary": "".join(summary_parts),
    }


async def _run_analysis(params: Dict[str, Any]) -> Dict[str, Any]:
    country = (params.get("country") or "").strip()
    focus = (params.get("focus") or "cyber_threats").strip()
    days = int(params.get("days") or 7)
    timespan = f"{days}d"

    country_en = _resolve_country_en(country) if country else ""
    location = _resolve_country_iso2(country) if country else ""

    if focus == "data_policy":
        query_parts = ["(data sovereignty OR GDPR OR data privacy OR cyber regulation)"]
    else:
        query_parts = ["(cyber attack OR data breach OR hacking OR ransomware)"]
    if country_en:
        query_parts.insert(0, country_en)
    query_str = " ".join(query_parts)

    mitre_data, news_result, cloudflare_result = await asyncio.gather(
        _fetch_mitre_attack(),
        _gdelt_search(query_str, mode="artlist", timespan=timespan, max_records=50),
        _cloudflare_radar_fetch(location),
        return_exceptions=True,
    )
    if isinstance(mitre_data, Exception):
        mitre_data = None
    if isinstance(news_result, Exception):
        news_result = None
    if isinstance(cloudflare_result, Exception):
        cloudflare_result = {}

    apt_groups: List[Dict] = []
    attack_techniques: List[Dict] = []
    if mitre_data:
        apt_groups = mitre_data.get("intrusion_sets", [])
        attack_techniques = mitre_data.get("attack_patterns", [])
        if country_en:
            apt_groups = _filter_by_country(apt_groups, country_en)
        apt_groups = apt_groups[:30]
        attack_techniques = attack_techniques[:50]

    cyber_news = _extract_news(news_result)
    threat_assessment = _assess_threat_landscape(apt_groups, attack_techniques, cyber_news, country_en)
    cloudflare_attack_trends = cloudflare_result if isinstance(cloudflare_result, dict) else {}

    country_label = country or "全球"
    for_llm = (
        f"{country_label} 网络威胁态势分析完成：发现 {len(apt_groups)} 个 APT 组织、"
        f"{len(attack_techniques)} 种攻击技术、{len(cyber_news)} 条安全新闻。"
        + (f"Cloudflare Radar 获取到 {cloudflare_attack_trends.get('data_points', 0)} 个攻击数据点。"
           if cloudflare_attack_trends else "")
    )

    return {
        "country": country_label,
        "focus": focus,
        "days": days,
        "apt_groups": apt_groups,
        "apt_group_count": len(apt_groups),
        "attack_techniques": attack_techniques,
        "technique_count": len(attack_techniques),
        "cyber_news": cyber_news,
        "news_count": len(cyber_news),
        "threat_assessment": threat_assessment,
        "cloudflare_attack_trends": cloudflare_attack_trends,
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
        parser.add_argument("--focus", default="cyber_threats")
        parser.add_argument("--days", type=int, default=7)
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())

    result = main(params)
    print(_json.dumps(result, ensure_ascii=False))
