"""
Stock Query Skill
股票查询技能 - 根据代码查询股票基础信息和实时行情
"""
import os
import re
import time
from typing import Dict, Any, Optional


def _resolve_ts_code_from_query(query: str) -> Optional[str]:
    """通过新浪 Suggest 将名称/关键词解析为 ts_code"""
    import httpx
    timestamp = int(time.time() * 1000)
    url = f"http://suggest3.sinajs.cn/suggest/type=&key={query}&name=suggestdata_{timestamp}"
    headers = {"Referer": "https://finance.sina.com.cn/"}
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            content = resp.text.split('"')[1] if '"' in resp.text else ""
            if not content:
                return None
            # 清理 query 关键词
            search_key = re.sub(r'详细分析|分析|查询|查看|获取|股票|行情|走势|价格|信息|帮我|请|一下|的', '', query).strip()
            if not search_key:
                search_key = query
            for item in content.split(';'):
                if not item.strip():
                    continue
                parts = item.split(',')
                if len(parts) < 5:
                    continue
                stock_name = parts[0]
                type_code = parts[1]
                code_body = parts[2]
                full_code = parts[3]
                if type_code not in ['11', '21', '51']:
                    continue
                if stock_name not in search_key and search_key not in stock_name and search_key not in code_body:
                    continue
                market_prefix = full_code[:2] if len(full_code) > 2 else ""
                if market_prefix == "sh":
                    return f"{code_body}.SH"
                elif market_prefix == "sz":
                    return f"{code_body}.SZ"
                elif market_prefix == "bj":
                    return f"{code_body}.BJ"
                else:
                    if code_body.startswith('6'):
                        return f"{code_body}.SH"
                    elif code_body.startswith('8') or code_body.startswith('4'):
                        return f"{code_body}.BJ"
                    else:
                        return f"{code_body}.SZ"
    except Exception as e:
        print(f"[stock_query] Sina resolve error: {e}")
    return None


def _get_stock_info(code: str) -> Dict[str, Any]:
    """获取个股基础信息（AkShare）"""
    try:
        import akshare as ak
        df = ak.stock_individual_info_em(symbol=code)
        if df is None or df.empty:
            return {}
        info = {}
        for _, row in df.iterrows():
            info[str(row.iloc[0])] = row.iloc[1]
        return info
    except Exception as e:
        print(f"[stock_query] get_stock_info error: {e}")
        return {}


def _get_realtime_quote(ts_code: str) -> Dict[str, float]:
    """获取实时行情（新浪财经）"""
    import httpx
    code = ts_code.split('.')[0]
    market = "sz" if ts_code.endswith(".SZ") else "sh" if ts_code.endswith(".SH") else "bj" if ts_code.endswith(".BJ") else ""
    if not market:
        market = "sh" if code.startswith("6") else "sz"
    sina_code = f"{market}{code}"
    url = f"http://hq.sinajs.cn/list={sina_code}"
    headers = {"Referer": "https://finance.sina.com.cn/"}
    defaults = {"price": 0.0, "pre_close": 0.0, "open": 0.0, "high": 0.0, "low": 0.0,
                "bid1": 0.0, "ask1": 0.0, "vol": 0, "amount": 0.0, "pct_chg": 0.0}
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code != 200:
                return defaults
            content = resp.text.split('"')[1] if '"' in resp.text else ""
            if not content:
                return defaults
            parts = content.split(',')
            if len(parts) > 9:
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
                return {"price": price, "pre_close": pre_close, "open": open_p,
                        "high": high, "low": low, "bid1": bid1, "ask1": ask1,
                        "vol": vol, "amount": amount, "pct_chg": round(pct_chg, 2)}
    except Exception as e:
        print(f"[stock_query] realtime quote error: {e}")
    return defaults


def _parse_float(val, default=0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    查询股票基础信息和实时行情

    params:
        ts_code (str): 股票代码，如 600519.SH
        query (str): 股票名称关键词（与 ts_code 二选一）
    """
    ts_code = params.get("ts_code", "").strip()
    query = params.get("query", "").strip()

    # 如果没有 ts_code，尝试通过 query 解析
    if not ts_code and query:
        ts_code = _resolve_ts_code_from_query(query)

    if not ts_code:
        return {
            "error": "缺少股票代码参数 ts_code 或有效的 query",
            "for_llm": {"error": "缺少股票代码参数 ts_code 或有效的 query"}
        }

    code = ts_code.split('.')[0]

    # 获取个股基础信息
    info = _get_stock_info(code)
    if not info:
        return {
            "error": f"未找到股票 {ts_code} 的详细信息",
            "for_llm": {"error": f"未找到股票 {ts_code} 的详细信息"}
        }

    # 获取实时行情
    quote = _get_realtime_quote(ts_code)

    # 解析市值
    total_mv = _parse_float(info.get("总市值", 0))
    circ_mv = _parse_float(info.get("流通市值", 0))

    # PE/PB 尝试从个股信息中获取
    pe_ttm = _parse_float(info.get("市盈率(TTM)", 0) or info.get("市盈率-动态", 0))
    pb = _parse_float(info.get("市净率", 0))

    area = str(info.get("地域", "") or info.get("所属地域", "") or "")
    list_date = str(info.get("上市时间", "") or "")
    market_str = list_date[:4] + "年上市" if list_date else ""

    data = {
        "ts_code": ts_code,
        "symbol": code,
        "name": str(info.get("股票简称", "")),
        "industry": str(info.get("行业", "-")),
        "market": market_str,
        "area": area if area else "-",
        "price": quote["price"],
        "pre_close": quote["pre_close"],
        "open": quote["open"],
        "high": quote["high"],
        "low": quote["low"],
        "pct_chg": quote["pct_chg"],
        "vol": quote["vol"],
        "amount": quote["amount"],
        "bid1": quote["bid1"],
        "ask1": quote["ask1"],
        "pe_ttm": pe_ttm,
        "pb": pb,
        "total_mv": total_mv,
        "circ_mv": circ_mv,
        "for_llm": {
            "ts_code": ts_code,
            "name": str(info.get("股票简称", "")),
            "industry": str(info.get("行业", "-")),
            "area": area if area else "-",
            "price": quote["price"],
            "pct_chg": quote["pct_chg"],
            "pe_ttm": pe_ttm,
            "pb": pb,
            "total_mv_yi": round(total_mv / 1e8, 2) if total_mv else 0
        }
    }

    print(f"[stock_query] Query successful: {data['name']} ({ts_code})")
    return data


if __name__ == "__main__":
    import sys
    import json
    import argparse

    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description="股票信息查询")
        parser.add_argument("--ts_code", type=str, default="", help="股票代码，如 600519.SH")
        parser.add_argument("--query", type=str, default="", help="股票名称关键词")
        args = parser.parse_args()
        result = main({"ts_code": args.ts_code, "query": args.query})
    else:
        data = json.loads(sys.stdin.read())
        result = main(data)

    print(json.dumps(result, ensure_ascii=False))
