"""
Market Overview Skill
市场概览技能 - 获取A股主要指数和市场宽度统计
"""
import os
from typing import Dict, Any, List
from datetime import datetime


def _get_index_spot() -> List[Dict]:
    """获取主要指数实时行情"""
    index_list = [
        ("000001", "上证指数", "sh"),
        ("399001", "深证成指", "sz"),
        ("399006", "创业板指", "sz"),
        ("000300", "沪深300", "sh"),
        ("000016", "上证50", "sh"),
        ("000905", "中证500", "sh"),
    ]
    results = []
    try:
        import akshare as ak
        df = ak.stock_zh_index_spot_em()
        if df is None or df.empty:
            return results

        for code, name, _ in index_list:
            row = df[df['代码'] == code]
            if row.empty:
                continue
            r = row.iloc[0]
            try:
                price = float(r.get('最新价', 0) or 0)
                pct_chg = float(r.get('涨跌幅', 0) or 0)
                change = float(r.get('涨跌额', 0) or 0)
                results.append({
                    "code": code,
                    "name": name,
                    "price": round(price, 2),
                    "pct_chg": round(pct_chg, 2),
                    "change": round(change, 2),
                    "open": float(r.get('今开', 0) or 0),
                    "high": float(r.get('最高', 0) or 0),
                    "low": float(r.get('最低', 0) or 0),
                    "vol": int(float(r.get('成交量', 0) or 0)),
                    "amount": float(r.get('成交额', 0) or 0),
                })
            except Exception:
                continue
    except Exception as e:
        print(f"[market_overview] get_index_spot error: {e}")
    return results


def _get_market_stats() -> Dict[str, int]:
    """获取市场宽度统计（涨跌停、涨跌平家数）"""
    stats = {"up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0, "total": 0}
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return stats

        total = len(df)
        pct_col = '涨跌幅'
        if pct_col not in df.columns:
            return stats

        import pandas as pd
        df[pct_col] = pd.to_numeric(df[pct_col], errors='coerce')

        up = int((df[pct_col] > 0).sum())
        down = int((df[pct_col] < 0).sum())
        flat = int((df[pct_col] == 0).sum())
        limit_up = int((df[pct_col] >= 9.8).sum())
        limit_down = int((df[pct_col] <= -9.8).sum())

        return {
            "up": up, "down": down, "flat": flat,
            "limit_up": limit_up, "limit_down": limit_down,
            "total": total
        }
    except Exception as e:
        print(f"[market_overview] get_market_stats error: {e}")
    return stats


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    获取A股市场概览

    params:
        date (str): 查询日期 YYYYMMDD，默认今日（仅用于标记，实时数据不受此参数影响）
    """
    date = params.get("date", datetime.now().strftime("%Y%m%d"))

    indices = _get_index_spot()
    market_stats = _get_market_stats()

    # 判断市场情绪
    up = market_stats.get("up", 0)
    down = market_stats.get("down", 0)
    total = market_stats.get("total", 1)
    up_ratio = up / total if total > 0 else 0

    if up_ratio >= 0.65:
        sentiment = "强势"
    elif up_ratio >= 0.55:
        sentiment = "偏多"
    elif up_ratio >= 0.45:
        sentiment = "中性"
    elif up_ratio >= 0.35:
        sentiment = "偏空"
    else:
        sentiment = "弱势"

    # 上证指数信息
    sh_index = next((i for i in indices if i["code"] == "000001"), {})

    for_llm = {
        "date": date,
        "sh_index": sh_index.get("price"),
        "sh_pct_chg": sh_index.get("pct_chg"),
        "up_count": up,
        "down_count": down,
        "flat_count": market_stats.get("flat", 0),
        "limit_up": market_stats.get("limit_up", 0),
        "limit_down": market_stats.get("limit_down", 0),
        "up_ratio": round(up_ratio * 100, 1),
        "sentiment": sentiment,
        "indices_summary": [
            f"{i['name']}: {i['price']} ({'+' if i['pct_chg'] >= 0 else ''}{i['pct_chg']}%)"
            for i in indices[:3]
        ]
    }

    return {
        "date": date,
        "indices": indices,
        "market_stats": market_stats,
        "sentiment": sentiment,
        "for_llm": for_llm
    }


if __name__ == "__main__":
    import sys
    import json
    import argparse

    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description="A股市场概览")
        parser.add_argument("--date", type=str, default="", help="查询日期 YYYYMMDD")
        args = parser.parse_args()
        result = main({"date": args.date} if args.date else {})
    else:
        data = json.loads(sys.stdin.read())
        result = main(data)

    print(json.dumps(result, ensure_ascii=False))
