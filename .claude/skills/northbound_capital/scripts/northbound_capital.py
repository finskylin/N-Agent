"""
Northbound Capital Skill
北向资金技能
获取沪深港通资金流向和个股持股数据
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


def _get_northbound_holding(code: str):
    """获取个股北向持股数据"""
    try:
        import akshare as ak
        df = ak.stock_hsgt_individual_em(symbol=code)
        return df
    except Exception as e:
        logger.warning(f"get_northbound_holding failed: {code}: {e}")
    return None


def _get_northbound_flow():
    """获取北向资金整体流向"""
    try:
        import akshare as ak
        df = ak.stock_hsgt_fund_flow_summary_em()
        return df
    except Exception as e:
        logger.warning(f"get_northbound_flow failed: {e}")
    try:
        import akshare as ak
        df = ak.stock_hsgt_north_net_flow_in_em()
        return df
    except Exception as e:
        logger.warning(f"get_northbound_flow fallback failed: {e}")
    return None


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")
    code = ts_code.split('.')[0] if '.' in ts_code else ts_code

    try:
        if code:
            df = _get_northbound_holding(code)
            title = f"{ts_code} 北向持股"
        else:
            df = _get_northbound_flow()
            title = "北向资金流向"

        if df is None or df.empty:
            err = "无法获取北向资金数据，可能非交易时间或数据源不可用"
            return {"error": err, "for_llm": {"error": err}}

        items = []
        for _, row in df.head(20).iterrows():
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
        summary = {"total_records": len(df)}
        if ts_code:
            summary["ts_code"] = ts_code
        analysis = f"北向资金数据获取成功，共{len(df)}条记录。"

        result = {
            "ts_code": ts_code,
            "title": title,
            "items": items,
            "columns": columns,
            "summary": summary,
            "analysis": analysis,
            "data_source": "akshare/hsgt_em",
        }
        result["for_llm"] = {
            "ts_code": ts_code or "市场",
            "title": title,
            "total_records": len(df),
            "analysis": analysis,
        }
        return result

    except Exception as e:
        logger.error(f"北向资金数据获取失败: {e}", exc_info=True)
        err = f"北向资金数据获取失败: {str(e)}"
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
