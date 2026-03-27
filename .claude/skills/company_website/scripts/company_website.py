#!/usr/bin/env python3
"""
Company Website Skill Script
公司官网信息抓取技能脚本 - 使用 Playwright + Proxy 支持动态网站与搜索
"""
import argparse
import json
import asyncio
import sys
import os
from typing import Dict, Any, Optional, List

# browser_service is not imported from app layer; use Playwright directly
browser_service = None

# 缓存层 (可选优化，不阻止搜索)
COMPANY_WEBSITE_CACHE = {}

async def scrape_company(ts_code: str, content_type: str = "all") -> Dict[str, Any]:
    """抓取公司官网信息 - 始终使用搜索引擎发现，无硬编码"""
    if not browser_service:
        return {"error": "BrowserService not found"}

    result = {"ts_code": ts_code, "success": False}
    
    # 始终使用搜索引擎发现官网 (Google 优先，代理生效)
    query = f"{ts_code} 公司官网"
    search_results = await browser_service.search(query, engine="google", limit=3)
    
    if not search_results or "error" in search_results[0]:
        # Fallback to Baidu if Google fails
        search_results = await browser_service.search(query, engine="baidu", limit=3)
        
    if not search_results:
        return {"error": f"未找到 {ts_code} 相关官网"}
        
    # Pick first result
    url = search_results[0].get("url", "")
    name = search_results[0].get("title", ts_code)
    result["source"] = "google_search"
    result["search_results"] = search_results
    
    result["name"] = name
    result["website"] = url
    
    # 2. Crawl Content
    try:
        page_data = await browser_service.crawl_page(url)
        
        if "error" in page_data:
            result["error"] = page_data["error"]
        else:
            result["success"] = True
            result["title"] = page_data.get("title")
            # Limit text length
            text = page_data.get("content", "")
            result["content"] = text  # 保留完整内容，不截断
            
            # Simple heuristic for extraction (Logic from original script simplified)
            if "pdf" in page_data.get("html", "").lower():
                result["has_docs"] = True
            
    except Exception as e:
        result["error"] = str(e)
    
    await browser_service.close()
    return result

async def main_async(args):
    """异步主函数"""
    if args.url:
         # Direct Crawl
         data = await browser_service.crawl_page(args.url)
         await browser_service.close()
         return data
    else:
         return await scrape_company(args.ts_code, args.type)

def main():
    parser = argparse.ArgumentParser(description='公司官网信息抓取技能 (Proxy Enabled)')
    parser.add_argument('--ts_code', type=str, help='股票代码')
    parser.add_argument('--url', type=str, help='直接指定URL抓取')
    parser.add_argument('--type', type=str, default='all')
    parser.add_argument('--playwright', action='store_true', help='(Deprecated) Always uses playwright')
    parser.add_argument('--output', type=str, default='json')
    
    args = parser.parse_args()
    
    try:
        if not args.ts_code and not args.url:
            print(json.dumps({"error": "Need --ts_code or --url"}))
            return

        result = asyncio.run(main_async(args))
        
        if args.output == 'json':
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result)
            
    except Exception as e:
        print(json.dumps({"error": str(e)}))

if __name__ == "__main__":
    main()
