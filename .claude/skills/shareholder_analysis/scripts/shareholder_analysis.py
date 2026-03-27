"""
Shareholder Analysis Skill
股东人数与十大股东分析技能
获取股东人数变化趋势和十大流通股东数据
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


def _get_shareholder_count(code: str) -> List[Dict]:
    """获取股东人数变化"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_gdhs_detail_em(symbol=code)
        if df is not None and not df.empty:
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
            logger.info(f"get_shareholder_count succeeded: {code}, rows={len(records)}")
            return records
    except Exception as e:
        logger.warning(f"get_shareholder_count failed: {code}: {e}")
    return []


def _get_top10_shareholders(code: str) -> List[Dict]:
    """获取十大流通股东"""
    # Build the symbol in the format akshare expects (e.g. sh600519)
    if code.startswith("6"):
        symbol = f"sh{code}"
    elif code.startswith(("0", "3")):
        symbol = f"sz{code}"
    else:
        symbol = code
    try:
        import akshare as ak
        df = ak.stock_gdfx_top_10_em(symbol=symbol)
        if df is not None and not df.empty:
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
            logger.info(f"get_top10_shareholders succeeded: {code}, rows={len(records)}")
            return records
    except Exception as e:
        logger.warning(f"get_top10_shareholders failed: {code}: {e}")
    return []


def _analyze_shareholders(count_data: List[Dict], top10: List[Dict]) -> Dict[str, Any]:
    """分析股东结构"""
    summary = {
        "count_records": len(count_data),
        "top10_count": len(top10),
    }
    signals = []

    # 股东人数趋势分析（取最近两期对比）
    if len(count_data) >= 2:
        # Try to find shareholder count column
        count_col = None
        for col in (count_data[0] if count_data else {}).keys():
            if "股东" in str(col) and ("人数" in str(col) or "户数" in str(col)):
                count_col = col
                break
        if count_col:
            latest = _safe_float(count_data[0].get(count_col, 0))
            prev = _safe_float(count_data[1].get(count_col, 0))
            if latest > 0 and prev > 0:
                change_pct = (latest - prev) / prev * 100
                summary["latest_holder_count"] = latest
                summary["holder_count_change_pct"] = round(change_pct, 2)
                if change_pct < -5:
                    signals.append("股东人数明显减少，筹码趋于集中，可能是积极信号")
                elif change_pct > 5:
                    signals.append("股东人数明显增加，筹码趋于分散，需关注获利了结风险")
                else:
                    signals.append("股东人数变化不大，持仓结构相对稳定")

    # 十大股东集中度
    if top10:
        # Try to find holding ratio column
        ratio_col = None
        for col in (top10[0] if top10 else {}).keys():
            if "比例" in str(col) or "占比" in str(col) or "持股比例" in str(col):
                ratio_col = col
                break
        if ratio_col:
            total_ratio = sum(_safe_float(r.get(ratio_col, 0)) for r in top10)
            summary["top10_holding_ratio"] = round(total_ratio, 2)
            if total_ratio > 60:
                signals.append(f"前十大股东持股集中度高（{total_ratio:.1f}%），股权结构稳定")
            elif total_ratio > 40:
                signals.append(f"前十大股东持股集中度适中（{total_ratio:.1f}%）")
            else:
                signals.append(f"前十大股东持股集中度偏低（{total_ratio:.1f}%），股权较分散")

    summary["signals"] = signals
    return summary


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")

    if not ts_code:
        return {"error": "缺少股票代码参数 ts_code", "for_llm": {"error": "缺少股票代码参数 ts_code"}}

    code = ts_code.split('.')[0] if '.' in ts_code else ts_code

    try:
        count_data = _get_shareholder_count(code)
        top10 = _get_top10_shareholders(code)

        if not count_data and not top10:
            err = "无法获取股东数据"
            return {"error": err, "for_llm": {"error": err}}

        analysis = _analyze_shareholders(count_data, top10)
        signals = analysis.get("signals", [])
        signal_text = "；".join(signals) if signals else "暂无信号"

        result = {
            "ts_code": ts_code,
            "title": f"股东分析 - {ts_code}",
            "shareholder_count_data": count_data[:10],
            "top10_shareholders": top10,
            "items": top10[:10] if top10 else count_data[:10],
            "columns": [{"key": k, "label": k} for k in (top10[0].keys() if top10 else (count_data[0].keys() if count_data else []))],
            "summary": analysis,
            "analysis": signal_text,
        }
        result["for_llm"] = {
            "ts_code": ts_code,
            "count_records": analysis.get("count_records", 0),
            "top10_count": analysis.get("top10_count", 0),
            "latest_holder_count": analysis.get("latest_holder_count", 0),
            "holder_count_change_pct": analysis.get("holder_count_change_pct", 0),
            "top10_holding_ratio": analysis.get("top10_holding_ratio", 0),
            "signals": signals,
            "analysis": signal_text,
        }
        return result

    except Exception as e:
        logger.error(f"股东分析失败: {e}", exc_info=True)
        err = f"股东分析失败: {str(e)}"
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
