"""
Realtime Quote Skill
实时行情技能 - 获取股票实时价格、涨跌幅、量价数据
"""
import os
from typing import Dict, Any


def _get_realtime_from_sina(ts_code: str) -> Dict[str, Any]:
    """通过新浪财经接口获取实时行情"""
    import httpx
    code = ts_code.split('.')[0]
    if ts_code.endswith(".SH"):
        market = "sh"
    elif ts_code.endswith(".SZ"):
        market = "sz"
    elif ts_code.endswith(".BJ"):
        market = "bj"
    else:
        market = "sh" if code.startswith("6") else "sz"

    sina_code = f"{market}{code}"
    url = f"http://hq.sinajs.cn/list={sina_code}"
    headers = {"Referer": "https://finance.sina.com.cn/"}

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code != 200:
                return {}
            content = resp.text.split('"')[1] if '"' in resp.text else ""
            if not content:
                return {}
            parts = content.split(',')
            if len(parts) < 10:
                return {}
            # Sina format: name,open,pre_close,price,high,low,bid,ask,vol,amount,...
            name = parts[0]
            open_p = float(parts[1]) if parts[1] else 0.0
            pre_close = float(parts[2]) if parts[2] else 0.0
            price = float(parts[3]) if parts[3] else 0.0
            high = float(parts[4]) if parts[4] else 0.0
            low = float(parts[5]) if parts[5] else 0.0
            bid1 = float(parts[6]) if parts[6] else 0.0
            ask1 = float(parts[7]) if parts[7] else 0.0
            vol = int(float(parts[8])) if parts[8] else 0
            amount = float(parts[9]) if parts[9] else 0.0
            pct_chg = ((price - pre_close) / pre_close * 100) if pre_close > 0 else 0.0
            change = price - pre_close

            trade_date = parts[30] if len(parts) > 30 else ""
            trade_time = parts[31] if len(parts) > 31 else ""

            return {
                "name": name,
                "open": open_p,
                "pre_close": pre_close,
                "price": price,
                "high": high,
                "low": low,
                "bid1": bid1,
                "ask1": ask1,
                "vol": vol,
                "amount": amount,
                "pct_chg": round(pct_chg, 2),
                "change": round(change, 2),
                "trade_date": trade_date,
                "trade_time": trade_time,
            }
    except Exception as e:
        print(f"[realtime_quote] Sina error: {e}")
    return {}


def _get_realtime_from_akshare(code: str) -> Dict[str, Any]:
    """通过 AkShare 获取实时行情（备用）"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return {}
        row = df[df['代码'] == code]
        if row.empty:
            return {}
        r = row.iloc[0]
        price = float(r.get('最新价', 0) or 0)
        pre_close = float(r.get('昨收', 0) or 0)
        pct_chg = float(r.get('涨跌幅', 0) or 0)
        return {
            "name": str(r.get('名称', '')),
            "price": price,
            "pre_close": pre_close,
            "open": float(r.get('今开', 0) or 0),
            "high": float(r.get('最高', 0) or 0),
            "low": float(r.get('最低', 0) or 0),
            "vol": int(float(r.get('成交量', 0) or 0)),
            "amount": float(r.get('成交额', 0) or 0),
            "pct_chg": round(pct_chg, 2),
            "change": round(price - pre_close, 2)
        }
    except Exception as e:
        print(f"[realtime_quote] AkShare error: {e}")
    return {}


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    获取股票实时行情

    params:
        ts_code (str): 股票代码，如 600519.SH
    """
    ts_code = params.get("ts_code", "").strip()
    if not ts_code:
        return {
            "error": "缺少 ts_code 参数",
            "for_llm": {"error": "缺少 ts_code 参数"}
        }

    code = ts_code.split('.')[0]

    # 优先使用新浪接口
    quote = _get_realtime_from_sina(ts_code)

    # 如果新浪失败，使用 AkShare 兜底
    if not quote or quote.get("price", 0) == 0:
        quote = _get_realtime_from_akshare(code)

    if not quote:
        return {
            "error": f"获取 {ts_code} 实时行情失败",
            "for_llm": {"error": f"获取 {ts_code} 实时行情失败"}
        }

    result = {
        "ts_code": ts_code,
        **quote,
        "for_llm": {
            "ts_code": ts_code,
            "name": quote.get("name", ""),
            "price": quote.get("price", 0),
            "pct_chg": quote.get("pct_chg", 0),
            "change": quote.get("change", 0),
            "high": quote.get("high", 0),
            "low": quote.get("low", 0),
            "vol_wan": round(quote.get("vol", 0) / 10000, 2),
            "amount_yi": round(quote.get("amount", 0) / 1e8, 2),
            "trade_time": quote.get("trade_time", "")
        }
    }
    return result


if __name__ == "__main__":
    import sys
    import json
    import argparse

    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description="股票实时行情")
        parser.add_argument("--ts_code", type=str, required=True, help="股票代码，如 600519.SH")
        args = parser.parse_args()
        result = main({"ts_code": args.ts_code})
    else:
        data = json.loads(sys.stdin.read())
        result = main(data)

    print(json.dumps(result, ensure_ascii=False))
