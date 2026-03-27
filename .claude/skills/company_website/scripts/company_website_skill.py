"""
Company Website / News Skill - LLM 智能优化版

功能：
1. 使用 LLM 理解用户真实意图，生成多个搜索关键词
2. 多轮搜索获取更丰富的数据
3. LLM 筛选和排序，返回用户真正关注的内容
4. 去除无关公告，聚焦重大事件
"""
import os
import json
from typing import Dict, Any, List, Optional
from loguru import logger


class CompanyWebsiteSkill:
    """
    公司新闻/重大事件技能 - LLM 智能优化版

    核心优化：
    - LLM 理解用户查询意图
    - 生成多个搜索关键词进行多轮搜索
    - LLM 筛选和排序结果，返回用户真正关心的内容
    """

    async def execute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行智能新闻采集"""
        ts_code = params.get("ts_code", "")
        stock_name = params.get("name", "") or params.get("query", "")
        user_query = params.get("user_intent", "") or params.get("query", "")
        max_results = params.get("max_results", 15)

        if not ts_code and not stock_name:
            return {
                "error": "缺少 ts_code 或 name 参数，无法搜索新闻",
                "for_llm": {"error": "缺少 ts_code 或 name 参数"},
            }

        try:
            search_term = stock_name or ts_code.replace(".SH", "").replace(".SZ", "")

            logger.info(f"[CompanyWebsite] Stock: {search_term}, User query: {user_query}")

            # Step 1: 使用 LLM 分析用户意图，生成多个搜索关键词
            search_keywords = await self._generate_search_keywords(search_term, user_query)
            logger.info(f"[CompanyWebsite] Generated {len(search_keywords)} search keywords: {search_keywords}")

            # Step 2: 多轮搜索，收集更多数据
            all_news_items = []
            seen_titles = set()

            for keyword in search_keywords:
                logger.info(f"[CompanyWebsite] Searching: {keyword}")
                items = await self._search_news_via_playwright(keyword, max_results=20)

                # 去重
                for item in items:
                    title_key = item.get("title", "")[:30]  # 用标题前30字符作为去重key
                    if title_key and title_key not in seen_titles:
                        seen_titles.add(title_key)
                        all_news_items.append(item)

                logger.info(f"[CompanyWebsite] Found {len(items)} items, total unique: {len(all_news_items)}")

                # 如果已经收集了足够多的数据，可以提前停止
                if len(all_news_items) >= 50:
                    break

            if not all_news_items:
                return {
                    "ts_code": ts_code,
                    "stockName": stock_name,
                    "name": stock_name,
                    "items": [{
                        "title": "暂无相关新闻",
                        "snippet": f"未能从搜索引擎获取到 {stock_name or ts_code} 的相关新闻，请稍后重试",
                        "source": "系统提示",
                        "date": "",
                        "link": ""
                    }],
                    "for_llm": {"message": f"未能获取 {stock_name or ts_code} 的新闻"},
                }

            # Step 3: 使用 LLM 筛选和排序结果
            logger.info(f"[CompanyWebsite] Filtering {len(all_news_items)} items with LLM...")
            filtered_items = await self._filter_and_rank_news(
                all_news_items,
                search_term,
                user_query,
                max_results
            )

            logger.info(f"[CompanyWebsite] Final result: {len(filtered_items)} items")

            return {
                "ts_code": ts_code,
                "stockName": stock_name,
                "name": stock_name,
                "items": filtered_items,
                "count": len(filtered_items),
                "total_searched": len(all_news_items),
                "for_llm": {
                    "company": stock_name or ts_code,
                    "news_count": len(filtered_items),
                    "message": f"成功获取 {stock_name or ts_code} 的 {len(filtered_items)} 条相关新闻",
                    "top_titles": [item.get("title", "") for item in filtered_items[:3]],
                },
            }

        except Exception as e:
            logger.error(f"[CompanyWebsite] Error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {"error": f"新闻采集失败: {str(e)}", "for_llm": {"error": f"新闻采集失败: {str(e)}"}}

    async def _generate_search_keywords(self, stock_name: str, user_query: str) -> List[str]:
        """
        使用 LLM 分析用户意图，生成多个搜索关键词
        """
        try:
            from anthropic import AsyncAnthropic

            api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY", "")
            model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
            client = AsyncAnthropic(api_key=api_key)

            prompt = f"""你是一个金融信息检索专家。用户想了解关于"{stock_name}"的信息。

用户的原始查询是："{user_query}"

请分析用户真正想了解的内容，生成3-5个搜索关键词，用于在百度资讯中搜索。

要求：
1. 每个关键词都要包含公司名称"{stock_name}"
2. 关键词要具体、有针对性，能搜到用户真正关心的内容
3. 避免太笼统的词如"新闻"、"公告"
4. 关注：重大事件、战略合作、技术突破、业绩增长、资本运作、市场动态等
5. 如果用户查询提到特定主题（如IPO、并购、新产品等），优先生成相关关键词

请直接返回JSON数组格式，例如：
["兆易创新 港股上市", "兆易创新 战略合作", "兆易创新 DRAM突破"]
"""

            response = await client.messages.create(
                model=model,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )

            content = response.content[0].text.strip()

            # 解析 JSON 数组 - 处理可能的 markdown 代码块
            if "```" in content:
                # 提取代码块内容
                import re
                code_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', content)
                if code_match:
                    content = code_match.group(1).strip()

            if content.startswith("["):
                keywords = json.loads(content)
                if isinstance(keywords, list) and len(keywords) > 0:
                    return keywords[:5]  # 最多5个关键词

            # 如果解析失败，返回默认关键词
            logger.warning(f"[CompanyWebsite] Failed to parse LLM keywords: {content}")

        except Exception as e:
            logger.error(f"[CompanyWebsite] LLM keyword generation failed: {e}")

        # 默认关键词（兜底）
        return [
            f"{stock_name} 重大事件 战略",
            f"{stock_name} 技术突破 研发",
            f"{stock_name} 业绩 财报",
            f"{stock_name} 投资 并购"
        ]

    async def _filter_and_rank_news(
        self,
        news_items: List[Dict[str, Any]],
        stock_name: str,
        user_query: str,
        max_results: int
    ) -> List[Dict[str, Any]]:
        """
        使用 LLM 筛选和排序新闻
        - 过滤掉无关的公告和杂讯
        - 按相关性和重要性排序
        """
        if len(news_items) <= max_results:
            return news_items

        try:
            from anthropic import AsyncAnthropic

            api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY", "")
            model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
            client = AsyncAnthropic(api_key=api_key)

            # 构建新闻摘要供 LLM 分析
            news_summary = []
            for i, item in enumerate(news_items[:40]):  # 最多分析40条
                news_summary.append({
                    "id": i,
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", "")[:100],
                    "source": item.get("source", ""),
                    "date": item.get("date", "")
                })

            prompt = f"""你是一个金融信息筛选专家。用户想了解关于"{stock_name}"的信息。

用户的原始查询是："{user_query}"

以下是搜索到的新闻列表：
{json.dumps(news_summary, ensure_ascii=False, indent=2)}

请从中筛选出最相关、最重要的新闻，返回新闻ID列表（按重要性排序）。

筛选标准：
1. 优先选择：重大战略合作、技术突破、业绩增长、资本运作（IPO/并购等）、市场扩张等重大事件
2. 过滤掉：普通公告（例行公告、停复牌公告等）、股吧讨论、广告软文、无关内容
3. 如果用户查询有特定关注点，优先选择相关内容
4. 时效性：优先选择近期新闻
5. 来源权威性：优先选择财联社、证券时报、第一财经等权威媒体

请返回最多{max_results}条新闻的ID，格式为JSON数组，例如：[3, 7, 1, 15, 8]
"""

            response = await client.messages.create(
                model=model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}]
            )

            content = response.content[0].text.strip()

            # 解析返回的ID列表
            if "[" in content:
                start = content.index("[")
                end = content.rindex("]") + 1
                id_list = json.loads(content[start:end])

                if isinstance(id_list, list):
                    # 根据ID列表重新排序
                    result = []
                    for news_id in id_list:
                        if isinstance(news_id, int) and 0 <= news_id < len(news_items):
                            result.append(news_items[news_id])
                        if len(result) >= max_results:
                            break

                    if result:
                        logger.info(f"[CompanyWebsite] LLM filtered: {len(news_items)} -> {len(result)}")
                        return result

            logger.warning(f"[CompanyWebsite] Failed to parse LLM filter result: {content}")

        except Exception as e:
            logger.error(f"[CompanyWebsite] LLM filtering failed: {e}")

        # 兜底：返回前 max_results 条
        return news_items[:max_results]

    async def _search_news_via_playwright(self, query: str, max_results: int = 20, retry_count: int = 2) -> List[Dict[str, Any]]:
        """
        使用 Playwright 通过百度资讯搜索新闻
        增加重试机制和更灵活的等待策略
        """
        import asyncio

        for attempt in range(retry_count + 1):
            try:
                results = await self._do_search(query, max_results)
                if results:
                    return results

                if attempt < retry_count:
                    logger.info(f"[CompanyWebsite] Retry {attempt + 1}/{retry_count} for query: {query}")
                    await asyncio.sleep(1)  # 重试前等待

            except Exception as e:
                logger.warning(f"[CompanyWebsite] Search attempt {attempt + 1} failed: {e}")
                if attempt < retry_count:
                    await asyncio.sleep(1)

        return []

    async def _do_search(self, query: str, max_results: int = 20) -> List[Dict[str, Any]]:
        """执行实际的搜索操作 - 使用新浪财经搜索（更稳定，不会触发验证码）"""
        try:
            from playwright.async_api import async_playwright
            import re

            # 来源域名映射
            source_map = {
                "eastmoney.com": "东方财富",
                "sina.com.cn": "新浪财经",
                "163.com": "网易财经",
                "qq.com": "腾讯新闻",
                "sohu.com": "搜狐财经",
                "hexun.com": "和讯网",
                "10jqka.com.cn": "同花顺",
                "cnstock.com": "中国证券网",
                "stcn.com": "证券时报",
                "cs.com.cn": "中证网",
                "yicai.com": "第一财经",
                "caixin.com": "财新网",
                "jiemian.com": "界面新闻",
                "cls.cn": "财联社",
                "nbd.com.cn": "每日经济新闻",
                "thepaper.cn": "澎湃新闻",
                "xinhuanet.com": "新华网",
                "people.com.cn": "人民网",
                "cctv.com": "央视网",
                "gov.cn": "政府网站",
                "36kr.com": "36氪",
                "wallstreetcn.com": "华尔街见闻",
                "douyin.com": "抖音",
                "finance.sina.com": "新浪财经",
            }

            # 需要过滤的非新闻页面
            filter_domains = [
                "quote.eastmoney.com",
                "guba.eastmoney.com",
                "stockpage.10jqka.com.cn",
                "xueqiu.com/S/",
                "stock.weibo.cn",
                "guba.",
                "finance.sina.com.cn/realstock",
            ]

            results = []

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )

                # 使用新浪财经搜索（更稳定，不会触发验证码）
                from urllib.parse import quote
                search_url = f"https://search.sina.com.cn/?q={quote(query)}&c=news&from=channel&ie=utf-8"
                logger.debug(f"[CompanyWebsite] Fetching: {search_url}")

                # 使用 domcontentloaded，更快更稳定
                await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)

                # 等待一小段时间让动态内容加载
                import asyncio
                await asyncio.sleep(1.5)

                # 新浪搜索的选择器
                try:
                    await page.wait_for_selector("div.box-result", timeout=8000)
                    logger.debug("[CompanyWebsite] Found results with selector: div.box-result")
                except:
                    logger.warning("[CompanyWebsite] No results found with sina selector")

                # 获取搜索结果 - 新浪财经搜索结构
                elements = await page.query_selector_all("div.box-result")

                for el in elements:
                    if len(results) >= max_results:
                        break

                    try:
                        # 新浪搜索结构: h2 > a 获取标题和链接
                        title_el = await el.query_selector("h2 a")
                        if not title_el:
                            continue

                        title = await title_el.inner_text()
                        title = title.strip()

                        if not title or len(title) < 5:
                            continue

                        # 获取链接
                        link = await title_el.get_attribute("href") or ""

                        # 过滤非新闻页面
                        should_skip = False
                        for domain in filter_domains:
                            if domain in link:
                                should_skip = True
                                break
                        if should_skip:
                            continue

                        # 新浪搜索结构: p.content 获取摘要
                        snippet = ""
                        content_el = await el.query_selector("p.content")
                        if content_el:
                            snippet = await content_el.inner_text()
                            snippet = snippet.strip()

                        # 如果没有摘要，使用 r-info
                        if not snippet:
                            try:
                                info_el = await el.query_selector("div.r-info")
                                if info_el:
                                    full_text = await info_el.inner_text()
                                    # 只取第一段作为摘要
                                    full_text = re.sub(r'\s+', ' ', full_text)
                                    if len(full_text) > 20:
                                        snippet = full_text[:200]
                            except:
                                pass

                        # 提取来源和日期 - 新浪搜索的 r-info 包含来源和时间
                        source = "未知来源"
                        date = ""
                        try:
                            info_el = await el.query_selector("div.r-info")
                            if info_el:
                                info_text = await info_el.inner_text()

                                # 尝试提取日期 (格式: 2026-01-17 18:35:27)
                                date_match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', info_text)
                                if date_match:
                                    date = date_match.group(1)
                                else:
                                    # 尝试其他日期格式
                                    date_patterns = [
                                        r'(\d{4}年\d{1,2}月\d{1,2}日)',
                                        r'(\d{1,2}月\d{1,2}日)',
                                        r'(\d+小时前)',
                                        r'(\d+分钟前)',
                                    ]
                                    for pattern in date_patterns:
                                        match = re.search(pattern, info_text)
                                        if match:
                                            date = match.group(1)
                                            break

                                # 尝试从 info 中提取来源 (日期后面的内容)
                                # 格式: "xxx新闻 2026-01-17 18:35:27"
                                lines = info_text.strip().split('\n')
                                if lines:
                                    last_line = lines[-1].strip()
                                    # 来源通常在日期之前
                                    source_match = re.search(r'^(.+?)\s+\d{4}', last_line)
                                    if source_match:
                                        source = source_match.group(1).strip()
                        except:
                            pass

                        # 如果没有从 info 中获取来源，从链接域名推断
                        if source == "未知来源":
                            try:
                                from urllib.parse import urlparse
                                parsed = urlparse(link)
                                domain = parsed.netloc.lower()
                                if domain.startswith("www."):
                                    domain = domain[4:]

                                for key, name in source_map.items():
                                    if key in domain:
                                        source = name
                                        break

                                if source == "未知来源":
                                    parts = domain.split(".")
                                    if len(parts) >= 2:
                                        source = parts[-2]
                            except:
                                pass

                        results.append({
                            "title": title,
                            "snippet": snippet or "点击查看详情",
                            "source": source,
                            "date": date,
                            "link": link
                        })

                    except Exception as e:
                        logger.debug(f"[CompanyWebsite] Error parsing element: {e}")
                        continue

                await browser.close()

            return results

        except Exception as e:
            logger.error(f"[CompanyWebsite] Playwright search failed: {e}")
            return []


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main(params: Dict[str, Any]) -> Dict[str, Any]:
    """同步入口，供框架调用"""
    import asyncio
    skill = CompanyWebsiteSkill()
    try:
        return asyncio.run(skill.execute(params))
    except Exception as e:
        return {"error": str(e), "for_llm": {"error": str(e)}}


if __name__ == "__main__":
    import sys
    import json as _json
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--query", default="")
        parser.add_argument("--ts_code", default="")
        parser.add_argument("--name", default="")
        parser.add_argument("--user_intent", default="")
        parser.add_argument("--max_results", type=int, default=15)
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())
    result = main(params)
    print(_json.dumps(result, ensure_ascii=False))
