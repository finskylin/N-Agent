"""
Valuation Analysis Skill
估值分析技能
获取股票 PE/PB/PS 等估值指标，与历史分位和行业对比
"""
import os
import math
import logging
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


def _get_valuation_indicators(code: str) -> Dict:
    """获取估值指标（市盈率、市净率、市销率等）"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            row = df[df['代码'] == code]
            if not row.empty:
                r = row.iloc[0]
                result = {}
                for col in df.columns:
                    val = r.get(col)
                    if val is None:
                        result[col] = ""
                    else:
                        try:
                            fval = float(val)
                            result[col] = fval if not (math.isnan(fval) or math.isinf(fval)) else ""
                        except (ValueError, TypeError):
                            result[col] = str(val)
                logger.info(f"get_valuation_indicators succeeded: {code}")
                return result
    except Exception as e:
        logger.warning(f"get_valuation_indicators spot failed: {code}: {e}")

    return {}


def _get_pe_history(code: str) -> List[Dict]:
    """获取历史PE数据"""
    # Build the symbol in the format akshare expects
    if code.startswith("6"):
        symbol = f"sh{code}"
    elif code.startswith(("0", "3")):
        symbol = f"sz{code}"
    else:
        symbol = code

    try:
        import akshare as ak
        df = ak.stock_a_pe(symbol=symbol)
        if df is not None and not df.empty:
            records = []
            for _, row in df.tail(60).iterrows():
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
            logger.info(f"get_pe_history succeeded: {code}, rows={len(records)}")
            return records
    except Exception as e:
        logger.warning(f"get_pe_history failed: {code}: {e}")
    return []


def _analyze_valuation(indicators: Dict, pe_history: List[Dict]) -> Dict[str, Any]:
    """估值分析"""
    summary = {}
    signals = []

    # Extract key metrics from spot data
    pe = 0.0
    pb = 0.0
    current_price = 0.0
    for col, val in indicators.items():
        if "市盈率" in str(col) and "动态" in str(col):
            pe = _safe_float(val)
        elif "市净率" in str(col):
            pb = _safe_float(val)
        elif "最新价" in str(col):
            current_price = _safe_float(val)

    if pe > 0:
        summary["pe"] = round(pe, 2)
        if pe < 15:
            signals.append(f"PE({pe:.1f}x)偏低，估值具有吸引力")
        elif pe < 30:
            signals.append(f"PE({pe:.1f}x)适中，估值合理")
        elif pe < 50:
            signals.append(f"PE({pe:.1f}x)偏高，需关注业绩支撑")
        else:
            signals.append(f"PE({pe:.1f}x)较高，估值存在压力")

    if pb > 0:
        summary["pb"] = round(pb, 2)
        if pb < 1:
            signals.append(f"PB({pb:.2f}x)破净，具有安全边际")
        elif pb < 2:
            signals.append(f"PB({pb:.2f}x)较低")
        elif pb > 5:
            signals.append(f"PB({pb:.2f}x)较高")

    if current_price > 0:
        summary["current_price"] = current_price

    # PE historical percentile
    if pe_history and pe > 0:
        pe_col = None
        for col in (pe_history[0].keys() if pe_history else []):
            if "pe" in str(col).lower() or "市盈" in str(col):
                pe_col = col
                break
        if pe_col:
            hist_pe_vals = [_safe_float(r.get(pe_col, 0)) for r in pe_history if _safe_float(r.get(pe_col, 0)) > 0]
            if hist_pe_vals:
                count_below = sum(1 for v in hist_pe_vals if v <= pe)
                pct = count_below / len(hist_pe_vals) * 100
                summary["pe_percentile"] = round(pct, 1)
                if pct <= 20:
                    signals.append(f"PE历史分位{pct:.0f}%，处于历史低位区间")
                elif pct <= 50:
                    signals.append(f"PE历史分位{pct:.0f}%，估值偏低")
                elif pct <= 80:
                    signals.append(f"PE历史分位{pct:.0f}%，估值偏高")
                else:
                    signals.append(f"PE历史分位{pct:.0f}%，估值处于历史高位")

    if not signals:
        signals.append("估值数据获取不完整，请参考基本面综合判断")

    summary["signals"] = signals
    return summary


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")

    if not ts_code:
        return {"error": "缺少股票代码参数 ts_code", "for_llm": {"error": "缺少股票代码参数 ts_code"}}

    code = ts_code.split('.')[0] if '.' in ts_code else ts_code

    try:
        indicators = _get_valuation_indicators(code)
        pe_history = _get_pe_history(code)

        if not indicators and not pe_history:
            err = "无法获取估值数据"
            return {"error": err, "for_llm": {"error": err}}

        analysis = _analyze_valuation(indicators, pe_history)
        signals = analysis.get("signals", [])
        signal_text = "；".join(signals) if signals else "暂无分析"

        # Build items from pe_history or indicators
        items = pe_history[-20:] if pe_history else ([indicators] if indicators else [])
        columns = [{"key": k, "label": k} for k in (items[0].keys() if items else [])]

        result = {
            "ts_code": ts_code,
            "title": f"估值分析 - {ts_code}",
            "items": items[:20],
            "columns": columns,
            "current_indicators": indicators,
            "pe_history": pe_history[-20:],
            "summary": analysis,
            "analysis": signal_text,
            "data_source": "akshare/spot_em+pe_history",
        }
        result["for_llm"] = {
            "ts_code": ts_code,
            "pe": analysis.get("pe", 0),
            "pb": analysis.get("pb", 0),
            "current_price": analysis.get("current_price", 0),
            "pe_percentile": analysis.get("pe_percentile"),
            "signals": signals,
            "analysis": signal_text,
        }
        return result

    except Exception as e:
        logger.error(f"估值分析失败: {e}", exc_info=True)
        err = f"估值分析失败: {str(e)}"
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
