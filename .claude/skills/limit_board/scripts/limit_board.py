"""
Limit Board Skill
涨跌停板池技能
获取涨跌停/强势/炸板/次新股池数据，分析板面特征
"""
import os
import math
import logging
from datetime import datetime
from collections import Counter
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


def _is_numeric(val) -> bool:
    if val is None:
        return False
    try:
        float(val)
        return True
    except (ValueError, TypeError):
        return False


def _get_limit_board(date: str = "", board_type: str = "涨停") -> Optional[object]:
    api_map = {
        "涨停": "stock_zt_pool_em",
        "跌停": "stock_dt_pool_em",
        "强势": "stock_zt_pool_strong_em",
        "次新": "stock_zt_pool_sub_new_em",
        "炸板": "stock_zt_pool_zbgc_em",
        "昨日涨停": "stock_zt_pool_previous_em",
    }
    func_name = api_map.get(board_type, "stock_zt_pool_em")
    if not date:
        date = datetime.now().strftime('%Y%m%d')
    try:
        import akshare as ak
        func = getattr(ak, func_name, None)
        if func is None:
            logger.warning(f"akshare API {func_name} not found")
            return None
        df = func(date=date)
        if df is not None and not df.empty:
            logger.info(f"get_limit_board succeeded: {board_type} {date}, rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"get_limit_board failed: {board_type} {date}: {e}")
    return None


def _build_items(df) -> List[Dict]:
    items = []
    for _, row in df.head(30).iterrows():
        item = {}
        for col in df.columns:
            val = row[col]
            item[col] = _safe_float(val) if _is_numeric(val) else (str(val) if val is not None else "")
        items.append(item)
    return items


def _calculate_summary(df, board_type: str) -> Dict[str, Any]:
    summary = {"total_count": len(df), "board_type": board_type}
    try:
        for col in ["连板数", "连续涨停天数"]:
            if col in df.columns:
                consec_values = df[col].apply(lambda x: _safe_float(x))
                summary["max_consecutive"] = int(consec_values.max()) if len(consec_values) > 0 else 0
                summary["consecutive_2plus"] = int((consec_values >= 2).sum())
                break
        for col in ["所属行业", "行业"]:
            if col in df.columns:
                industries = df[col].dropna().tolist()
                industry_counts = Counter(industries)
                summary["hot_industries"] = [name for name, _ in industry_counts.most_common(5)]
                summary["industry_distribution"] = dict(industry_counts.most_common(10))
                break
    except Exception as e:
        logger.warning(f"计算涨跌停汇总失败: {e}")
    return summary


def _analyze_board(board_type: str, summary: Dict, display_date: str) -> str:
    signals = []
    total = summary.get("total_count", 0)
    signals.append(f"{display_date} {board_type}池共{total}只股票")
    max_consec = summary.get("max_consecutive", 0)
    consec_2plus = summary.get("consecutive_2plus", 0)
    if max_consec >= 5:
        signals.append(f"最高{max_consec}连板，市场情绪活跃")
    elif max_consec >= 3:
        signals.append(f"最高{max_consec}连板")
    if consec_2plus > 0:
        signals.append(f"2连板以上{consec_2plus}只")
    hot_industries = summary.get("hot_industries", [])
    if hot_industries:
        signals.append(f"热门板块: {'、'.join(hot_industries[:3])}")
    if board_type == "涨停":
        if total > 80:
            signals.append("涨停家数较多，市场做多情绪强烈")
        elif total > 50:
            signals.append("涨停家数适中，市场活跃度较好")
        elif total > 20:
            signals.append("涨停家数一般，市场分化明显")
        else:
            signals.append("涨停家数偏少，市场情绪低迷")
    return "；".join(signals)


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    board_type = params.get("board_type", "涨停")
    date = params.get("date", "")

    try:
        df = _get_limit_board(date=date, board_type=board_type)
        if df is None or df.empty:
            err = f"无法获取{board_type}池数据，可能非交易时间或数据源不可用"
            return {"error": err, "for_llm": {"error": err}}

        items = _build_items(df)
        columns = [{"key": col, "label": col} for col in df.columns]
        display_date = date if date else datetime.now().strftime("%Y-%m-%d")
        summary = _calculate_summary(df, board_type)
        analysis = _analyze_board(board_type, summary, display_date)

        result = {
            "title": f"{board_type}池 ({display_date})",
            "items": items,
            "columns": columns,
            "summary": summary,
            "analysis": analysis,
            "data_source": "akshare/zt_pool_em",
        }
        result["for_llm"] = {
            "board_type": board_type,
            "date": display_date,
            "total_count": summary.get("total_count", 0),
            "max_consecutive": summary.get("max_consecutive", 0),
            "hot_industries": summary.get("hot_industries", []),
            "analysis": analysis,
        }
        return result

    except Exception as e:
        logger.error(f"涨跌停板池获取失败: {e}", exc_info=True)
        err = f"涨跌停板池获取失败: {str(e)}"
        return {"error": err, "for_llm": {"error": err}}


if __name__ == "__main__":
    import sys, json as _json
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--board_type", default="涨停")
        parser.add_argument("--date", default="")
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())
    result = main(params)
    print(_json.dumps(result, ensure_ascii=False, default=str))
