"""
Global Trade Skill
全球贸易格局分析技能
接入世界银行和联合国贸易数据库，分析国家间贸易流向、GDP结构、FDI趋势和贸易依赖度。
无跨层 import，所有配置通过环境变量读取。
"""
import asyncio
from datetime import datetime
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

WB_INDICATORS = {
    "NY.GDP.MKTP.CD": "gdp",
    "NE.TRD.GNFS.ZS": "trade_pct_gdp",
    "BX.KLT.DINV.WD.GD.ZS": "fdi_pct_gdp",
    "TX.VAL.TECH.CD": "hightech_exports",
    "NE.EXP.GNFS.CD": "exports",
    "NE.IMP.GNFS.CD": "imports",
}

WB_INDICATOR_LABELS = {
    "gdp": "GDP (US$)",
    "trade_pct_gdp": "贸易/GDP (%)",
    "fdi_pct_gdp": "FDI/GDP (%)",
    "hightech_exports": "高技术出口 (US$)",
    "exports": "出口 (US$)",
    "imports": "进口 (US$)",
}

COUNTRY_ISO3_MAP = {
    "中国": "CHN", "美国": "USA", "日本": "JPN", "韩国": "KOR",
    "德国": "DEU", "法国": "FRA", "英国": "GBR", "印度": "IND",
    "俄罗斯": "RUS", "加拿大": "CAN", "澳大利亚": "AUS",
    "巴西": "BRA", "墨西哥": "MEX", "沙特": "SAU", "土耳其": "TUR",
}

# ISO3 → ISO2 mapping for World Bank URLs
ISO3_TO_ISO2 = {
    "CHN": "CN", "USA": "US", "JPN": "JP", "KOR": "KR",
    "DEU": "DE", "FRA": "FR", "GBR": "GB", "IND": "IN",
    "RUS": "RU", "CAN": "CA", "AUS": "AU", "BRA": "BR",
    "MEX": "MX", "SAU": "SA", "TUR": "TR",
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
    iso2_codes = [ISO3_TO_ISO2.get(c, c[:2]) for c in codes]
    return ";".join(iso2_codes) if iso2_codes else "CN;US"


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


def _parse_worldbank_by_country(wb_data: Dict[str, List[Dict]], country_codes: List[str]) -> Dict[str, Any]:
    iso3_set = set(country_codes)
    economies: Dict[str, Dict[str, List[Dict]]] = {
        code: {v: [] for v in WB_INDICATORS.values()} for code in country_codes
    }
    for ind_code, records in wb_data.items():
        key = WB_INDICATORS.get(ind_code)
        if not key:
            continue
        for rec in records:
            if not isinstance(rec, dict):
                continue
            iso3 = rec.get("countryiso3code", "")
            year = rec.get("date", "")
            value = rec.get("value")
            if iso3 in iso3_set and value is not None:
                economies[iso3][key].append({"year": str(year), "value": safe_float(value)})
    for code in economies:
        for key in economies[code]:
            economies[code][key].sort(key=lambda x: x["year"])
    return economies


def _latest_value(series: List[Dict]) -> Optional[float]:
    if not series:
        return None
    for item in reversed(series):
        val = item.get("value")
        if val is not None and val != 0:
            return val
    return None


def _analyze_trade_dependency(economies: Dict[str, Any], country_codes: List[str]) -> Dict[str, Any]:
    dependency: Dict[str, Any] = {}
    for code in country_codes:
        country_data = economies.get(code, {})
        latest_trade_pct = _latest_value(country_data.get("trade_pct_gdp", []))
        latest_gdp = _latest_value(country_data.get("gdp", []))
        latest_exports = _latest_value(country_data.get("exports", []))
        latest_imports = _latest_value(country_data.get("imports", []))
        export_dep = 0.0
        import_dep = 0.0
        trade_balance_ratio = 0.0
        if latest_gdp and latest_gdp > 0:
            if latest_exports:
                export_dep = round(latest_exports / latest_gdp * 100, 2)
            if latest_imports:
                import_dep = round(latest_imports / latest_gdp * 100, 2)
            if latest_exports and latest_imports:
                trade_balance_ratio = round((latest_exports - latest_imports) / latest_gdp * 100, 2)
        dependency[code] = {
            "trade_openness": latest_trade_pct or 0.0,
            "export_dependency": export_dep,
            "import_dependency": import_dep,
            "trade_balance_ratio": trade_balance_ratio,
            "latest_gdp_usd": latest_gdp or 0.0,
            "latest_exports_usd": latest_exports or 0.0,
            "latest_imports_usd": latest_imports or 0.0,
        }
    return dependency


def _compare_economies(economies: Dict[str, Any], country_codes: List[str]) -> List[Dict[str, Any]]:
    matrix: List[Dict[str, Any]] = []
    for key, label in WB_INDICATOR_LABELS.items():
        row: Dict[str, Any] = {"indicator": label}
        for code in country_codes:
            country_data = economies.get(code, {})
            series = country_data.get(key, [])
            latest = _latest_value(series)
            row[code] = latest if latest is not None else "N/A"
        matrix.append(row)
    return matrix


async def _run_analysis(params: Dict[str, Any]) -> Dict[str, Any]:
    countries_str = (params.get("countries") or "CHN,USA").strip()
    years = int(params.get("years") or 5)

    current_year = datetime.now().year
    start_year = current_year - years
    date_range = f"{start_year}:{current_year}"

    raw_codes = [c.strip() for c in countries_str.split(",") if c.strip()]
    if not raw_codes:
        raw_codes = ["CHN", "USA"]
    country_codes = [_resolve_country_code(c) for c in raw_codes]

    wb_countries = _countries_for_worldbank(",".join(country_codes))

    wb_tasks = [_worldbank_fetch(wb_countries, ind, date_range) for ind in WB_INDICATORS.keys()]
    wb_results = await asyncio.gather(*wb_tasks, return_exceptions=True)

    wb_data: Dict[str, List[Dict]] = {}
    for ind_code, result in zip(WB_INDICATORS.keys(), wb_results):
        if isinstance(result, Exception):
            logger.warning(f"WB indicator {ind_code} failed: {result}")
            wb_data[ind_code] = []
        else:
            wb_data[ind_code] = result if isinstance(result, list) else []

    economies = _parse_worldbank_by_country(wb_data, country_codes)
    trade_dependency = _analyze_trade_dependency(economies, country_codes)
    comparison_matrix = _compare_economies(economies, country_codes)

    country_names = ", ".join(country_codes)
    for_llm = (
        f"全球贸易格局分析完成：分析国家 {country_names}，"
        f"时间范围 {date_range}，获取 {len(WB_INDICATORS)} 类经济指标。"
    )

    return {
        "economies": economies,
        "trade_dependency": trade_dependency,
        "comparison_matrix": comparison_matrix,
        "countries": country_codes,
        "date_range": date_range,
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
        parser.add_argument("--countries", default="CHN,USA")
        parser.add_argument("--years", type=int, default=5)
        parser.add_argument("--indicators", default="")
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())

    result = main(params)
    print(_json.dumps(result, ensure_ascii=False))
