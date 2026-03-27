"""
Performance Forecast Skill
业绩预告分析技能
获取上市公司业绩预告、业绩快报和业绩预测数据
"""
import os
import math
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List

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


def _get_performance_forecast(code: str) -> List[Dict]:
    """获取业绩预告"""
    today = datetime.now()
    # Try current quarter and surrounding dates
    dates_to_try = []
    for i in range(0, 180, 30):
        d = today - timedelta(days=i)
        dates_to_try.append(d.strftime('%Y%m%d'))

    for try_date in dates_to_try[:4]:
        try:
            import akshare as ak
            df = ak.stock_yjyg_em(date=try_date)
            if df is not None and not df.empty:
                if code:
                    for col in df.columns:
                        if '代码' in str(col):
                            filtered = df[df[col].astype(str).str.contains(code, na=False)]
                            if not filtered.empty:
                                df = filtered
                            break
                records = []
                for _, row in df.iterrows():
                    record = {}
                    for col in df.columns:
                        val = row[col]
                        if val is None:
                            record[col] = ""
                        else:
                            try:
                                fval = float(val)
                                record[col] = fval if not (math.isnan(fval) or math.isinf(fval)) else ""
                            except (ValueError, TypeError):
                                record[col] = str(val)
                    records.append(record)
                if records:
                    logger.info(f"get_performance_forecast succeeded: {code}, date={try_date}, rows={len(records)}")
                    return records
        except Exception as e:
            logger.warning(f"get_performance_forecast failed date={try_date}: {e}")
            continue
    return []


def _get_performance_express(code: str) -> List[Dict]:
    """获取业绩快报"""
    today = datetime.now()
    dates_to_try = [today.strftime('%Y%m%d')]
    for i in range(1, 6):
        d = today - timedelta(days=i * 30)
        dates_to_try.append(d.strftime('%Y%m%d'))

    for try_date in dates_to_try:
        try:
            import akshare as ak
            df = ak.stock_yjkb_em(date=try_date)
            if df is not None and not df.empty:
                if code:
                    for col in df.columns:
                        if '代码' in str(col):
                            filtered = df[df[col].astype(str).str.contains(code, na=False)]
                            if not filtered.empty:
                                df = filtered
                            break
                records = []
                for _, row in df.iterrows():
                    record = {}
                    for col in df.columns:
                        val = row[col]
                        if val is None:
                            record[col] = ""
                        else:
                            try:
                                fval = float(val)
                                record[col] = fval if not (math.isnan(fval) or math.isinf(fval)) else ""
                            except (ValueError, TypeError):
                                record[col] = str(val)
                    records.append(record)
                if records:
                    logger.info(f"get_performance_express succeeded: {code}, date={try_date}, rows={len(records)}")
                    return records
        except Exception as e:
            logger.warning(f"get_performance_express failed date={try_date}: {e}")
            continue
    return []


def _analyze_performance(forecast: List[Dict], express: List[Dict], code: str) -> Dict[str, Any]:
    """分析业绩数据"""
    summary = {
        "forecast_count": len(forecast),
        "express_count": len(express),
    }
    signals = []

    data_to_analyze = forecast if forecast else express
    if data_to_analyze:
        row = data_to_analyze[0]
        # Look for profit change ratio
        for col, val in row.items():
            if "增长" in str(col) or "变动" in str(col) or "同比" in str(col):
                change = _safe_float(val)
                if change != 0:
                    summary["profit_change_pct"] = change
                    if change > 50:
                        signals.append(f"预计利润增长{change:.0f}%，业绩高增长")
                    elif change > 20:
                        signals.append(f"预计利润增长{change:.0f}%，业绩稳步增长")
                    elif change > 0:
                        signals.append(f"预计利润增长{change:.0f}%，业绩小幅增长")
                    elif change > -20:
                        signals.append(f"预计利润变动{change:.0f}%，业绩小幅下滑")
                    else:
                        signals.append(f"预计利润变动{change:.0f}%，业绩压力较大")
                    break

        # Look for forecast type
        for col, val in row.items():
            if "类型" in str(col) or "预告类型" in str(col):
                summary["forecast_type"] = str(val)
                if "预增" in str(val):
                    signals.append("业绩预增，积极信号")
                elif "预减" in str(val):
                    signals.append("业绩预减，需要关注")
                elif "扭亏" in str(val):
                    signals.append("扭亏为盈，基本面改善")
                elif "续亏" in str(val):
                    signals.append("续亏，持续关注基本面")
                break

    if not signals:
        if forecast or express:
            signals.append(f"获取到{len(forecast)}条业绩预告和{len(express)}条业绩快报")
        else:
            signals.append("暂无业绩预告/快报数据")

    summary["signals"] = signals
    return summary


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")
    code = ts_code.split('.')[0] if '.' in ts_code else ts_code

    try:
        forecast = _get_performance_forecast(code)
        express = _get_performance_express(code)

        if not forecast and not express:
            err = "无法获取业绩预告/快报数据"
            return {"error": err, "for_llm": {"error": err}}

        analysis = _analyze_performance(forecast, express, code)
        signals = analysis.get("signals", [])
        signal_text = "；".join(signals) if signals else "暂无分析"

        items = forecast if forecast else express
        columns = [{"key": k, "label": k} for k in (items[0].keys() if items else [])]

        result = {
            "ts_code": ts_code,
            "title": f"业绩预告分析 - {ts_code}" if ts_code else "业绩预告汇总",
            "items": items[:10],
            "columns": columns,
            "performance_forecast": forecast[:10],
            "performance_express": express[:10],
            "summary": analysis,
            "analysis": signal_text,
            "data_source": "akshare/yjyg_em+yjkb_em",
        }
        result["for_llm"] = {
            "ts_code": ts_code or "市场",
            "forecast_count": analysis.get("forecast_count", 0),
            "express_count": analysis.get("express_count", 0),
            "profit_change_pct": analysis.get("profit_change_pct"),
            "forecast_type": analysis.get("forecast_type", ""),
            "signals": signals,
            "analysis": signal_text,
        }
        return result

    except Exception as e:
        logger.error(f"业绩预告分析失败: {e}", exc_info=True)
        err = f"业绩预告分析失败: {str(e)}"
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
