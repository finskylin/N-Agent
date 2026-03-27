"""
Peer Comparison Skill
同行业对比分析技能
获取同行业/同板块股票的关键财务指标对比
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


def _get_peer_comparison(code: str) -> List[Dict]:
    """获取同行业股票对比数据"""
    # Build the symbol in the format akshare expects (e.g. SH600519)
    if code.startswith("6"):
        symbol = f"SH{code}"
    elif code.startswith(("0", "3")):
        symbol = f"SZ{code}"
    else:
        symbol = code

    try:
        import akshare as ak
        df = ak.stock_zh_growth_comparison_em(symbol=symbol)
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
            logger.info(f"get_peer_comparison succeeded: {code}, rows={len(records)}")
            return records
    except Exception as e:
        logger.warning(f"get_peer_comparison failed: {code}: {e}")

    # Fallback: try stock_compare_industry_em
    try:
        import akshare as ak
        df = ak.stock_compare_industry_em(symbol=symbol)
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
            logger.info(f"get_peer_comparison fallback succeeded: {code}, rows={len(records)}")
            return records
    except Exception as e:
        logger.warning(f"get_peer_comparison fallback failed: {code}: {e}")

    return []


def _analyze_peer(peers: List[Dict], code: str) -> Dict[str, Any]:
    """分析同业对比结果"""
    summary = {"peer_count": len(peers)}
    signals = []

    if not peers:
        return summary

    # Find the target stock row
    target_row = None
    for row in peers:
        for col, val in row.items():
            if str(val) == code or str(val).endswith(code):
                target_row = row
                break
        if target_row:
            break

    # PE ranking analysis
    pe_col = None
    for col in (peers[0].keys() if peers else []):
        if "市盈" in str(col) or "PE" in str(col):
            pe_col = col
            break
    if pe_col:
        pe_vals = [_safe_float(r.get(pe_col, 0)) for r in peers if _safe_float(r.get(pe_col, 0)) > 0]
        if pe_vals:
            avg_pe = sum(pe_vals) / len(pe_vals)
            summary["avg_peer_pe"] = round(avg_pe, 2)
            if target_row:
                target_pe = _safe_float(target_row.get(pe_col, 0))
                if target_pe > 0:
                    summary["target_pe"] = target_pe
                    if target_pe < avg_pe * 0.8:
                        signals.append(f"PE({target_pe:.1f}x)低于行业均值({avg_pe:.1f}x)，估值具备优势")
                    elif target_pe > avg_pe * 1.2:
                        signals.append(f"PE({target_pe:.1f}x)高于行业均值({avg_pe:.1f}x)，存在估值溢价")
                    else:
                        signals.append(f"PE({target_pe:.1f}x)与行业均值({avg_pe:.1f}x)接近")

    if not signals:
        signals.append(f"获取到{len(peers)}家同行对比数据")

    summary["signals"] = signals
    return summary


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")

    if not ts_code:
        return {"error": "缺少股票代码参数 ts_code", "for_llm": {"error": "缺少股票代码参数 ts_code"}}

    code = ts_code.split('.')[0] if '.' in ts_code else ts_code

    try:
        peers = _get_peer_comparison(code)

        if not peers:
            err = "无法获取同行业对比数据"
            return {"error": err, "for_llm": {"error": err}}

        analysis = _analyze_peer(peers, code)
        signals = analysis.get("signals", [])
        signal_text = "；".join(signals) if signals else "暂无分析"

        columns = [{"key": k, "label": k} for k in (peers[0].keys() if peers else [])]

        result = {
            "ts_code": ts_code,
            "title": f"同行业对比 - {ts_code}",
            "items": peers[:20],
            "columns": columns,
            "summary": analysis,
            "analysis": signal_text,
            "data_source": "akshare/growth_comparison_em",
        }
        result["for_llm"] = {
            "ts_code": ts_code,
            "peer_count": analysis.get("peer_count", 0),
            "avg_peer_pe": analysis.get("avg_peer_pe", 0),
            "target_pe": analysis.get("target_pe", 0),
            "signals": signals,
            "analysis": signal_text,
        }
        return result

    except Exception as e:
        logger.error(f"同行业对比分析失败: {e}", exc_info=True)
        err = f"同行业对比分析失败: {str(e)}"
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
