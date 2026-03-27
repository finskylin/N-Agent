"""
Monetary System Skill
货币体系博弈分析技能
接入 IMF 宏观经济数据库和美联储 FRED 数据库，分析全球货币体系演变、
利率政策走向、美元指数和黄金价格联动
"""
import os
import math
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

FRED_SERIES_CONFIG: Dict[str, Dict[str, str]] = {
    "FEDFUNDS": {"label": "联邦基金利率", "unit": "%"},
    "DGS10": {"label": "10年期美国国债收益率", "unit": "%"},
    "DTWEXBGS": {"label": "美元指数(贸易加权)", "unit": "index"},
    "GOLDAMGBD228NLBM": {"label": "伦敦金价(美元/盎司)", "unit": "USD/oz"},
    "DEXCHUS": {"label": "美元/人民币汇率", "unit": "CNY/USD"},
}

IMF_INDICATORS: Dict[str, str] = {
    "NGDP_RPCH": "GDP增长率(%)",
    "PCPIPCH": "通胀率(CPI,%)",
    "BCA_NGDPD": "经常账户余额(占GDP%)",
}

DISCLAIMER = "数据来源：IMF World Economic Outlook、美联储FRED数据库。数据仅供参考，不构成投资建议。"


def _safe_float(val, default=None):
    if val is None or val == "" or val == ".":
        return default
    try:
        result = float(val)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (ValueError, TypeError):
        return default


def _resolve_country_code(code: str) -> str:
    """统一国家代码格式"""
    mapping = {
        "CN": "CHN", "US": "USA", "JP": "JPN", "DE": "DEU",
        "GB": "GBR", "FR": "FRA", "IT": "ITA", "CA": "CAN",
        "AU": "AUS", "KR": "KOR", "IN": "IND", "BR": "BRA",
        "RU": "RUS", "中国": "CHN", "美国": "USA", "日本": "JPN",
    }
    return mapping.get(code.upper(), code.upper())


def _fred_fetch(series_id: str, limit: int = 60) -> List[Dict]:
    """从 FRED API 获取时间序列数据"""
    api_key = os.environ.get("FRED_API_KEY", "")
    if not api_key:
        logger.info(f"FRED_API_KEY not set, skipping series {series_id}")
        return []

    import urllib.request
    import json

    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={api_key}&file_type=json"
        f"&limit={limit}&sort_order=desc"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        observations = data.get("observations", [])
        # Reverse to ascending order
        observations.reverse()
        return observations
    except Exception as e:
        logger.warning(f"FRED fetch {series_id} failed: {e}")
        return []


def _imf_fetch(indicator: str, countries: str) -> Dict:
    """从 IMF API 获取宏观经济数据"""
    import urllib.request
    import json

    # countries: comma-separated ISO3 codes like CHN,USA
    country_list = countries.replace(",", "+")
    url = (
        f"https://www.imf.org/external/datamapper/api/v1/{indicator}/{country_list}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data
    except Exception as e:
        logger.warning(f"IMF fetch {indicator} failed: {e}")
        return {}


def _fetch_fred_series(include_commodities: bool) -> Dict[str, Any]:
    """获取 FRED 金融市场数据"""
    series_ids = ["FEDFUNDS", "DGS10"]
    if include_commodities:
        series_ids.extend(["DTWEXBGS", "GOLDAMGBD228NLBM", "DEXCHUS"])

    result: Dict[str, Any] = {}
    for sid in series_ids:
        cfg = FRED_SERIES_CONFIG.get(sid, {"label": sid, "unit": ""})
        raw = _fred_fetch(sid, limit=60)
        observations = []
        for obs in raw:
            date = obs.get("date", "")
            val = _safe_float(obs.get("value"))
            if val is not None:
                observations.append({"date": date, "value": val})

        result[sid] = {
            "label": cfg["label"],
            "unit": cfg["unit"],
            "observations": observations,
            "latest": observations[-1] if observations else None,
        }
    return result


def _fetch_imf_macro(countries: str) -> Dict[str, Any]:
    """获取 IMF 宏观数据"""
    result: Dict[str, Any] = {}
    for indicator, label in IMF_INDICATORS.items():
        resp = _imf_fetch(indicator, countries)
        values = resp.get("values", {})
        indicator_data = values.get(indicator, {})
        parsed: Dict[str, Dict[str, float]] = {}
        for country_code, year_data in indicator_data.items():
            if isinstance(year_data, dict):
                parsed[country_code] = {
                    yr: _safe_float(val, default=None)
                    for yr, val in year_data.items()
                    if _safe_float(val, default=None) is not None
                }
        result[indicator] = {"label": label, "data": parsed}
    return result


def _compute_trend(observations: List[Dict], window: int = 10) -> Optional[float]:
    """计算近 window 期变化量"""
    if not observations or len(observations) < 2:
        return None
    recent = observations[-window:] if len(observations) >= window else observations
    first_val = recent[0].get("value")
    last_val = recent[-1].get("value")
    if first_val is None or last_val is None:
        return None
    return round(last_val - first_val, 4)


def _get_latest_value(observations: List[Dict]) -> Optional[float]:
    if not observations:
        return None
    return observations[-1].get("value")


def _analyze_monetary_stance(fred_series: Dict[str, Any]) -> Dict[str, Any]:
    """分析货币政策鹰鸽倾向"""
    fedfunds_obs = fred_series.get("FEDFUNDS", {}).get("observations", [])
    dgs10_obs = fred_series.get("DGS10", {}).get("observations", [])

    stance = "数据不足"
    confidence = "低"
    signals: List[Dict] = []

    ff_trend = _compute_trend(fedfunds_obs)
    if ff_trend is not None:
        if ff_trend > 0.05:
            stance = "鹰派"
            confidence = "高" if ff_trend > 0.25 else "中"
            signals.append({"type": "hawkish", "message": f"联邦基金利率呈上升趋势 (变动 {ff_trend:+.2f}%)"})
        elif ff_trend < -0.05:
            stance = "鸽派"
            confidence = "高" if ff_trend < -0.25 else "中"
            signals.append({"type": "dovish", "message": f"联邦基金利率呈下降趋势 (变动 {ff_trend:+.2f}%)"})
        else:
            stance = "中性"
            confidence = "中"
            signals.append({"type": "neutral", "message": "联邦基金利率保持稳定"})

    t10_trend = _compute_trend(dgs10_obs)
    if t10_trend is not None:
        if t10_trend > 0.1:
            signals.append({"type": "hawkish", "message": f"10年期国债收益率上升 (变动 {t10_trend:+.2f}%)，市场预期偏紧缩"})
        elif t10_trend < -0.1:
            signals.append({"type": "dovish", "message": f"10年期国债收益率下降 (变动 {t10_trend:+.2f}%)，市场预期偏宽松"})

    ff_latest = _get_latest_value(fedfunds_obs)
    t10_latest = _get_latest_value(dgs10_obs)
    term_spread = None
    if ff_latest is not None and t10_latest is not None:
        term_spread = round(t10_latest - ff_latest, 2)
        if term_spread < 0:
            signals.append({"type": "warning", "message": f"收益率曲线倒挂 (期限利差 {term_spread}%)，可能预示经济衰退"})
        elif term_spread < 0.5:
            signals.append({"type": "caution", "message": f"收益率曲线趋平 (期限利差 {term_spread}%)"})

    return {
        "stance": stance,
        "confidence": confidence,
        "signals": signals,
        "fed_funds_latest": ff_latest,
        "treasury_10y_latest": t10_latest,
        "term_spread": term_spread,
    }


def _assess_currency_dynamics(fred_series: Dict[str, Any]) -> Dict[str, Any]:
    """分析美元指数与黄金/人民币联动"""
    dollar_obs = fred_series.get("DTWEXBGS", {}).get("observations", [])
    gold_obs = fred_series.get("GOLDAMGBD228NLBM", {}).get("observations", [])
    cny_obs = fred_series.get("DEXCHUS", {}).get("observations", [])

    dynamics: Dict[str, Any] = {"signals": []}

    dollar_trend = _compute_trend(dollar_obs)
    if dollar_trend is not None:
        dynamics["dollar_trend"] = {
            "direction": "升值" if dollar_trend > 0 else "贬值" if dollar_trend < 0 else "持平",
            "change": round(dollar_trend, 2),
            "latest": _get_latest_value(dollar_obs),
        }

    gold_trend = _compute_trend(gold_obs)
    if gold_trend is not None:
        dynamics["gold_trend"] = {
            "direction": "上涨" if gold_trend > 0 else "下跌" if gold_trend < 0 else "持平",
            "change": round(gold_trend, 2),
            "latest": _get_latest_value(gold_obs),
        }

    cny_trend = _compute_trend(cny_obs)
    if cny_trend is not None:
        dynamics["cny_trend"] = {
            "direction": "贬值" if cny_trend > 0 else "升值" if cny_trend < 0 else "持平",
            "change": round(cny_trend, 4),
            "latest": _get_latest_value(cny_obs),
        }

    if dollar_trend is not None and gold_trend is not None:
        if dollar_trend > 0 and gold_trend > 0:
            dynamics["signals"].append({"type": "anomaly", "message": "美元和黄金同涨，可能反映避险情绪升温或去美元化加速"})
        elif dollar_trend < 0 and gold_trend < 0:
            dynamics["signals"].append({"type": "anomaly", "message": "美元和黄金同跌，可能反映风险偏好上升"})
        elif dollar_trend > 0 and gold_trend < 0:
            dynamics["signals"].append({"type": "normal", "message": "美元走强、黄金走弱，符合传统负相关逻辑"})
        elif dollar_trend < 0 and gold_trend > 0:
            dynamics["signals"].append({"type": "normal", "message": "美元走弱、黄金走强，符合传统负相关逻辑"})

    cny_latest = _get_latest_value(cny_obs)
    if cny_latest is not None:
        if cny_latest < 7.0:
            dynamics["signals"].append({"type": "info", "message": f"美元/人民币 {cny_latest}，人民币处于较强区间"})
        elif cny_latest > 7.3:
            dynamics["signals"].append({"type": "warning", "message": f"美元/人民币 {cny_latest}，人民币承压明显"})

    return dynamics


def _latest_year_value(year_data: Dict[str, Any]) -> Optional[float]:
    if not year_data:
        return None
    latest_year = max(year_data.keys())
    val = year_data.get(latest_year)
    if val is not None:
        return round(float(val), 2)
    return None


def _build_table_items(imf_macro: Dict[str, Any], country_codes: List[str]) -> List[Dict]:
    gdp_data = imf_macro.get("NGDP_RPCH", {}).get("data", {})
    inf_data = imf_macro.get("PCPIPCH", {}).get("data", {})
    bca_data = imf_macro.get("BCA_NGDPD", {}).get("data", {})
    items = []
    for cc in country_codes:
        items.append({
            "country": cc,
            "gdp_growth": _latest_year_value(gdp_data.get(cc, {})),
            "inflation": _latest_year_value(inf_data.get(cc, {})),
            "current_account": _latest_year_value(bca_data.get(cc, {})),
        })
    return items


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    countries_param = params.get("countries", "CHN,USA")
    include_commodities = params.get("include_commodities", True)
    if isinstance(include_commodities, str):
        include_commodities = include_commodities.lower() not in ("false", "0", "no")

    country_codes = [_resolve_country_code(c.strip()) for c in countries_param.split(",")]
    countries_str = ",".join(country_codes)

    try:
        fred_series = _fetch_fred_series(include_commodities)
        imf_macro = _fetch_imf_macro(countries_str)

        monetary_stance = _analyze_monetary_stance(fred_series)
        currency_dynamics = _assess_currency_dynamics(fred_series)

        items = _build_table_items(imf_macro, country_codes)
        columns = [
            {"key": "country", "label": "国家"},
            {"key": "gdp_growth", "label": "GDP增长率(%)"},
            {"key": "inflation", "label": "通胀率(%)"},
            {"key": "current_account", "label": "经常账户(占GDP%)"},
        ]

        # Build price data for charts
        price_data = []
        for sid in ["DTWEXBGS", "GOLDAMGBD228NLBM", "DEXCHUS"]:
            series = fred_series.get(sid, {})
            obs = series.get("observations", [])
            if obs:
                price_data.append({
                    "series_id": sid,
                    "label": series.get("label", sid),
                    "unit": series.get("unit", ""),
                    "data": obs[-30:],
                })

        result = {
            "title": f"货币体系博弈分析 ({countries_str})",
            "items": items,
            "columns": columns,
            "imf_macro": imf_macro,
            "fred_series": fred_series,
            "monetary_stance": monetary_stance,
            "currency_dynamics": currency_dynamics,
            "price_data": price_data,
            "countries": country_codes,
            "disclaimer": DISCLAIMER,
            "data_source": "IMF WEO + FRED",
        }

        # Summarize key signals
        all_signals = monetary_stance.get("signals", []) + currency_dynamics.get("signals", [])
        signal_text = "；".join([s.get("message", "") for s in all_signals[:5]]) if all_signals else "数据获取中"

        result["for_llm"] = {
            "countries": country_codes,
            "monetary_stance": monetary_stance.get("stance", ""),
            "monetary_confidence": monetary_stance.get("confidence", ""),
            "fed_funds_latest": monetary_stance.get("fed_funds_latest"),
            "treasury_10y_latest": monetary_stance.get("treasury_10y_latest"),
            "term_spread": monetary_stance.get("term_spread"),
            "dollar_trend": currency_dynamics.get("dollar_trend", {}).get("direction", ""),
            "gold_trend": currency_dynamics.get("gold_trend", {}).get("direction", ""),
            "cny_trend": currency_dynamics.get("cny_trend", {}).get("direction", ""),
            "key_signals": [s.get("message", "") for s in all_signals[:3]],
            "analysis": signal_text,
        }
        return result

    except Exception as e:
        logger.error(f"货币体系分析失败: {e}", exc_info=True)
        err = f"货币体系分析失败: {str(e)}"
        return {"error": err, "for_llm": {"error": err}}


if __name__ == "__main__":
    import sys, json as _json
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--countries", default="CHN,USA")
        parser.add_argument("--include_commodities", default="true")
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())
    result = main(params)
    print(_json.dumps(result, ensure_ascii=False, default=str))
