"""
Dragon Tiger Skill
龙虎榜技能
获取龙虎榜详情和营业部排名，分析机构和游资动向
"""
import os
import math
import logging
from datetime import datetime, timedelta
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


def _get_dragon_tiger(date: str = "", code: str = "") -> Optional[object]:
    if not date:
        date = datetime.now().strftime('%Y%m%d')
    try:
        import akshare as ak
        if code:
            df = ak.stock_lhb_stock_statistic_em(symbol="近一月")
            if df is not None and not df.empty:
                df = df[df['代码'].astype(str).str.contains(code)]
        else:
            df = ak.stock_lhb_detail_em(start_date=date, end_date=date)
        if df is not None and not df.empty:
            logger.info(f"get_dragon_tiger succeeded: {date}, rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"get_dragon_tiger failed: {date}: {e}")
    return None


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")
    date = params.get("date", "")
    code = ts_code.split('.')[0] if '.' in ts_code else ts_code

    try:
        df = _get_dragon_tiger(date=date, code=code)
        if df is None or df.empty:
            err = "无法获取龙虎榜数据，可能非交易日或数据源不可用"
            return {"error": err, "for_llm": {"error": err}}

        import pandas as pd
        # 清理 NaN
        df = df.where(df.notna(), other=None)
        items = []
        for _, row in df.head(30).iterrows():
            item = {}
            for col in df.columns:
                val = row[col]
                if val is None:
                    item[col] = ""
                else:
                    try:
                        fval = float(val)
                        item[col] = fval if not (math.isnan(fval) or math.isinf(fval)) else ""
                    except (ValueError, TypeError):
                        item[col] = str(val)
            items.append(item)
        columns = [{"key": col, "label": col} for col in df.columns]
        display_date = date if date else datetime.now().strftime('%Y-%m-%d')
        summary = {"total": len(df), "date": display_date}
        analysis = f"{display_date} 龙虎榜共{len(df)}条记录。"

        result = {
            "ts_code": ts_code,
            "title": f"龙虎榜 ({display_date})",
            "items": items,
            "columns": columns,
            "summary": summary,
            "analysis": analysis,
            "data_source": "akshare/lhb_detail_em",
        }
        result["for_llm"] = {
            "date": display_date,
            "total_records": len(df),
            "ts_code": ts_code or "市场",
            "analysis": analysis,
        }
        return result

    except Exception as e:
        logger.error(f"龙虎榜数据获取失败: {e}", exc_info=True)
        err = f"龙虎榜数据获取失败: {str(e)}"
        return {"error": err, "for_llm": {"error": err}}


if __name__ == "__main__":
    import sys, json as _json
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--ts_code", default="")
        parser.add_argument("--date", default="")
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())
    result = main(params)
    print(_json.dumps(result, ensure_ascii=False, default=str))
