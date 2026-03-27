"""
Industry Comparison Skill
行业板块对比分析技能
获取行业板块涨跌幅、估值对比和资金流向
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


def _df_to_records(df) -> List[Dict]:
    records = []
    for _, row in df.head(50).iterrows():
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
    return records


def _fetch_board(market: str) -> List[Dict]:
    """从 akshare 获取行业/概念板块数据，失败抛异常"""
    import akshare as ak
    df = ak.stock_board_industry_name_em() if market == "sw" else ak.stock_board_concept_name_em()
    if df is None or df.empty:
        raise ValueError(f"Empty dataframe for market={market}")
    return _df_to_records(df)


def _fetch_board_fallback() -> List[Dict]:
    """fallback：同花顺行业板块（含涨跌幅/净流入，与东财同维度）"""
    import akshare as ak
    df = ak.stock_board_industry_summary_ths()
    if df is None or df.empty:
        raise ValueError("Empty dataframe from stock_board_industry_summary_ths")
    # 统一列名与东财对齐
    df = df.rename(columns={"板块": "板块名称"})
    return _df_to_records(df)


def _fetch_board_fallback2() -> List[Dict]:
    """fallback2：新浪行业板块"""
    import akshare as ak
    df = ak.stock_sector_spot(indicator="新浪行业")
    if df is None or df.empty:
        raise ValueError("Empty dataframe from stock_sector_spot sina")
    df = df.rename(columns={"板块": "板块名称", "涨跌幅": "涨跌幅(%)"})
    return _df_to_records(df)


def _get_industry_board(market: str = "sw") -> List[Dict]:
    """获取行业板块行情：东财 → 同花顺 → 新浪"""
    for name, fn in [
        ("eastmoney", lambda: _fetch_board(market)),
        ("ths",       _fetch_board_fallback),
        ("sina",      _fetch_board_fallback2),
    ]:
        try:
            records = fn()
            logger.info(f"get_industry_board succeeded via {name}: rows={len(records)}")
            return records
        except Exception as e:
            logger.warning(f"get_industry_board {name} failed: {e}")
    logger.error("get_industry_board all sources failed")
    return []


def _get_industry_detail(industry_name: str) -> List[Dict]:
    """获取特定行业板块个股"""
    try:
        import akshare as ak
        df = ak.stock_board_industry_cons_em(symbol=industry_name)
        if df is not None and not df.empty:
            return _df_to_records(df)
    except Exception as e:
        logger.warning(f"get_industry_detail failed: {industry_name}: {e}")
    return []


def _analyze_industry(boards: List[Dict], industry_name: str) -> Dict[str, Any]:
    """分析行业对比"""
    summary = {"board_count": len(boards)}
    signals = []

    if not boards:
        return summary

    # Find change pct column
    change_col = None
    for col in (boards[0].keys() if boards else []):
        if "涨跌幅" in str(col) or "涨跌" in str(col):
            change_col = col
            break

    if change_col:
        change_vals = [(r.get("板块名称", r.get("名称", "")), _safe_float(r.get(change_col, 0))) for r in boards]
        change_vals.sort(key=lambda x: x[1], reverse=True)
        if change_vals:
            top3 = change_vals[:3]
            bottom3 = change_vals[-3:]
            top_names = "、".join([f"{n}({v:+.2f}%)" for n, v in top3 if n])
            bot_names = "、".join([f"{n}({v:+.2f}%)" for n, v in bottom3 if n])
            if top_names:
                signals.append(f"涨幅前三：{top_names}")
            if bot_names:
                signals.append(f"跌幅前三：{bot_names}")

        # Find target industry if specified
        if industry_name:
            for name, change in change_vals:
                if industry_name in str(name):
                    rank = next((i + 1 for i, (n, _) in enumerate(change_vals) if industry_name in str(n)), 0)
                    signals.append(f"{industry_name}行业涨跌幅{change:+.2f}%，排名第{rank}/{len(change_vals)}")
                    summary["target_change"] = change
                    summary["target_rank"] = rank
                    break

    if not signals:
        signals.append(f"获取到{len(boards)}个行业板块数据")

    summary["signals"] = signals
    return summary


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")
    industry_name = params.get("industry_name", "")
    market = params.get("market", "sw")

    try:
        boards = _get_industry_board(market=market)

        detail = []
        if industry_name:
            detail = _get_industry_detail(industry_name)

        if not boards:
            err = "无法获取行业板块数据"
            return {"error": err, "for_llm": {"error": err}}

        analysis = _analyze_industry(boards, industry_name)
        signals = analysis.get("signals", [])
        signal_text = "；".join(signals) if signals else "暂无分析"

        items = detail if detail else boards
        columns = [{"key": k, "label": k} for k in (items[0].keys() if items else [])]

        result = {
            "ts_code": ts_code,
            "title": f"行业对比 - {industry_name or '全行业'}",
            "items": items[:20],
            "columns": columns,
            "industry_boards": boards[:20],
            "industry_detail": detail[:20],
            "summary": analysis,
            "analysis": signal_text,
            "data_source": "akshare/board_industry_em",
        }
        result["for_llm"] = {
            "ts_code": ts_code or industry_name or "市场",
            "board_count": analysis.get("board_count", 0),
            "target_change": analysis.get("target_change"),
            "target_rank": analysis.get("target_rank"),
            "signals": signals,
            "analysis": signal_text,
        }
        return result

    except Exception as e:
        logger.error(f"行业对比分析失败: {e}", exc_info=True)
        err = f"行业对比分析失败: {str(e)}"
        return {"error": err, "for_llm": {"error": err}}


if __name__ == "__main__":
    import sys, json as _json
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--ts_code", default="")
        parser.add_argument("--industry_name", default="")
        parser.add_argument("--market", default="sw")
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())
    result = main(params)
    print(_json.dumps(result, ensure_ascii=False, default=str))
