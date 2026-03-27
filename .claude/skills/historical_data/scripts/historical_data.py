"""
Historical Data Skill
历史行情数据采集技能 - 获取股票日线/周线/月线历史数据
"""
import os
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta


def _get_stock_history(code: str, period: str = "daily",
                       start_date: str = "", end_date: str = "",
                       adjust: str = "qfq") -> Optional[Any]:
    """获取历史行情数据，优先新浪财经（直连稳定），降级东方财富"""
    import akshare as ak
    import pandas as pd

    suffix = code.split(".")[-1].upper() if "." in code else (
        "SH" if code.startswith(("6", "9")) else "SZ"
    )
    pure_code = code.split(".")[0]

    # 优先：新浪财经（在 NO_PROXY 直连，稳定）
    try:
        period_map = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}
        ak_period = period_map.get(period, "daily")
        sina_symbol = ("sh" if suffix == "SH" else "sz") + pure_code

        if ak_period == "daily":
            df = ak.stock_zh_a_daily(symbol=sina_symbol, start_date=start_date,
                                     end_date=end_date, adjust=adjust)
        else:
            # 周线/月线降级到东方财富（新浪不支持）
            raise ValueError(f"sina no {ak_period}, use eastmoney")

        if df is not None and not df.empty:
            df = df.rename(columns={"volume": "vol", "date": "trade_date"}).copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
            return df

    except Exception as e:
        print(f"[historical_data] sina error: {e}, fallback to eastmoney")

    # 降级：东方财富
    try:
        df = ak.stock_zh_a_hist(symbol=pure_code, period=period,
                                start_date=start_date, end_date=end_date, adjust=adjust)
        if df is None or df.empty:
            return None
        col_map = {
            '日期': 'trade_date', '开盘': 'open', '最高': 'high', '最低': 'low',
            '收盘': 'close', '成交量': 'volume', '成交额': 'amount',
            '涨跌幅': 'pct_chg', '涨跌额': 'change', '振幅': 'amplitude', '换手率': 'turnover_rate',
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if 'volume' in df.columns:
            df = df.rename(columns={'volume': 'vol'})
        return df
    except Exception as e:
        print(f"[historical_data] AkShare get_stock_history error: {e}")
        return None


def _get_stock_info_basic(code: str) -> Dict[str, Any]:
    """获取个股基本信息（行业、名称等），用新浪财经避免东方财富连接问题"""
    try:
        import akshare as ak
        pure_code = code.split(".")[0]
        suffix = code.split(".")[-1].upper() if "." in code else (
            "SH" if pure_code.startswith(("6", "9")) else "SZ"
        )
        sina_symbol = ("sh" if suffix == "SH" else "sz") + pure_code
        df = ak.stock_individual_basic_info_xq(symbol=sina_symbol)
        if df is not None and not df.empty:
            info = dict(zip(df.iloc[:, 0].astype(str), df.iloc[:, 1].astype(str)))
            return {
                "name": info.get("股票简称", info.get("name", "")),
                "industry": info.get("行业", info.get("industry", "")),
                "area": info.get("地域", ""),
            }
    except Exception:
        pass
    # 降级：直接从代码推断
    try:
        import akshare as ak
        pure_code = code.split(".")[0]
        df = ak.stock_individual_info_em(symbol=pure_code)
        if df is not None and not df.empty:
            info = {}
            for _, row in df.iterrows():
                info[str(row.iloc[0])] = row.iloc[1]
            return {
                "name": str(info.get("股票简称", "")),
                "industry": str(info.get("行业", "")),
                "area": str(info.get("地域", "") or info.get("所属地域", "")),
            }
    except Exception as e:
        print(f"[historical_data] get_stock_info error: {e}")
    return {}


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    获取股票历史行情数据

    params:
        ts_code (str): 股票代码，如 600519.SH
        freq (str): D/W/M，默认 D
        limit (int): 获取条数，默认 120
        start_date (str): 开始日期 YYYYMMDD，可选
        end_date (str): 结束日期 YYYYMMDD，可选
    """
    import pandas as pd

    ts_code = params.get("ts_code", "").strip()
    if not ts_code:
        return {
            "error": "缺少 ts_code 参数",
            "for_llm": {"error": "缺少 ts_code 参数"}
        }

    freq = params.get("freq", "D").upper()
    limit = int(params.get("limit", 120))
    start_date = params.get("start_date", "")
    end_date = params.get("end_date", "")

    code = ts_code.split('.')[0]
    end_date_str = end_date if end_date else datetime.now().strftime("%Y%m%d")

    if not start_date:
        # 根据频率计算回溯天数
        if freq == "W":
            days_back = limit * 7 * 1.5
        elif freq == "M":
            days_back = limit * 30 * 1.5
        else:
            days_back = limit * 1.5
        start_date = (datetime.now() - timedelta(days=int(days_back))).strftime("%Y%m%d")

    period_map = {"D": "daily", "W": "weekly", "M": "monthly"}
    period = period_map.get(freq, "daily")

    df = _get_stock_history(code, period=period, start_date=start_date, end_date=end_date_str, adjust="qfq")

    if df is None or df.empty:
        return {
            "ts_code": ts_code,
            "freq": freq,
            "count": 0,
            "data": [],
            "for_llm": {
                "ts_code": ts_code,
                "freq": freq,
                "count": 0,
                "message": f"未找到 {ts_code} 的 {freq} 线数据"
            }
        }

    # 标准化日期格式
    if 'trade_date' in df.columns:
        df['trade_date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y%m%d')
    df = df.sort_values('trade_date')

    if limit and len(df) > limit:
        df = df.tail(limit)

    records = df.to_dict('records')

    # 确保每条记录含 date 字段（前端图表需要）
    for r in records:
        r['date'] = r.get('trade_date', '')

    # 获取股票基本信息
    stock_info = _get_stock_info_basic(code)

    # 最新数据
    latest = records[-1] if records else {}
    period_start = records[0].get('trade_date', '') if records else ''
    period_end = records[-1].get('trade_date', '') if records else ''

    return {
        "ts_code": ts_code,
        "freq": freq,
        "count": len(records),
        "data": records,
        "name": stock_info.get("name", ""),
        "industry": stock_info.get("industry", ""),
        "area": stock_info.get("area", ""),
        "for_llm": {
            "ts_code": ts_code,
            "name": stock_info.get("name", ""),
            "freq": freq,
            "count": len(records),
            "latest_close": latest.get("close"),
            "latest_pct_chg": latest.get("pct_chg"),
            "period": f"{period_start}~{period_end}"
        }
    }


if __name__ == "__main__":
    import sys
    import json
    import argparse

    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description="股票历史行情数据")
        parser.add_argument("--ts_code", type=str, required=True, help="股票代码，如 600519.SH")
        parser.add_argument("--freq", type=str, default="D", help="频率 D/W/M")
        parser.add_argument("--limit", type=int, default=120, help="获取条数")
        parser.add_argument("--start_date", type=str, default="", help="开始日期 YYYYMMDD")
        parser.add_argument("--end_date", type=str, default="", help="结束日期 YYYYMMDD")
        args = parser.parse_args()
        result = main({
            "ts_code": args.ts_code,
            "freq": args.freq,
            "limit": args.limit,
            "start_date": args.start_date,
            "end_date": args.end_date,
        })
    else:
        data = json.loads(sys.stdin.read())
        result = main(data)

    print(json.dumps(result, ensure_ascii=False))
