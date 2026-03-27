"""
Intraday Tick Skill
日内逐笔成交数据技能
获取个股日内逐笔成交数据，追踪大单动向
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


def _get_intraday_tick(code: str):
    """获取逐笔成交数据"""
    try:
        import akshare as ak
        df = ak.stock_intraday_em(symbol=code)
        return df
    except Exception as e:
        logger.warning(f"akshare逐笔数据获取失败: {e}")
        return None


def _build_items(df) -> List[Dict]:
    items = []
    display_df = df.head(100)
    for _, row in display_df.iterrows():
        item = {}
        for col in df.columns:
            val = row[col]
            try:
                fval = float(val)
                if not (math.isnan(fval) or math.isinf(fval)):
                    item[col] = fval
                else:
                    item[col] = str(val)
            except (ValueError, TypeError):
                item[col] = str(val) if val is not None else ""
        items.append(item)
    return items


def _analyze_ticks(df) -> Dict[str, Any]:
    summary = {"total_ticks": len(df)}
    try:
        # 尝试识别买卖方向列
        direction_col = None
        for col in ["方向", "买卖", "type", "direction"]:
            if col in df.columns:
                direction_col = col
                break
        if direction_col:
            buy_ticks = df[df[direction_col].astype(str).str.contains("买|B|buy", case=False, na=False)]
            sell_ticks = df[df[direction_col].astype(str).str.contains("卖|S|sell", case=False, na=False)]
            summary["buy_count"] = len(buy_ticks)
            summary["sell_count"] = len(sell_ticks)
        # 成交量列
        vol_col = None
        for col in ["成交量", "手数", "volume", "vol"]:
            if col in df.columns:
                vol_col = col
                break
        if vol_col:
            volumes = df[vol_col].apply(lambda x: _safe_float(x))
            summary["total_volume"] = round(float(volumes.sum()), 0)
            # 大单统计（超过100手）
            large_orders = volumes[volumes >= 100]
            summary["large_order_count"] = len(large_orders)
            summary["large_order_volume"] = round(float(large_orders.sum()), 0)
    except Exception as e:
        logger.warning(f"逐笔分析失败: {e}")
    return summary


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")

    if not ts_code:
        return {"error": "缺少股票代码参数 ts_code", "for_llm": {"error": "缺少股票代码参数 ts_code"}}

    code = ts_code.split(".")[0] if "." in ts_code else ts_code

    try:
        df = _get_intraday_tick(code)

        if df is None or df.empty:
            err = f"无法获取 {ts_code} 的逐笔成交数据，可能非交易时间"
            return {"error": err, "for_llm": {"error": err}}

        items = _build_items(df)
        columns = [{"key": col, "label": col} for col in df.columns]
        summary = _analyze_ticks(df)

        result = {
            "ts_code": ts_code,
            "title": f"{ts_code} 日内逐笔成交",
            "items": items,
            "columns": columns,
            "summary": summary,
            "data_source": "akshare/intraday_em",
        }
        result["for_llm"] = {
            "ts_code": ts_code,
            "total_ticks": summary.get("total_ticks", 0),
            "buy_count": summary.get("buy_count"),
            "sell_count": summary.get("sell_count"),
            "large_order_count": summary.get("large_order_count"),
            "large_order_volume": summary.get("large_order_volume"),
        }
        return result

    except Exception as e:
        logger.error(f"逐笔数据获取失败: {e}", exc_info=True)
        err = f"逐笔数据获取失败: {str(e)}"
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
