"""
Institutional Research Skill
机构调研记录技能
获取上市公司机构调研记录，分析机构关注度
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


def _get_institution_visits(code: str) -> List[Dict]:
    """获取机构调研详情"""
    today = datetime.now()
    date_str = today.strftime('%Y%m%d')
    dates_to_try = []
    for i in range(30):
        d = today - timedelta(days=i)
        if d.weekday() < 5:
            dates_to_try.append(d.strftime('%Y%m%d'))

    for try_date in dates_to_try[:5]:
        try:
            import akshare as ak
            df = ak.stock_jgdy_detail_em(date=try_date)
            if df is not None and not df.empty:
                # Filter by code if provided
                if code:
                    for col in df.columns:
                        if '代码' in str(col) or 'code' in str(col).lower():
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
                logger.info(f"get_institution_visits succeeded: {code or 'all'}, date={try_date}, rows={len(records)}")
                return records
        except Exception as e:
            logger.warning(f"get_institution_visits failed date={try_date}: {e}")
            continue

    # Fallback: summary
    for try_date in dates_to_try[:5]:
        try:
            import akshare as ak
            df = ak.stock_jgdy_tj_em(date=try_date)
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
                logger.info(f"get_institution_visits fallback succeeded: {try_date}, rows={len(records)}")
                return records
        except Exception as e:
            logger.warning(f"get_institution_visits fallback failed date={try_date}: {e}")
            continue

    return []


def _analyze_visits(visits: List[Dict], code: str) -> Dict[str, Any]:
    """分析机构调研数据"""
    summary = {"total_visits": len(visits)}
    if not visits:
        return summary

    # Count unique institutions
    inst_names = set()
    for v in visits:
        for col, val in v.items():
            if "机构" in str(col) and "名称" in str(col) and val:
                inst_names.add(str(val))
    summary["unique_institutions"] = len(inst_names)

    # Count institution types
    fund_count = 0
    for v in visits:
        for col, val in v.items():
            if "类型" in str(col) and "基金" in str(val):
                fund_count += 1

    signals = []
    if len(visits) >= 10:
        signals.append(f"近期机构调研频繁（{len(visits)}次），机构关注度高")
    elif len(visits) >= 3:
        signals.append(f"近期有{len(visits)}次机构调研记录")
    else:
        signals.append("近期机构调研较少")

    summary["signals"] = signals
    return summary


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    ts_code = params.get("ts_code", "")
    code = ts_code.split('.')[0] if '.' in ts_code else ts_code

    try:
        visits = _get_institution_visits(code)

        if not visits:
            err = "无法获取机构调研数据，可能近期暂无调研记录"
            return {"error": err, "for_llm": {"error": err}}

        analysis = _analyze_visits(visits, code)
        signals = analysis.get("signals", [])
        signal_text = "；".join(signals) if signals else "暂无机构调研数据"

        columns = [{"key": k, "label": k} for k in (visits[0].keys() if visits else [])]

        result = {
            "ts_code": ts_code,
            "title": f"机构调研记录 - {ts_code}" if ts_code else "机构调研汇总",
            "items": visits[:20],
            "columns": columns,
            "summary": analysis,
            "analysis": signal_text,
            "data_source": "akshare/jgdy_em",
        }
        result["for_llm"] = {
            "ts_code": ts_code or "市场",
            "total_visits": analysis.get("total_visits", 0),
            "unique_institutions": analysis.get("unique_institutions", 0),
            "signals": signals,
            "analysis": signal_text,
        }
        return result

    except Exception as e:
        logger.error(f"机构调研分析失败: {e}", exc_info=True)
        err = f"机构调研分析失败: {str(e)}"
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
