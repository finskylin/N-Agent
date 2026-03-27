"""
Technical Indicators Skill
技术指标计算技能 - 基于历史K线数据计算 MA/MACD/KDJ/RSI/BOLL 等技术指标
"""
import os
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta


def _fetch_eastmoney_kline(symbol: str, start_date: str, end_date: str) -> List[Dict]:
    """直接调用东方财富 API 获取 K 线数据（绕过 AKShare，自带重试和 UA）"""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    # 判断市场前缀：沪市1，深市0
    code = symbol.split(".")[0]
    suffix = symbol.split(".")[-1].upper() if "." in symbol else ""
    market = "1" if suffix in ("SH", "BJ") else "0"
    secid = f"{market}.{code}"

    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": 101, "fqt": 1,
        "secid": secid,
        "beg": start_date, "end": end_date,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://quote.eastmoney.com/",
        "Connection": "close",
    }
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    # 东方财富直连，不走代理（代理对东方财富不稳定）
    NO_PROXY = {"http": None, "https": None}

    resp = session.get(url, params=params, headers=headers, proxies=NO_PROXY, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    klines = (data.get("data") or {}).get("klines") or []

    records = []
    for k in klines:
        parts = k.split(",")
        if len(parts) >= 6:
            records.append({
                "trade_date": parts[0].replace("-", ""),
                "date": parts[0].replace("-", ""),
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "vol": float(parts[5]),
                "amount": float(parts[6]) if len(parts) > 6 else 0.0,
                "pct_chg": float(parts[8]) if len(parts) > 8 else 0.0,
            })
    return records


def _get_historical_data(code: str, days_needed: int) -> List[Dict]:
    """获取历史行情数据，优先用新浪财经（直连稳定），降级用东方财富 API"""
    import akshare as ak
    import pandas as pd

    start_date = (datetime.now() - timedelta(days=days_needed * 2)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")
    pure_code = code.split(".")[0]
    suffix = code.split(".")[-1].upper() if "." in code else (
        "SH" if pure_code.startswith(("6", "9")) else "SZ"
    )

    # 优先：新浪财经（在 NO_PROXY 列表里，直连稳定）
    try:
        sina_symbol = ("sh" if suffix == "SH" else "sz") + pure_code
        df = ak.stock_zh_a_daily(symbol=sina_symbol, start_date=start_date,
                                 end_date=end_date, adjust="qfq")
        if df is not None and not df.empty:
            df = df.rename(columns={"volume": "vol"}).copy()
            df["trade_date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
            df["date"] = df["trade_date"]
            df = df.sort_values("trade_date").reset_index(drop=True)
            for col in ["close", "open", "high", "low", "vol"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            return df.to_dict("records")
    except Exception as e:
        print(f"[technical_indicators] sina error: {e}, falling back to eastmoney")

    # 降级：东方财富直接 API
    try:
        records = _fetch_eastmoney_kline(code, start_date, end_date)
        if records:
            return records
    except Exception as e:
        print(f"[technical_indicators] get_historical_data error: {e}")

    return []


def _calculate_indicators_series(data: List[Dict], output_days: int = 60):
    """计算多日时间序列技术指标"""
    import pandas as pd
    import numpy as np

    df = pd.DataFrame(data)
    if df.empty:
        return [], {}

    for col in ['close', 'open', 'high', 'low', 'vol']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    if 'trade_date' in df.columns:
        df['date'] = df['trade_date']
    df = df.sort_values('date').reset_index(drop=True)

    close = df['close']
    high = df['high']
    low = df['low']

    # MA
    df['ma5'] = close.rolling(window=5).mean()
    df['ma10'] = close.rolling(window=10).mean()
    df['ma20'] = close.rolling(window=20).mean()
    df['ma60'] = close.rolling(window=60).mean()

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df['dif'] = ema12 - ema26
    df['dea'] = df['dif'].ewm(span=9, adjust=False).mean()
    df['macd'] = (df['dif'] - df['dea']) * 2

    # KDJ
    low_min = low.rolling(window=9).min()
    high_max = high.rolling(window=9).max()
    rsv = (close - low_min) / (high_max - low_min) * 100
    df['k'] = rsv.ewm(com=2, adjust=False).mean()
    df['d'] = df['k'].ewm(com=2, adjust=False).mean()
    df['j'] = 3 * df['k'] - 2 * df['d']

    # RSI
    def calc_rsi(series, period):
        delta = series.diff()
        up = delta.clip(lower=0)
        down = -1 * delta.clip(upper=0)
        ma_up = up.ewm(com=period - 1, adjust=False).mean()
        ma_down = down.ewm(com=period - 1, adjust=False).mean()
        rs = ma_up / ma_down
        return 100 - (100 / (1 + rs))

    df['rsi6'] = calc_rsi(close, 6)
    df['rsi12'] = calc_rsi(close, 12)
    df['rsi24'] = calc_rsi(close, 24)

    # BOLL
    df['boll_mid'] = close.rolling(window=20).mean()
    boll_std = close.rolling(window=20).std()
    df['boll_upper'] = df['boll_mid'] + 2 * boll_std
    df['boll_lower'] = df['boll_mid'] - 2 * boll_std

    df_output = df.tail(output_days).copy()

    series_data = []
    for _, row in df_output.iterrows():
        item = {
            "date": row.get('date', ''),
            "open": round(float(row['open']), 2) if pd.notna(row.get('open')) else None,
            "high": round(float(row['high']), 2) if pd.notna(row.get('high')) else None,
            "low": round(float(row['low']), 2) if pd.notna(row.get('low')) else None,
            "close": round(float(row['close']), 2) if pd.notna(row.get('close')) else None,
            "vol": round(float(row.get('vol', 0)), 0) if pd.notna(row.get('vol')) else None,
            "ma5": round(float(row['ma5']), 2) if pd.notna(row.get('ma5')) else None,
            "ma10": round(float(row['ma10']), 2) if pd.notna(row.get('ma10')) else None,
            "ma20": round(float(row['ma20']), 2) if pd.notna(row.get('ma20')) else None,
            "ma60": round(float(row['ma60']), 2) if pd.notna(row.get('ma60')) else None,
            "dif": round(float(row['dif']), 2) if pd.notna(row.get('dif')) else None,
            "dea": round(float(row['dea']), 2) if pd.notna(row.get('dea')) else None,
            "macd": round(float(row['macd']), 2) if pd.notna(row.get('macd')) else None,
            "k": round(float(row['k']), 2) if pd.notna(row.get('k')) else None,
            "d": round(float(row['d']), 2) if pd.notna(row.get('d')) else None,
            "j": round(float(row['j']), 2) if pd.notna(row.get('j')) else None,
            "rsi6": round(float(row['rsi6']), 2) if pd.notna(row.get('rsi6')) else None,
            "rsi12": round(float(row['rsi12']), 2) if pd.notna(row.get('rsi12')) else None,
            "rsi24": round(float(row['rsi24']), 2) if pd.notna(row.get('rsi24')) else None,
            "boll_upper": round(float(row['boll_upper']), 2) if pd.notna(row.get('boll_upper')) else None,
            "boll_mid": round(float(row['boll_mid']), 2) if pd.notna(row.get('boll_mid')) else None,
            "boll_lower": round(float(row['boll_lower']), 2) if pd.notna(row.get('boll_lower')) else None,
        }
        series_data.append(item)

    last_row = df_output.iloc[-1] if not df_output.empty else None
    latest_indicators = {}
    if last_row is not None:
        latest_indicators = {
            "ma": {
                "ma5": round(float(last_row['ma5']), 2) if pd.notna(last_row.get('ma5')) else None,
                "ma10": round(float(last_row['ma10']), 2) if pd.notna(last_row.get('ma10')) else None,
                "ma20": round(float(last_row['ma20']), 2) if pd.notna(last_row.get('ma20')) else None,
                "ma60": round(float(last_row['ma60']), 2) if pd.notna(last_row.get('ma60')) else None,
            },
            "macd": {
                "dif": round(float(last_row['dif']), 2) if pd.notna(last_row.get('dif')) else None,
                "dea": round(float(last_row['dea']), 2) if pd.notna(last_row.get('dea')) else None,
                "macd": round(float(last_row['macd']), 2) if pd.notna(last_row.get('macd')) else None,
            },
            "kdj": {
                "k": round(float(last_row['k']), 2) if pd.notna(last_row.get('k')) else None,
                "d": round(float(last_row['d']), 2) if pd.notna(last_row.get('d')) else None,
                "j": round(float(last_row['j']), 2) if pd.notna(last_row.get('j')) else None,
            },
            "rsi": {
                "rsi6": round(float(last_row['rsi6']), 2) if pd.notna(last_row.get('rsi6')) else None,
                "rsi12": round(float(last_row['rsi12']), 2) if pd.notna(last_row.get('rsi12')) else None,
                "rsi24": round(float(last_row['rsi24']), 2) if pd.notna(last_row.get('rsi24')) else None,
            },
            "boll": {
                "upper": round(float(last_row['boll_upper']), 2) if pd.notna(last_row.get('boll_upper')) else None,
                "mid": round(float(last_row['boll_mid']), 2) if pd.notna(last_row.get('boll_mid')) else None,
                "lower": round(float(last_row['boll_lower']), 2) if pd.notna(last_row.get('boll_lower')) else None,
            }
        }

    return series_data, latest_indicators


def _generate_signals(indicators: Dict) -> List[Dict]:
    """生成技术信号"""
    signals = []
    ma = indicators.get("ma", {})
    if ma.get("ma5") and ma.get("ma10") and ma["ma5"] > ma["ma10"]:
        signals.append({"type": "MA", "signal": "GOLDEN_CROSS", "message": "5日均线大于10日均线"})

    kdj = indicators.get("kdj", {})
    j_val = kdj.get("j")
    if j_val is not None:
        if j_val > 80:
            signals.append({"type": "KDJ", "signal": "OVERBOUGHT", "message": "KDJ超买"})
        elif j_val < 20:
            signals.append({"type": "KDJ", "signal": "OVERSOLD", "message": "KDJ超卖"})

    rsi = indicators.get("rsi", {})
    rsi6 = rsi.get("rsi6")
    if rsi6 is not None:
        if rsi6 > 70:
            signals.append({"type": "RSI", "signal": "OVERBOUGHT", "message": f"RSI6={rsi6}超买"})
        elif rsi6 < 30:
            signals.append({"type": "RSI", "signal": "OVERSOLD", "message": f"RSI6={rsi6}超卖"})

    return signals


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    计算技术指标

    params:
        ts_code (str): 股票代码，如 600519.SH
        indicators (list): 指标列表，默认 ["MA","MACD","KDJ","RSI","BOLL"]
        days (int): 输出最近 N 天，默认 60
    """
    ts_code = params.get("ts_code", "").strip()
    if not ts_code:
        return {
            "error": "缺少 ts_code 参数",
            "for_llm": {"error": "缺少 ts_code 参数"}
        }

    days = int(params.get("days", 60))
    code = ts_code.split('.')[0]

    # 需要更多历史数据用于计算（指标需要前置数据）
    historical = _get_historical_data(code, days + 60)

    if not historical:
        return {
            "error": f"无法获取 {ts_code} 的历史数据用于技术指标计算",
            "for_llm": {"error": f"无法获取 {ts_code} 的历史数据"}
        }

    try:
        series_data, latest_indicators = _calculate_indicators_series(historical, days)
        signals = _generate_signals(latest_indicators)

        # 构建 for_llm 摘要
        macd_info = latest_indicators.get("macd", {})
        kdj_info = latest_indicators.get("kdj", {})
        rsi_info = latest_indicators.get("rsi", {})

        return {
            "ts_code": ts_code,
            "title": f"技术指标分析 (近{days}日)",
            "series": series_data,
            "latest": latest_indicators,
            "signals": signals,
            "stats": {
                "days": len(series_data),
                "start_date": series_data[0]["date"] if series_data else "",
                "end_date": series_data[-1]["date"] if series_data else ""
            },
            "for_llm": {
                "ts_code": ts_code,
                "days": len(series_data),
                "latest_date": series_data[-1]["date"] if series_data else "",
                "macd_dif": macd_info.get("dif"),
                "macd_dea": macd_info.get("dea"),
                "kdj_k": kdj_info.get("k"),
                "kdj_d": kdj_info.get("d"),
                "kdj_j": kdj_info.get("j"),
                "rsi6": rsi_info.get("rsi6"),
                "rsi12": rsi_info.get("rsi12"),
                "signals": [s["message"] for s in signals]
            }
        }

    except Exception as e:
        return {
            "error": f"指标计算失败: {str(e)}",
            "for_llm": {"error": f"指标计算失败: {str(e)}"}
        }


if __name__ == "__main__":
    import sys
    import json
    import argparse

    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description="技术指标计算")
        parser.add_argument("--ts_code", type=str, required=True, help="股票代码，如 600519.SH")
        parser.add_argument("--days", type=int, default=60, help="输出最近 N 天")
        args = parser.parse_args()
        result = main({"ts_code": args.ts_code, "days": args.days})
    else:
        data = json.loads(sys.stdin.read())
        result = main(data)

    print(json.dumps(result, ensure_ascii=False))
