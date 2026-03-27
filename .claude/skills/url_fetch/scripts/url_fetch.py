"""
UrlFetchSkill — 轻量原子网页抓取工具

功能：抓取指定 URL 的网页全文内容，支持单个或多个 URL。
不包含：搜索引擎查询、intelligent 多轮搜索、充分性检查。
反爬能力：与 web_search 完全一致（httpx 快速抓取 + Playwright 渲染降级 + JS 隐身注入）。

适用场景：quick_search 返回结果后，需要抓取某些链接的详细内容。
"""
from typing import List, Dict, Any, Optional
from playwright.async_api import async_playwright
import asyncio
import re
import os
import sys
import httpx
import time
from urllib.parse import urlparse
from loguru import logger

# ========== Skill 日志文件配置 ==========
_SKILL_LOG_FILE = os.environ.get("SKILL_LOG_FILE_URL_FETCH", "/app/logs/skill_url_fetch.log")
_SKILL_LOG_ENABLED = os.environ.get("SKILL_LOG_ENABLED", "true").lower() == "true"

if _SKILL_LOG_ENABLED:
    try:
        os.makedirs(os.path.dirname(_SKILL_LOG_FILE), exist_ok=True)
        logger.add(
            _SKILL_LOG_FILE,
            rotation="10 MB",
            retention="3 days",
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
            enqueue=True,
        )
        logger.info(f"[UrlFetchSkill] Log file enabled: {_SKILL_LOG_FILE}")
    except Exception as e:
        print(f"[UrlFetchSkill] Failed to setup log file: {e}", file=sys.stderr)


class UrlFetchSkill:
    name = "url_fetch"
    description = "抓取指定 URL 的网页全文内容。支持单个或多个 URL。使用 httpx 快速抓取 + Playwright 渲染降级，包含完整反爬机制。"

    # 共享浏览器实例
    _shared_playwright = None
    _shared_browser = None

    async def _get_shared_browser(self):
        """获取共享浏览器实例（惰性初始化）"""
        if self._shared_browser is None or not self._shared_browser.is_connected():
            if self._shared_playwright is None:
                self._shared_playwright = await async_playwright().start()
            self._shared_browser = await self._shared_playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            logger.info("[UrlFetch][SharedBrowser] New browser instance created")
        return self._shared_browser

    async def _close_shared_browser(self):
        """关闭共享浏览器实例"""
        if self._shared_browser:
            try:
                await self._shared_browser.close()
            except Exception:
                pass
            self._shared_browser = None
        if self._shared_playwright:
            try:
                await self._shared_playwright.stop()
            except Exception:
                pass
            self._shared_playwright = None

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "urls": {
                "type": "string",
                "description": "要抓取的 URL，支持多个 URL（逗号、空格或换行分隔）"
            },
        }

    async def execute(self, context) -> Dict[str, Any]:
        """执行抓取：解析 URL 列表，并行抓取内容"""
        params = context.params if hasattr(context, 'params') else context
        urls_param = params.get("urls", "") or params.get("url", "") or params.get("query", "")
        self._render_wait = min(int(params.get("render_wait", 3)), 8)

        # 兼容 LLM 将参数嵌套到 urls 字段的情况：{"urls": "{\"url\": \"...\", \"render_wait\": 15}"}
        if isinstance(urls_param, str) and urls_param.strip().startswith("{"):
            try:
                nested = json.loads(urls_param)
                if isinstance(nested, dict):
                    urls_param = nested.get("url", "") or nested.get("urls", "") or urls_param
                    if "render_wait" in nested:
                        self._render_wait = min(int(nested["render_wait"]), 8)
            except (json.JSONDecodeError, ValueError):
                pass
        elif isinstance(urls_param, dict):
            nested = urls_param
            urls_param = nested.get("url", "") or nested.get("urls", "") or ""
            if "render_wait" in nested:
                self._render_wait = int(nested["render_wait"])

        if not urls_param:
            return {"for_llm": {"error": "urls parameter is required"}, "for_ui": {}}

        logger.info(f"[UrlFetch] Starting fetch: urls='{urls_param[:100]}...'")
        start_time = time.time()

        # 解析 URL 列表
        urls = []
        for url in re.split(r'[,\s\n]+', urls_param):
            url = url.strip()
            if url and url.startswith(('http://', 'https://')):
                urls.append(url)

        if not urls:
            return {
                "for_llm": {"error": "No valid URLs provided. URLs must start with http:// or https://"},
                "for_ui": {}
            }

        # 并行抓取
        results = await self._fetch_urls(urls)

        duration = time.time() - start_time
        success_count = sum(1 for r in results if r.get("fetch_success"))
        failed_urls = [r.get("url", "") for r in results if not r.get("fetch_success")]

        logger.info(f"[UrlFetch] Completed in {duration:.2f}s. Success: {success_count}/{len(urls)}")

        # 构建 for_ui 每页卡片字段：标题、URL、发布时间
        ui_fields = []
        for idx, r in enumerate(results):
            if not r.get("fetch_success"):
                continue
            _title = r.get("title", "") or "无标题"
            _url = r.get("url", "")
            _date = r.get("publication_date", "") or ""
            ui_fields.append({"label": f"[{idx+1}] 标题", "value": _title})
            ui_fields.append({"label": f"[{idx+1}] URL", "value": _url})
            if _date:
                ui_fields.append({"label": f"[{idx+1}] 发布时间", "value": _date})
        # 兜底：无逐条字段时退化为摘要
        if not ui_fields:
            ui_fields = [
                {"label": "成功", "value": success_count},
                {"label": "失败", "value": len(failed_urls)},
                {"label": "耗时", "value": f"{duration:.1f}s"},
            ]

        return {
            "for_llm": {
                "fetched_pages": [
                    {
                        "url": r.get("url", ""),
                        "title": r.get("title", ""),
                        "content": r.get("content", "")[:8000],
                        "word_count": r.get("word_count", 0),
                        "publication_date": r.get("publication_date", ""),
                        "fetch_success": r.get("fetch_success", False),
                    }
                    for r in results
                ],
                "total_fetched": success_count,
                "failed_urls": failed_urls,
            },
            "for_ui": {
                "components": [{
                    "component": "dynamic_card",
                    "data": {
                        "title": f"网页抓取结果（{success_count}/{len(urls)}）",
                        "fields": ui_fields,
                    }
                }]
            }
        }

    async def _fetch_urls(self, urls: List[str]) -> List[Dict[str, Any]]:
        """并行抓取多个 URL"""
        max_concurrent = int(os.environ.get("URL_FETCH_CONCURRENCY", 8))
        # 单个 URL 总超时：render_wait + 固定开销，最大不超过 15s
        _per_url_timeout = min(getattr(self, '_render_wait', 6) + 6, 15)
        semaphore = asyncio.Semaphore(max_concurrent)

        results = []
        try:
            browser = await self._get_shared_browser()

            async def _do_fetch(idx: int, url: str) -> Dict[str, Any]:
                """实际抓取逻辑（由 fetch_single 用 wait_for 包裹）"""
                # 优先 httpx 快速抓取
                page_content = await self._fetch_page_content_fast(url)
                if not page_content:
                    # httpx 失败，回退到 Playwright
                    page_content = await self._fetch_page_content(browser, url)

                if page_content:
                    return {
                        "url": url,
                        "title": page_content.get("title", ""),
                        "content": page_content.get("content", ""),
                        "word_count": page_content.get("word_count", 0),
                        "publication_date": page_content.get("publication_date", ""),
                        "fetch_success": True,
                    }
                return {"url": url, "title": "", "content": "", "word_count": 0, "fetch_success": False}

            async def fetch_single(idx: int, url: str) -> Dict[str, Any]:
                async with semaphore:
                    logger.info(f"[UrlFetch][{idx+1}/{len(urls)}] Fetching: {url[:80]}... (timeout={_per_url_timeout}s)")
                    try:
                        return await asyncio.wait_for(_do_fetch(idx, url), timeout=_per_url_timeout)
                    except asyncio.TimeoutError:
                        logger.warning(f"[UrlFetch][{idx+1}] Timed out after {_per_url_timeout}s: {url[:80]}")
                        return {"url": url, "title": "", "content": "", "word_count": 0, "fetch_success": False}
                    except Exception as e:
                        logger.error(f"[UrlFetch][{idx+1}] Error: {e}")
                        return {
                            "url": url, "title": "", "content": "",
                            "word_count": 0, "fetch_success": False,
                        }

            fetch_tasks = [fetch_single(idx, url) for idx, url in enumerate(urls)]
            results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

            # 处理异常
            final = []
            for idx, res in enumerate(results):
                if isinstance(res, Exception):
                    logger.error(f"[UrlFetch][{idx+1}] Task exception: {res}")
                    final.append({
                        "url": urls[idx], "title": "", "content": "",
                        "word_count": 0, "fetch_success": False,
                    })
                else:
                    final.append(res)
            return final

        except Exception as e:
            logger.error(f"[UrlFetch] Browser initialization failed: {e}")
            return [{"url": url, "title": "", "content": "", "word_count": 0, "fetch_success": False} for url in urls]

    # ========== httpx 快速抓取（含 4 层质量检测）==========

    async def _fetch_page_content_fast(self, url: str) -> Optional[Dict[str, Any]]:
        """
        httpx 快速抓取（100-500ms），不需要 JS 渲染的页面直接用这个。
        返回 None 表示需要回退到 Playwright。

        4 层质量检测（防止误判反爬/空壳页面为成功）：
        1. Cloudflare / WAF 检测
        2. 登录墙 / 付费墙检测
        3. JS 渲染 SPA 空壳检测
        4. 内容信噪比检测
        """
        if os.environ.get("WEB_SEARCH_HTTPX_FETCH_ENABLED", "true").lower() not in ("true", "1", "yes"):
            return None

        JS_DOMAINS = {
            "twitter.com", "x.com", "instagram.com", "facebook.com",
            "youtube.com", "bilibili.com", "weibo.com", "zhihu.com",
            "tiktok.com", "douyin.com", "reddit.com",
        }
        try:
            domain = urlparse(url).netloc.lower()
        except Exception:
            return None

        if any(d in domain for d in JS_DOMAINS):
            return None

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200 and len(resp.text) > 500:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(resp.text, "html.parser")

                    # Layer 1: Cloudflare / WAF 检测
                    cf_markers = ["cf-browser-verification", "cf_chl_opt",
                                  "challenge-platform", "ray ID", "Checking your browser"]
                    raw_text = resp.text[:3000].lower()
                    if any(m.lower() in raw_text for m in cf_markers):
                        logger.info(f"[UrlFetch][FastFetch] Cloudflare/WAF detected: {url[:60]}")
                        return None

                    # Layer 2: 登录墙 / 付费墙检测
                    paywall_markers = ["请登录", "登录后查看", "sign in to continue",
                                       "subscribe to read", "付费阅读", "会员专享",
                                       "login required", "please log in"]
                    if any(m.lower() in raw_text for m in paywall_markers):
                        body_text = soup.get_text(strip=True)
                        if len(body_text) < 500:
                            logger.info(f"[UrlFetch][FastFetch] Paywall/login wall: {url[:60]}")
                            return None

                    # Layer 3: JS 渲染 SPA 空壳检测
                    body = soup.find("body")
                    if body:
                        children = [c for c in body.children if c.name]
                        if len(children) <= 3:
                            body_html = str(body)
                            spa_markers = ['id="app"', 'id="root"', 'id="__next"',
                                           'id="__nuxt"', 'data-reactroot']
                            if any(m in body_html for m in spa_markers):
                                text_ratio = len(body.get_text(strip=True)) / max(len(body_html), 1)
                                if text_ratio < 0.1:
                                    logger.info(f"[UrlFetch][FastFetch] SPA shell detected (ratio={text_ratio:.2f}): {url[:60]}")
                                    return None

                    # Layer 4: 内容信噪比检测
                    for tag in soup(["script", "style", "nav", "footer", "aside", "header"]):
                        tag.decompose()
                    content_el = soup.find("article") or soup.find("main") or soup.find("body")
                    content = content_el.get_text(separator="\n", strip=True) if content_el else ""

                    lines = [l for l in content.split("\n") if len(l.strip()) > 15]
                    meaningful_content = "\n".join(lines)

                    if len(meaningful_content) < 600:
                        logger.info(f"[UrlFetch][FastFetch] Low content quality ({len(meaningful_content)} chars): {url[:60]}")
                        return None

                    title = soup.title.string if soup.title and soup.title.string else ""
                    meta_desc = ""
                    meta_el = soup.find("meta", attrs={"name": "description"})
                    if meta_el:
                        meta_desc = meta_el.get("content", "")
                    # 提取发布日期
                    pub_date = ""
                    for date_meta in [
                        soup.find("meta", attrs={"property": "article:published_time"}),
                        soup.find("meta", attrs={"name": "publishdate"}),
                        soup.find("meta", attrs={"name": "publish_date"}),
                        soup.find("meta", attrs={"name": "date"}),
                        soup.find("time", attrs={"datetime": True}),
                    ]:
                        if date_meta:
                            raw_date = date_meta.get("content") or date_meta.get("datetime") or ""
                            if raw_date:
                                pub_date = raw_date.strip()[:25]
                                break

                    logger.info(f"[UrlFetch][FastFetch] httpx success: {url[:60]} ({len(meaningful_content)} chars)")
                    return {
                        "title": title,
                        "content": meaningful_content[:10000],
                        "word_count": len(meaningful_content),
                        "meta_description": meta_desc,
                        "publication_date": pub_date,
                    }
        except Exception:
            pass
        return None

    # ========== Playwright 渲染抓取 ==========

    async def _fetch_page_content(self, browser, url: str) -> Optional[Dict[str, Any]]:
        """
        Playwright 完整浏览器渲染抓取（用于 httpx 失败的情况）
        """
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                extra_http_headers={
                    "sec-ch-ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"macOS"',
                    "sec-fetch-dest": "document",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-site": "none",
                    "sec-fetch-user": "?1",
                    "upgrade-insecure-requests": "1",
                },
            )
            page = await context.new_page()

            # 先尝试 networkidle（等 JS 渲染完），超时则降级到 domcontentloaded
            try:
                await page.goto(url, wait_until="networkidle", timeout=15000)
            except Exception:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                except Exception:
                    pass

            # 等待内容渲染（SPA/动态页面需要更多时间）
            _render_wait_ms = getattr(self, '_render_wait', 3) * 1000
            try:
                await page.wait_for_selector(
                    "article, .article-content, .post-content, main, .content, #content, "
                    ".video-desc, .desc-text, .news-body, .detail-content, .g-body, "
                    "#endText, .post_body, .article_content",
                    timeout=_render_wait_ms,
                )
            except Exception:
                # 没找到特定选择器，等待页面稳定
                await page.wait_for_timeout(_render_wait_ms)

            title = await page.title()

            meta_desc = ""
            try:
                meta_el = await page.query_selector('meta[name="description"]')
                if meta_el:
                    meta_desc = await meta_el.get_attribute("content") or ""
            except:
                pass

            # 提取正文 - 多策略
            content = ""
            article_selectors = [
                "article", ".article-content", ".post-content", ".entry-content",
                ".content-text", ".article-body", "#article", "main article",
                ".main-content", ".news-content", ".text-content",
                "[itemprop='articleBody']",
                # 网易
                "#endText", ".post_body", ".article_content", ".g-body",
                ".video-desc", ".desc-text", ".detail-content",
                # 腾讯/微信
                "#js_content", ".rich_media_content", ".content-article",
                # 今日头条/抖音
                ".article-content", ".tta-article-content",
                # 通用
                "#content", "#main-content", ".entry", ".story-body",
            ]
            for selector in article_selectors:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        text = await el.inner_text()
                        if text and len(text) > 100:
                            content = text.strip()
                            break
                except:
                    continue

            # 策略2: body 过滤
            if not content or len(content) < 200:
                try:
                    await page.evaluate("""
                        document.querySelectorAll('header, footer, nav, aside, script, style, .sidebar, .comment, .ad, .advertisement').forEach(el => el.remove());
                    """)
                    body_el = await page.query_selector("body")
                    if body_el:
                        full_text = await body_el.inner_text()
                        lines = full_text.split('\n')
                        clean_lines = [line.strip() for line in lines if len(line.strip()) > 8]
                        content = '\n'.join(clean_lines[:100])
                except Exception:
                    pass

            # 提取发布日期
            publication_date = ""
            try:
                date_meta_selectors = [
                    'meta[property="article:published_time"]',
                    'meta[name="publishdate"]', 'meta[name="publish_date"]',
                    'meta[name="date"]', 'meta[property="og:article:published_time"]',
                    'time[datetime]',
                ]
                for sel in date_meta_selectors:
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            raw = await el.get_attribute("content") or await el.get_attribute("datetime") or ""
                            if raw:
                                publication_date = raw.strip()[:25]
                                break
                    except Exception:
                        continue
            except Exception:
                pass

            await page.close()
            await context.close()

            # 内容截断
            if len(content) > 10000:
                content = content[:10000]

            return {
                "title": title,
                "meta_description": meta_desc,
                "content": content,
                "word_count": len(content),
                "publication_date": publication_date,
            }

        except Exception as e:
            logger.debug(f"[UrlFetch] Failed to fetch page content from {url}: {e}")
            return None

    # ========== 代理配置 ==========
    def _get_proxy_config(self) -> Optional[str]:
        """获取代理配置"""
        import json
        proxy_server = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
        if not proxy_server:
            try:
                config_path = "/app/config/skills/proxy_config.json"
                if os.path.exists(config_path):
                    with open(config_path, 'r', encoding='utf-8') as f:
                        proxy_config = json.load(f)
                        proxy_section = proxy_config.get("proxy", {})
                        if proxy_section.get("enabled", False):
                            proxy_server = proxy_section.get("http_proxy") or proxy_section.get("https_proxy")
                            if not proxy_server:
                                proxy_host = proxy_section.get("proxy_host")
                                proxy_port = proxy_section.get("proxy_port")
                                proxy_protocol = proxy_section.get("proxy_protocol", "http")
                                if proxy_host and proxy_port:
                                    proxy_server = f"{proxy_protocol}://{proxy_host}:{proxy_port}"
            except Exception:
                pass
        return proxy_server


def _main():
    """直接执行入口: python3 script.py --param1 value1
    也支持 JSON stdin: echo '{"param1": "v1"}' | python3 script.py
    """
    import argparse
    import asyncio
    import json
    import sys

    params = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read().strip()
            if raw:
                params = json.loads(raw)
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Run UrlFetchSkill directly")
    parser.add_argument("--urls", type=str, dest="urls")
    args = parser.parse_args()
    for k, v in vars(args).items():
        if v is not None:
            params[k] = v

    async def run():
        skill = UrlFetchSkill()
        result = await skill.execute(params)
        out = result if isinstance(result, dict) else {"data": str(result)}
        print(json.dumps(out, ensure_ascii=False, default=str, indent=2))

    asyncio.run(run())


if __name__ == "__main__":
    _main()
