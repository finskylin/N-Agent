"""
Dividend Analysis Skill
分红派息分析技能
获取历史分红数据，计算股息率、派息比率趋势，评估分红可持续性
"""
import os
import math
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


def _safe_float(val) -> float:
    if val is None or val == "-" or val == "":
        return 0.0
    try:
        result = float(val)
        if math.isnan(result) or math.isinf(result):
            return 0.0
        return result
    except (ValueError, TypeError):
        return 0.0


def _get_dividend_history(code: str) -> List[Dict]:
    """获取历史分红明细"""
    try:
        import akshare as ak
        df = ak.stock_fhps_detail_em(symbol=code)
        if df is not None and not df.empty:
            records = []
            for _, row in df.iterrows():
                cash = _safe_float(row.get("每股分红税前", row.get("派息(税前)(元)", row.get("现金分红-每股分红(元)", 0))))
                bonus = _safe_float(row.get("送股", row.get("送股(股)", 0)))
                convert = _safe_float(row.get("转增", row.get("转增(股)", 0)))
                announce_date = str(row.get("公告日期", row.get("报告期", row.get("除权除息日", ""))))
                ex_date = str(row.get("除权除息日", ""))
                year = announce_date[:4] if announce_date and len(announce_date) >= 4 else ""
                records.append({
                    "year": year,
                    "announce_date": announce_date,
                    "ex_dividend_date": ex_date,
                    "cash_per_share": round(cash, 4),
                    "bonus_per_share": round(bonus, 4),
                    "convert_per_share": round(convert, 4),
                    "dividend_yield": 0.0,
                })
            records.sort(key=lambda x: x.get("year", ""), reverse=True)
            logger.info(f"dividend_history succeeded: {code}, count={len(records)}")
            return records
    except Exception as e:
        logger.warning(f"get_dividend_history fhps failed: {code}: {e}")

    try:
        import akshare as ak
        df = ak.stock_history_dividend_detail(symbol=code, indicator="分红")
        if df is not None and not df.empty:
            records = []
            for _, row in df.iterrows():
                cash = _safe_float(row.get("每股分红税前", row.get("派息(税前)(元)", 0)))
                bonus = _safe_float(row.get("送股", row.get("送股(股)", 0)))
                announce_date = str(row.get("公告日期", row.get("除权除息日", "")))
                year = announce_date[:4] if announce_date and len(announce_date) >= 4 else ""
                records.append({
                    "year": year,
                    "announce_date": announce_date,
                    "ex_dividend_date": str(row.get("除权除息日", "")),
                    "cash_per_share": round(cash, 4),
                    "bonus_per_share": round(bonus, 4),
                    "convert_per_share": 0.0,
                    "dividend_yield": 0.0,
                })
            records.sort(key=lambda x: x.get("year", ""), reverse=True)
            return records
    except Exception as e:
        logger.warning(f"get_dividend_history fallback failed: {code}: {e}")
    return []


def _get_current_price(code: str) -> float:
    """获取当前股价"""
    try:
        import akshare as ak
        prefix = "sh" if code.startswith('6') else "sz"
        df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            row = df[df['代码'] == code]
            if not row.empty:
                return _safe_float(row.iloc[0].get('最新价', 0))
    except Exception as e:
        logger.warning(f"get_current_price failed: {code}: {e}")
    return 0.0


def _build_summary(dividend_history: List[Dict], current_price: float) -> Dict[str, Any]:
    summary = {
        "total_dividends": len(dividend_history),
        "current_price": current_price,
    }
    if not dividend_history:
        return summary

    # 最近一年分红
    latest_year = dividend_history[0].get("year", "")
    latest_year_dividends = [d for d in dividend_history if d.get("year") == latest_year]
    latest_cash = sum(d.get("cash_per_share", 0) for d in latest_year_dividends)
    summary["latest_year"] = latest_year
    summary["latest_cash_per_share"] = round(latest_cash, 4)

    # 股息率
    if current_price > 0 and latest_cash > 0:
        summary["dividend_yield"] = round(latest_cash / current_price * 100, 2)
    else:
        summary["dividend_yield"] = 0.0

    # 连续分红年数
    years_with_dividend = set()
    for d in dividend_history:
        if d.get("cash_per_share", 0) > 0 and d.get("year"):
            years_with_dividend.add(d.get("year"))
    if years_with_dividend:
        sorted_years = sorted(years_with_dividend, reverse=True)
        consecutive = 1
        for i in range(1, len(sorted_years)):
            if int(sorted_years[i-1]) - int(sorted_years[i]) == 1:
                consecutive += 1
            else:
                break
        summary["consecutive_years"] = consecutive
    else:
        summary["consecutive_years"] = 0

    # 5年平均股息率
    recent_years = sorted(set(d.get("year", "") for d in dividend_history if d.get("year")), reverse=True)[:5]
    if recent_years and current_price > 0:
        avg_cash = sum(
            d.get("cash_per_share", 0)
            for d in dividend_history
            if d.get("year") in recent_years
        ) / len(recent_years)
        summary["avg_5y_dividend_yield"] = round(avg_cash / current_price * 100, 2)

    # 可持续性评估
    consec = summary.get("consecutive_years", 0)
    dy = summary.get("dividend_yield", 0)
    if consec >= 10 and dy > 2:
        summary["sustainability"] = "高"
        summary["sustainability_score"] = 90
    elif consec >= 5 and dy > 1:
        summary["sustainability"] = "较高"
        summary["sustainability_score"] = 75
    elif consec >= 3:
        summary["sustainability"] = "中等"
        summary["sustainability_score"] = 60
    elif consec >= 1:
        summary["sustainability"] = "较低"
        summary["sustainability_score"] = 40
    else:
        summary["sustainability"] = "不明确"
        summary["sustainability_score"] = 20

    return summary


def _analyze_dividend(dividend_history: List[Dict], summary: Dict) -> str:
    signals = []
    total = summary.get("total_dividends", 0)
    if total == 0:
        return "该股票暂无分红派息记录"
    dy = summary.get("dividend_yield", 0)
    consec = summary.get("consecutive_years", 0)
    sustainability = summary.get("sustainability", "")
    latest_year = summary.get("latest_year", "")
    latest_cash = summary.get("latest_cash_per_share", 0)
    signals.append(f"共{total}条分红记录")
    if latest_cash > 0:
        signals.append(f"{latest_year}年每股分红{latest_cash:.2f}元")
    if dy > 0:
        signals.append(f"当前股息率{dy:.2f}%")
    if consec > 0:
        signals.append(f"已连续{consec}年分红")
    if sustainability:
        signals.append(f"分红可持续性{sustainability}")
    return "，".join(signals)


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")

    if not ts_code:
        return {"error": "缺少股票代码参数 ts_code", "for_llm": {"error": "缺少股票代码参数 ts_code"}}

    code = ts_code.split('.')[0] if '.' in ts_code else ts_code

    try:
        dividend_history = _get_dividend_history(code)
        current_price = _get_current_price(code)
        summary = _build_summary(dividend_history, current_price)
        analysis = _analyze_dividend(dividend_history, summary)

        result = {
            "ts_code": ts_code,
            "title": f"分红派息分析 - {ts_code}",
            "items": dividend_history[:10],
            "columns": [
                {"key": "year", "label": "年度"},
                {"key": "announce_date", "label": "公告日"},
                {"key": "cash_per_share", "label": "每股现金(元)"},
                {"key": "bonus_per_share", "label": "每股送股"},
                {"key": "convert_per_share", "label": "每股转增"},
                {"key": "dividend_yield", "label": "股息率%"},
            ],
            "summary": summary,
            "dividend_history": dividend_history,
            "analysis": analysis,
        }
        result["for_llm"] = {
            "ts_code": ts_code,
            "total_dividends": summary.get("total_dividends", 0),
            "latest_year": summary.get("latest_year", ""),
            "latest_cash_per_share": summary.get("latest_cash_per_share", 0),
            "dividend_yield": summary.get("dividend_yield", 0),
            "consecutive_years": summary.get("consecutive_years", 0),
            "sustainability": summary.get("sustainability", ""),
            "analysis": analysis,
        }
        return result

    except Exception as e:
        logger.error(f"分红派息分析失败: {e}", exc_info=True)
        err = f"分红派息分析失败: {str(e)}"
        return {"error": err, "for_llm": {"error": err}}


if __name__ == "__main__":
    import sys, json as _json
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--ts_code", default="")
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())
    result = main(params)
    print(_json.dumps(result, ensure_ascii=False, default=str))
