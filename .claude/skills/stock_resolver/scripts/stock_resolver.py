"""
Stock Resolver Skill
股票代码解析技能 - 将股票名称/关键词解析为标准 ts_code
"""
import os
import re
import time
from typing import Dict, Any, List, Optional

import httpx


def _sina_suggest(query: str) -> List[Dict[str, str]]:
    """通过新浪 Suggest 接口搜索股票代码"""
    timestamp = int(time.time() * 1000)
    url = f"http://suggest3.sinajs.cn/suggest/type=&key={query}&name=suggestdata_{timestamp}"
    headers = {"Referer": "https://finance.sina.com.cn/"}
    results = []
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code != 200:
                return results
            content = resp.text.split('"')[1] if '"' in resp.text else ""
            if not content:
                return results
            items = content.split(';')
            for item in items:
                if not item.strip():
                    continue
                parts = item.split(',')
                if len(parts) < 5:
                    continue
                stock_name = parts[0]
                type_code = parts[1]
                code_body = parts[2]
                full_code = parts[3]  # e.g., sh688027

                # 11=沪市股票, 21=深市股票, 51=北交所
                if type_code not in ['11', '21', '51']:
                    continue

                market_prefix = full_code[:2] if len(full_code) > 2 else ""
                if market_prefix == "sh":
                    ts_code = f"{code_body}.SH"
                elif market_prefix == "sz":
                    ts_code = f"{code_body}.SZ"
                elif market_prefix == "bj":
                    ts_code = f"{code_body}.BJ"
                else:
                    if code_body.startswith('6'):
                        ts_code = f"{code_body}.SH"
                    elif code_body.startswith('8') or code_body.startswith('4'):
                        ts_code = f"{code_body}.BJ"
                    else:
                        ts_code = f"{code_body}.SZ"

                results.append({
                    "ts_code": ts_code,
                    "name": stock_name,
                    "market": ts_code.split('.')[-1]
                })
    except Exception as e:
        print(f"[stock_resolver] Sina suggest error: {e}")
    return results


def _akshare_search(query: str) -> List[Dict[str, str]]:
    """通过 AkShare 搜索股票"""
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        if df is None or df.empty:
            return []
        mask = df['name'].str.contains(query, na=False) | df['code'].str.contains(query, na=False)
        matched = df[mask].head(5)
        results = []
        for _, row in matched.iterrows():
            code = str(row['code'])
            name = str(row['name'])
            if code.startswith('6'):
                ts_code = f"{code}.SH"
            elif code.startswith('8') or code.startswith('4'):
                ts_code = f"{code}.BJ"
            else:
                ts_code = f"{code}.SZ"
            results.append({"ts_code": ts_code, "name": name, "market": ts_code.split('.')[-1]})
        return results
    except Exception as e:
        print(f"[stock_resolver] AkShare search error: {e}")
        return []


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    解析股票名称/关键词为 ts_code

    params:
        query (str): 股票名称或代码关键词
    """
    query = params.get("query", "").strip()
    if not query:
        return {
            "error": "缺少 query 参数",
            "for_llm": {"error": "缺少 query 参数"}
        }

    # 先尝试新浪 Suggest（更快）
    matches = _sina_suggest(query)

    # 如果新浪无结果，使用 AkShare 兜底
    if not matches:
        matches = _akshare_search(query)

    if not matches:
        return {
            "error": f"未找到与 '{query}' 匹配的股票",
            "for_llm": {"error": f"未找到与 '{query}' 匹配的股票", "query": query}
        }

    best_match = matches[0]
    return {
        "best_match": best_match,
        "matches": matches,
        "query": query,
        "for_llm": {
            "ts_code": best_match["ts_code"],
            "name": best_match["name"],
            "query": query,
            "all_matches": [f"{m['ts_code']} ({m['name']})" for m in matches]
        }
    }


if __name__ == "__main__":
    import sys
    import json
    import argparse

    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description="股票代码解析")
        parser.add_argument("--query", type=str, required=True, help="股票名称或代码关键词")
        args = parser.parse_args()
        result = main({"query": args.query})
    else:
        data = json.loads(sys.stdin.read())
        result = main(data)

    print(json.dumps(result, ensure_ascii=False))
