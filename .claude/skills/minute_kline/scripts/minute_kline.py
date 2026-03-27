"""
Minute Kline Skill
分钟K线数据技能
获取个股分时/分钟级别K线数据，分析日内趋势和量能变化
"""
import os
import math
import logging
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


def _get_minute_kline(code: str, period: str = "5", start_date: str = "", end_date: str = ""):
    """获取分钟K线数据"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist_min_em(
            symbol=code,
            period=period,
            start_date=start_date if start_date else "",
            end_date=end_date if end_date else "",
            adjust=""
        )
        return df
    except Exception as e:
        logger.warning(f"akshare分钟K线获取失败: {e}")
        return None


def _build_items(df) -> List[Dict]:
    items = []
    display_df = df.tail(50)
    for _, row in display_df.iterrows():
        item = {
            "time": str(row.get("时间", row.get("time", ""))),
            "open": _safe_float(row.get("开盘", row.get("open", 0))),
            "high": _safe_float(row.get("最高", row.get("high", 0))),
            "low": _safe_float(row.get("最低", row.get("low", 0))),
            "close": _safe_float(row.get("收盘", row.get("close", 0))),
            "volume": _safe_float(row.get("成交量", row.get("volume", 0))),
        }
        for extra_col, extra_key in [("成交额", "amount"), ("涨跌幅", "change_pct")]:
            if extra_col in row.index:
                item[extra_key] = _safe_float(row.get(extra_col, 0))
        items.append(item)
    return items


def _calculate_summary(df) -> Dict[str, Any]:
    summary = {"total_bars": len(df)}
    try:
        for high_col in ["最高", "high"]:
            if high_col in df.columns:
                summary["intraday_high"] = round(_safe_float(df[high_col].max()), 4)
                break
        for low_col in ["最低", "low"]:
            if low_col in df.columns:
                summary["intraday_low"] = round(_safe_float(df[low_col].min()), 4)
                break
        close_col = "收盘" if "收盘" in df.columns else "close"
        if close_col in df.columns:
            summary["latest_close"] = round(_safe_float(df[close_col].iloc[-1]), 4)
            summary["first_open"] = round(_safe_float(df[close_col].iloc[0]), 4)
        vol_col = "成交量" if "成交量" in df.columns else "volume"
        if vol_col in df.columns:
            volumes = df[vol_col].apply(lambda x: _safe_float(x)).tolist()
            summary["total_volume"] = round(sum(volumes), 2)
            mid = len(volumes) // 2
            if mid > 0:
                first_half_avg = sum(volumes[:mid]) / mid
                second_half_avg = sum(volumes[mid:]) / max(len(volumes[mid:]), 1)
                vol_ratio = round(second_half_avg / max(first_half_avg, 0.01), 2)
                if vol_ratio > 1.5:
                    summary["volume_trend"] = "显著放量"
                elif vol_ratio > 1.1:
                    summary["volume_trend"] = "温和放量"
                elif vol_ratio > 0.9:
                    summary["volume_trend"] = "量能平稳"
                elif vol_ratio > 0.6:
                    summary["volume_trend"] = "温和缩量"
                else:
                    summary["volume_trend"] = "显著缩量"
                summary["volume_ratio"] = vol_ratio
    except Exception as e:
        logger.warning(f"计算分时汇总失败: {e}")
    return summary


def _analyze_intraday(period: str, summary: Dict) -> str:
    signals = []
    high = summary.get("intraday_high", 0)
    low = summary.get("intraday_low", 0)
    latest = summary.get("latest_close", 0)
    first_open = summary.get("first_open", 0)
    vol_trend = summary.get("volume_trend", "数据不足")
    total_bars = summary.get("total_bars", 0)
    if high > 0 and low > 0:
        amplitude = round((high - low) / low * 100, 2)
        signals.append(f"日内振幅{amplitude:.2f}%")
    if latest > 0 and first_open > 0:
        intraday_chg = round((latest - first_open) / first_open * 100, 2)
        if intraday_chg > 1:
            signals.append(f"日内上涨{intraday_chg:.2f}%，整体走强")
        elif intraday_chg > 0:
            signals.append(f"日内微涨{intraday_chg:.2f}%，震荡偏强")
        elif intraday_chg > -1:
            signals.append(f"日内微跌{intraday_chg:.2f}%，震荡偏弱")
        else:
            signals.append(f"日内下跌{intraday_chg:.2f}%，整体走弱")
    if vol_trend != "数据不足":
        signals.append(f"量能{vol_trend}")
    signals.append(f"共{total_bars}根{period}分钟K线")
    return "，".join(signals)


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")
    period = str(params.get("period", "5"))

    if not ts_code:
        return {"error": "缺少股票代码参数 ts_code", "for_llm": {"error": "缺少股票代码参数 ts_code"}}

    code = ts_code.split(".")[0] if "." in ts_code else ts_code

    try:
        df = _get_minute_kline(
            code,
            period=period,
            start_date=params.get("start_date", ""),
            end_date=params.get("end_date", ""),
        )

        if df is None or df.empty:
            err = f"无法获取 {ts_code} 的{period}分钟K线数据"
            return {"error": err, "for_llm": {"error": err}}

        items = _build_items(df)
        columns = [
            {"key": "time", "label": "时间"},
            {"key": "open", "label": "开盘"},
            {"key": "high", "label": "最高"},
            {"key": "low", "label": "最低"},
            {"key": "close", "label": "收盘"},
            {"key": "volume", "label": "成交量"},
        ]
        summary = _calculate_summary(df)
        analysis = _analyze_intraday(period, summary)

        result = {
            "ts_code": ts_code,
            "title": f"{ts_code} {period}分钟K线",
            "items": items,
            "columns": columns,
            "summary": summary,
            "analysis": analysis,
            "data_source": "akshare/hist_min_em",
        }
        result["for_llm"] = {
            "ts_code": ts_code,
            "period": period,
            "total_bars": summary.get("total_bars", 0),
            "intraday_high": summary.get("intraday_high"),
            "intraday_low": summary.get("intraday_low"),
            "volume_trend": summary.get("volume_trend"),
            "analysis": analysis,
        }
        return result

    except Exception as e:
        logger.error(f"分钟K线获取失败: {e}", exc_info=True)
        err = f"分钟K线获取失败: {str(e)}"
        return {"error": err, "for_llm": {"error": err}}


if __name__ == "__main__":
    import sys, json as _json
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--ts_code", default="")
        parser.add_argument("--period", default="5")
        parser.add_argument("--start_date", default="")
        parser.add_argument("--end_date", default="")
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())
    result = main(params)
    print(_json.dumps(result, ensure_ascii=False, default=str))
