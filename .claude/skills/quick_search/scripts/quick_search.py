"""
QuickSearchSkill — 轻量原子搜索工具（性能优化版）

功能：多引擎并行搜索（Google/Baidu/Sina/DuckDuckGo），返回搜索结果列表。
不包含：intelligent 多轮搜索、充分性检查、详情页抓取。
反爬能力：与 web_search 完全一致（patchright、browserforge、代理、fallback 链）。

性能优化：
- 共享浏览器实例池（避免每次冷启动 Chromium）
- 默认 3 引擎（ddgs + baidu + sina），Google 仅在有代理时启用
- 减少固定等待时间（wait_for_selector 替代 wait_for_timeout）
- 整体 15 秒超时保护（先到先用，不等最慢引擎）
- DuckDuckGo 优先用 ddgs 库（无需浏览器）

适用场景：Agent Loop 迭代搜索 — 搜完看结果，不满意换关键词再搜。
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

# Patchright: 协议层反检测，用于 Google/DuckDuckGo 等强反爬引擎
try:
    from patchright.async_api import async_playwright as patchright_async_playwright
    PATCHRIGHT_AVAILABLE = True
    logger.info("[QuickSearch] patchright available — Google/DuckDuckGo will use protocol-level stealth")
except ImportError:
    PATCHRIGHT_AVAILABLE = False
    logger.info("[QuickSearch] patchright not installed — using playwright + JS stealth")

# browserforge: 贝叶斯网络生成统计真实的浏览器指纹
try:
    from browserforge.fingerprints import FingerprintGenerator, Screen as BFScreen
    from browserforge.headers import Browser as BFBrowser
    _bf_fp_generator = FingerprintGenerator(
        browser=BFBrowser(name="chrome", min_version=120),
        os=("macos", "windows"),
        screen=BFScreen(min_width=1280, max_width=1920, min_height=800, max_height=1080),
    )
    BROWSERFORGE_AVAILABLE = True
    logger.info("[QuickSearch] browserforge available — will generate random fingerprints")
except Exception:
    BROWSERFORGE_AVAILABLE = False
    _bf_fp_generator = None
    logger.info("[QuickSearch] browserforge not available — using static fingerprint")

# ========== Skill 日志文件配置 ==========
_SKILL_LOG_FILE = os.environ.get("SKILL_LOG_FILE_QUICK_SEARCH", "/app/logs/skill_quick_search.log")
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
        logger.info(f"[QuickSearchSkill] Log file enabled: {_SKILL_LOG_FILE}")
    except Exception as e:
        print(f"[QuickSearchSkill] Failed to setup log file: {e}", file=sys.stderr)

# Selenium imports for Google search fallback
try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

# ========== 共享浏览器池（类级别单例） ==========
_browser_pool_lock = asyncio.Lock() if hasattr(asyncio, 'Lock') else None


class _BrowserPool:
    """进程级浏览器实例池，避免每次搜索都冷启动 Chromium"""

    def __init__(self):
        # patchright 浏览器（Google/DuckDuckGo 反爬）
        self._patchright_pw = None
        self._patchright_browser = None
        # 普通 playwright 浏览器（Baidu/Sina 等）
        self._plain_pw = None
        self._plain_browser = None
        self._lock = asyncio.Lock()
        # 空闲超时自动关闭（秒）
        self._idle_timeout = 120
        self._last_used = 0
        self._cleanup_task = None

    async def get_browser(self, use_patchright: bool, proxy_server: str = None):
        """获取浏览器实例（惰性创建，自动复用）"""
        async with self._lock:
            self._last_used = time.time()
            self._schedule_cleanup()

            if use_patchright and PATCHRIGHT_AVAILABLE:
                if self._patchright_browser and self._patchright_browser.is_connected():
                    return self._patchright_browser
                # 创建新实例
                if self._patchright_pw is None:
                    self._patchright_pw = await patchright_async_playwright().start()
                launch_args = self._build_launch_args(proxy_server)
                self._patchright_browser = await self._patchright_pw.chromium.launch(**launch_args)
                logger.info("[QuickSearch][BrowserPool] Patchright browser created")
                return self._patchright_browser
            else:
                if self._plain_browser and self._plain_browser.is_connected():
                    return self._plain_browser
                if self._plain_pw is None:
                    self._plain_pw = await async_playwright().start()
                launch_args = self._build_launch_args(proxy_server)
                self._plain_browser = await self._plain_pw.chromium.launch(**launch_args)
                logger.info("[QuickSearch][BrowserPool] Plain browser created")
                return self._plain_browser

    @staticmethod
    def _build_launch_args(proxy_server: str = None) -> dict:
        args = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1920,1080",
                "--headless=new",
            ],
        }
        if proxy_server:
            args["proxy"] = {
                "server": proxy_server,
                "bypass": "localhost,127.0.0.1,docker.internal"
            }
        return args

    def _schedule_cleanup(self):
        """调度空闲清理任务"""
        if self._cleanup_task and not self._cleanup_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._cleanup_task = asyncio.ensure_future(self._idle_cleanup())
        except Exception:
            pass

    async def _idle_cleanup(self):
        """空闲超时后关闭浏览器释放资源"""
        while True:
            await asyncio.sleep(30)
            if time.time() - self._last_used > self._idle_timeout:
                await self.close_all()
                logger.info("[QuickSearch][BrowserPool] Idle cleanup done")
                break

    async def close_all(self):
        """关闭所有浏览器实例"""
        for browser, pw, name in [
            (self._patchright_browser, self._patchright_pw, "patchright"),
            (self._plain_browser, self._plain_pw, "plain"),
        ]:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass
        self._patchright_browser = None
        self._patchright_pw = None
        self._plain_browser = None
        self._plain_pw = None


# 全局浏览器池
_browser_pool = _BrowserPool()


class QuickSearchSkill:
    name = "quick_search"
    description = "轻量搜索引擎查询，返回搜索结果列表。支持 Google/Baidu/Sina/DuckDuckGo 多引擎并行搜索。不抓取详情页，仅返回搜索摘要。适用于迭代搜索场景。"

    # 整体搜索超时（秒）：先到先用，不等最慢的引擎
    _SEARCH_TIMEOUT = 5

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "query": {
                "type": "string",
                "description": "搜索关键词"
            },
            "max_results": {
                "type": "integer",
                "description": "返回结果数量，默认10条"
            },
        }

    # Engine URLs and Selectors
    ENGINES = {
        "google": {
            "url": "https://www.google.com/search?q={query}",
            "result_selector": "div.g, div[data-hveid], div.tF2Cxc, div[jscontroller][data-hveid]",
            "title_selector": "h3",
            "link_selector": "a",
            "snippet_selectors": ["div.VwiC3b", "div[style*='-webkit-line-clamp']", "span.aCOpRe", "div[data-sncf]", "div.IsZvec"],
            "requires_proxy": True,
        },
        "baidu": {
            "url": "https://www.baidu.com/s?wd={query}&tn=news&ie=utf-8",
            "result_selector": "div.result, div.c-container, div[class*='result']",
            "title_selector": "h3 a, a.news-title-font_1xS-F, a[class*='title']",
            "link_selector": "h3 a, a.news-title-font_1xS-F, a[class*='title']",
            "snippet_selectors": [
                "span.c-font-normal", "div.c-span-last", "span[class*='content']",
                "div[class*='content']", "p[class*='content']", ".c-color-text"
            ],
            "requires_proxy": False,
        },
        "sina": {
            "url": "https://search.sina.com.cn/?q={query}&c=news",
            "result_selector": "div.box-result",
            "title_selector": "h2 > a",
            "link_selector": "h2 > a",
            "snippet_selectors": ["p.content", "div.content"],
            "requires_proxy": False,
        },
        "duckduckgo": {
            "url": "https://html.duckduckgo.com/html/?q={query}",
            "result_selector": "div.result, div.links_main",
            "title_selector": "a.result__a",
            "link_selector": "a.result__a",
            "snippet_selectors": ["a.result__snippet", "div.result__snippet"],
            "requires_proxy": True,
        },
        "sogou": {
            "url": "https://www.sogou.com/web?query={query}",
            "result_selector": "div.vrwrap, div.rb",
            "title_selector": "h3 a",
            "link_selector": "h3 a",
            "snippet_selectors": ["p.str_info", "div.str_info", "p.str-text-info", "div.text-layout"],
            "requires_proxy": False,
        }
    }

    FILTER_DOMAINS = [
        "quote.eastmoney.com", "guba.eastmoney.com", "stockpage.10jqka.com.cn",
        "xueqiu.com/S/", "stock.weibo.cn", "guba.sina.cn", "emwap.eastmoney.com",
        "vip.stock.finance.sina.com.cn", "guba.", "stock.sina.com.cn/hkstock",
        "sina.com.cn/stock", "finance.sina.com.cn/realstock",
    ]

    SOURCE_MAP = {
        "eastmoney.com": "东方财富", "sina.com.cn": "新浪财经", "163.com": "网易财经",
        "qq.com": "腾讯新闻", "sohu.com": "搜狐财经", "hexun.com": "和讯网",
        "10jqka.com.cn": "同花顺", "cnstock.com": "中国证券网", "stcn.com": "证券时报",
        "cs.com.cn": "中证网", "yicai.com": "第一财经", "caixin.com": "财新网",
        "jiemian.com": "界面新闻", "cls.cn": "财联社", "nbd.com.cn": "每日经济新闻",
        "thepaper.cn": "澎湃新闻", "xinhuanet.com": "新华网", "people.com.cn": "人民网",
        "cctv.com": "央视网", "gov.cn": "政府网站",
    }

    # ========== 搜索时间过滤 ==========
    _time_filter_config = None

    @classmethod
    def _load_time_filter_config(cls):
        """懒加载时间过滤配置"""
        if cls._time_filter_config is not None:
            return cls._time_filter_config
        try:
            import json as _json
            cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "../../../../config/skills/search_time_filter.json")
            cfg_path = os.path.normpath(cfg_path)
            if not os.path.exists(cfg_path):
                cfg_path = "/app/config/skills/search_time_filter.json"
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cls._time_filter_config = _json.load(f)
            else:
                cls._time_filter_config = {}
        except Exception as e:
            logger.warning(f"[QuickSearch][TimeFilter] Config load error: {e}")
            cls._time_filter_config = {}
        return cls._time_filter_config

    @staticmethod
    def _apply_time_filter(url: str, engine_name: str, time_range: str) -> str:
        """追加时间过滤参数到 URL"""
        try:
            if not time_range or time_range == "none":
                return url
            cfg = QuickSearchSkill._load_time_filter_config()
            if not cfg.get("enabled", True):
                return url
            params_str = cfg.get("time_range_params", {}).get(time_range, {}).get(engine_name, "")
            if not params_str or not isinstance(params_str, str):
                return url
            if params_str == "__dynamic_bing_1y__":
                from datetime import datetime, timedelta
                _end = int((datetime.utcnow() - datetime(1970, 1, 1)).days)
                _start = _end - 365
                params_str = f"filters=ex1%3a%22ez5_{_start}_{_end}%22"
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}{params_str}"
        except Exception:
            return url

    def _get_time_range(self) -> str:
        """读取时间过滤范围"""
        try:
            _sp = "/tmp/_search_time_range.txt"
            if os.path.exists(_sp):
                with open(_sp, "r") as _f:
                    _file_tr = _f.read().strip().lower()
                if _file_tr in {"1d", "7d", "30d", "1y"}:
                    return _file_tr
        except Exception:
            pass
        return "none"

    async def execute(self, context) -> Dict[str, Any]:
        """执行搜索：多引擎并行，15 秒超时保护，先到先用"""
        params = context.params if hasattr(context, 'params') else context
        query = params.get("query", "")
        max_results = params.get("max_results", 10)
        search_timeout = min(int(params.get("search_timeout", self._SEARCH_TIMEOUT)), 15)
        self._SEARCH_TIMEOUT = search_timeout

        if not query:
            return {"for_llm": {"error": "query is required"}, "for_ui": {}}

        logger.info(f"[QuickSearch] Starting search: query='{query}', max_results={max_results}")
        start_time = time.time()

        # 获取代理配置
        proxy_server = self._get_proxy_config()

        # 引擎选择策略：
        # - ddgs(无需浏览器，最快) + baidu + sina 作为基础组合
        # - 有代理时加入 google（需代理且最慢，但结果质量高）
        main_engines = ["duckduckgo", "baidu", "sina"]
        if proxy_server:
            main_engines.append("google")

        def _get_engine_proxy(engine_name: str) -> Optional[str]:
            engine_config = self.ENGINES.get(engine_name, {})
            needs_proxy = engine_config.get("requires_proxy", False)
            if needs_proxy and proxy_server:
                return proxy_server
            return None

        # 构建搜索任务：用 wait_for 包裹每个引擎，确保超时后能真正 cancel Playwright 协程
        _per_engine_timeout = self._SEARCH_TIMEOUT  # 每个引擎独立超时 = 总超时

        async def _run_with_timeout(coro, engine_name: str):
            try:
                return await asyncio.wait_for(coro, timeout=_per_engine_timeout)
            except asyncio.TimeoutError:
                logger.warning(f"[QuickSearch][{engine_name.upper()}] Timed out after {_per_engine_timeout}s")
                return []
            except Exception as e:
                logger.warning(f"[QuickSearch][{engine_name.upper()}] Error: {e}")
                return []

        search_tasks = {}
        for engine in main_engines:
            engine_proxy = _get_engine_proxy(engine)
            if engine == "google":
                coro = self._search_google_fallback(query, max_results, engine_proxy)
            elif engine == "baidu":
                coro = self._search_baidu_fallback(query, max_results, engine_proxy)
            elif engine == "duckduckgo":
                coro = self._search_duckduckgo_fast(query, max_results, engine_proxy)
            else:
                coro = self._search_with_engine(engine, query, max_results, engine_proxy)
            search_tasks[engine] = asyncio.ensure_future(_run_with_timeout(coro, engine))

        logger.info(f"[QuickSearch] Launching {len(search_tasks)} engines: {list(search_tasks.keys())}")

        # 等待所有引擎完成（每个已由 wait_for 独立超时保护，不再需要外层 cancel）
        all_tasks = set(search_tasks.values())
        done, pending = await asyncio.wait(all_tasks, timeout=self._SEARCH_TIMEOUT + 1)

        # 兜底：理论上 pending 应为空，保险起见仍 cancel
        for task in pending:
            task.cancel()
            engine_name = next((k for k, v in search_tasks.items() if v is task), "unknown")
            logger.warning(f"[QuickSearch][{engine_name.upper()}] Still pending after timeout+1s, force cancelled")

        # 合并 + 去重
        results = []
        searched_engines = []
        for engine in main_engines:
            task = search_tasks[engine]
            if task in done:
                try:
                    engine_results = task.result()
                    if isinstance(engine_results, Exception):
                        raise engine_results
                    if not engine_results or not isinstance(engine_results, list):
                        searched_engines.append(f"{engine}(empty)")
                        continue
                    new_results = []
                    for result in engine_results:
                        if not self._is_duplicate(result, results):
                            new_results.append(result)
                    results.extend(new_results)
                    searched_engines.append(engine)
                    logger.info(f"[QuickSearch][{engine.upper()}] {len(engine_results)} results, {len(new_results)} unique")
                except Exception as e:
                    logger.error(f"[QuickSearch][{engine.upper()}] Search failed: {e}")
                    searched_engines.append(f"{engine}(failed)")
            else:
                searched_engines.append(f"{engine}(timeout)")

        # 截断到 max_results
        deduplicated = results[:max_results]

        duration = time.time() - start_time
        logger.info(f"[QuickSearch] Completed in {duration:.2f}s. Total: {len(deduplicated)} results from {searched_engines}")

        # 构建 for_ui 每条结果字段：标题、URL、来源、时间、摘要
        ui_fields = []
        for idx, r in enumerate(deduplicated[:10]):
            _title = r.get("title", "") or "无标题"
            _link = r.get("link", "")
            _source = r.get("source", "")
            _date = r.get("date", "")
            _snippet = r.get("snippet", "")
            ui_fields.append({"label": f"[{idx+1}] 标题", "value": _title})
            ui_fields.append({"label": f"[{idx+1}] URL", "value": _link})
            if _source:
                ui_fields.append({"label": f"[{idx+1}] 来源", "value": _source})
            if _date:
                ui_fields.append({"label": f"[{idx+1}] 时间", "value": _date})
            if _snippet:
                ui_fields.append({"label": f"[{idx+1}] 摘要", "value": _snippet[:120]})
        # 兜底：无逐条字段时退化为摘要
        if not ui_fields:
            ui_fields = [
                {"label": "结果数", "value": len(deduplicated)},
                {"label": "搜索引擎", "value": ", ".join(searched_engines)},
                {"label": "耗时", "value": f"{duration:.1f}s"},
            ]

        return {
            "for_llm": {
                "query": query,
                "total_results": len(deduplicated),
                "searched_engines": searched_engines,
                "results": [
                    {"title": r.get("title"), "link": r.get("link"),
                     "snippet": r.get("snippet", ""), "source": r.get("source", ""),
                     "date": r.get("date", "")}
                    for r in deduplicated
                ]
            },
            "for_ui": {
                "components": [{
                    "component": "dynamic_card",
                    "data": {
                        "title": f"搜索: {query}（{len(deduplicated)} 条结果）",
                        "fields": ui_fields,
                    }
                }]
            }
        }

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
                            if proxy_server:
                                logger.info(f"[QuickSearch] Loaded proxy from config: {proxy_server}")
            except Exception as e:
                logger.warning(f"[QuickSearch] Failed to load proxy config: {e}")
        return proxy_server

    # ========== 搜索引擎方法 ==========

    async def _search_with_engine(self, engine_name: str, query: str, max_results_per_engine: int, proxy_server: str = None) -> List[Dict[str, Any]]:
        """使用指定搜索引擎进行搜索（复用共享浏览器池）"""
        if engine_name not in self.ENGINES:
            logger.warning(f"Engine {engine_name} not found, skipping")
            return []

        engine_config = self.ENGINES[engine_name]
        search_url = engine_config["url"].format(query=query)

        # 追加时间过滤参数
        try:
            _tr = self._get_time_range()
            search_url = self._apply_time_filter(search_url, engine_name, _tr)
        except Exception:
            pass

        results = []
        _use_patchright = PATCHRIGHT_AVAILABLE and engine_name in ("google", "duckduckgo")

        context = None
        try:
            # 从共享池获取浏览器
            browser = await _browser_pool.get_browser(_use_patchright, proxy_server)
            if _use_patchright:
                logger.info(f"[QuickSearch][{engine_name.upper()}] Using patchright (shared browser)")

            # 指纹生成 + Context 创建
            _fp = None
            if _use_patchright and BROWSERFORGE_AVAILABLE:
                try:
                    _fp = _bf_fp_generator.generate()
                except Exception:
                    pass

            if _fp:
                _fp_headers = {
                    k: v for k, v in (_fp.headers or {}).items()
                    if k.lower() not in ("user-agent", "host")
                }
                _fp_headers.setdefault("accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8")
                _fp_headers.setdefault("upgrade-insecure-requests", "1")
                _locale = "zh-TW" if engine_name == "google" else "zh-CN"
                _tz = "Asia/Taipei" if engine_name == "google" else "Asia/Shanghai"
                context = await browser.new_context(
                    user_agent=_fp.navigator.userAgent,
                    viewport={"width": _fp.screen.width, "height": _fp.screen.height},
                    locale=_locale, timezone_id=_tz,
                    java_script_enabled=True,
                    extra_http_headers=_fp_headers,
                )
            else:
                _client_hints_headers = {
                    "sec-ch-ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"macOS"',
                    "sec-ch-ua-platform-version": '"14.2.0"',
                    "sec-ch-ua-arch": '"x86"',
                    "sec-ch-ua-bitness": '"64"',
                    "sec-ch-ua-full-version-list": '"Not A(Brand";v="99.0.0.0", "Google Chrome";v="121.0.6167.85", "Chromium";v="121.0.6167.85"',
                    "sec-ch-ua-model": '""',
                    "sec-fetch-dest": "document",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-site": "none",
                    "sec-fetch-user": "?1",
                    "upgrade-insecure-requests": "1",
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "accept-encoding": "gzip, deflate, br",
                    "accept-language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                }
                if engine_name == "google":
                    context = await browser.new_context(
                        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                        viewport={"width": 1920, "height": 1080},
                        locale="zh-TW", timezone_id="Asia/Taipei",
                        java_script_enabled=True,
                        extra_http_headers=_client_hints_headers,
                    )
                else:
                    context = await browser.new_context(
                        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                        viewport={"width": 1920, "height": 1080},
                        locale="zh-CN", timezone_id="Asia/Shanghai",
                        extra_http_headers=_client_hints_headers,
                    )

            page = await context.new_page()

            # 反检测脚本注入
            _deferred_stealth_script = None
            if _use_patchright and _fp:
                try:
                    _hw = getattr(_fp.navigator, 'hardwareConcurrency', 8) or 8
                    _mem = getattr(_fp.navigator, 'deviceMemory', 8) or 8
                    _touch = getattr(_fp.navigator, 'maxTouchPoints', 0) or 0
                    _plat = getattr(_fp.navigator, 'platform', 'MacIntel') or 'MacIntel'
                    _vend = getattr(_fp.navigator, 'vendor', 'Google Inc.') or 'Google Inc.'
                    _langs = getattr(_fp.navigator, 'languages', None)
                    _langs_js = str(list(_langs)) if _langs else "['en-US', 'en']"
                    _gl_vendor = "Intel Inc."
                    _gl_renderer = "Intel Iris OpenGL Engine"
                    if hasattr(_fp, 'videoCard'):
                        _gl_vendor = getattr(_fp.videoCard, 'vendor', _gl_vendor) or _gl_vendor
                        _gl_renderer = getattr(_fp.videoCard, 'renderer', _gl_renderer) or _gl_renderer

                    _stealth_script = f"""
                        Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
                        Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {_hw} }});
                        Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {_mem} }});
                        Object.defineProperty(navigator, 'maxTouchPoints', {{ get: () => {_touch} }});
                        Object.defineProperty(navigator, 'platform', {{ get: () => "{_plat}" }});
                        Object.defineProperty(navigator, 'vendor', {{ get: () => "{_vend}" }});
                        Object.defineProperty(navigator, 'languages', {{ get: () => {_langs_js} }});
                        delete window.__playwright;
                        delete window.__pw_manual;
                        (function() {{
                            const _gp = WebGLRenderingContext.prototype.getParameter;
                            WebGLRenderingContext.prototype.getParameter = function(p) {{
                                if (p === 37445) return "{_gl_vendor}";
                                if (p === 37446) return "{_gl_renderer}";
                                return _gp.call(this, p);
                            }};
                            if (typeof WebGL2RenderingContext !== 'undefined') {{
                                const _gp2 = WebGL2RenderingContext.prototype.getParameter;
                                WebGL2RenderingContext.prototype.getParameter = function(p) {{
                                    if (p === 37445) return "{_gl_vendor}";
                                    if (p === 37446) return "{_gl_renderer}";
                                    return _gp2.call(this, p);
                                }};
                            }}
                        }})();
                        (function() {{
                            const _tdu = HTMLCanvasElement.prototype.toDataURL;
                            HTMLCanvasElement.prototype.toDataURL = function() {{
                                try {{
                                    const c = this.getContext('2d');
                                    if (c) {{
                                        const s = c.fillStyle;
                                        c.fillStyle = 'rgba('+Math.floor(Math.random()*256)+','+Math.floor(Math.random()*256)+','+Math.floor(Math.random()*256)+',0.01)';
                                        c.fillRect(0, 0, 1, 1);
                                        c.fillStyle = s;
                                    }}
                                }} catch(e) {{}}
                                return _tdu.apply(this, arguments);
                            }};
                        }})();
                    """
                    if proxy_server:
                        _deferred_stealth_script = _stealth_script
                    else:
                        await page.add_init_script(_stealth_script)
                except Exception:
                    pass
            elif _use_patchright:
                try:
                    _stealth_script = """
                        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                        (function() {
                            const _gp = WebGLRenderingContext.prototype.getParameter;
                            WebGLRenderingContext.prototype.getParameter = function(p) {
                                if (p === 37445) return "Intel Inc.";
                                if (p === 37446) return "Intel Iris OpenGL Engine";
                                return _gp.call(this, p);
                            };
                        })();
                        (function() {
                            const _tdu = HTMLCanvasElement.prototype.toDataURL;
                            HTMLCanvasElement.prototype.toDataURL = function() {
                                try {
                                    const c = this.getContext('2d');
                                    if (c) { const s=c.fillStyle; c.fillStyle='rgba('+Math.floor(Math.random()*256)+','+Math.floor(Math.random()*256)+','+Math.floor(Math.random()*256)+',0.01)'; c.fillRect(0,0,1,1); c.fillStyle=s; }
                                } catch(e) {}
                                return _tdu.apply(this, arguments);
                            };
                        })();
                    """
                    if proxy_server:
                        _deferred_stealth_script = _stealth_script
                    else:
                        await page.add_init_script(_stealth_script)
                except Exception:
                    pass
            else:
                await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {} };
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
                Object.defineProperty(navigator, 'plugins', {
                    get: () => {
                        const plugins = [
                            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                            { name: 'Native Client', filename: 'internal-nacl-plugin' }
                        ];
                        plugins.length = 3;
                        return plugins;
                    }
                });
                Object.defineProperty(navigator, 'languages', { get: () => ['zh-TW', 'zh', 'en-US', 'en'] });
                Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
                Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
                Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            """)

            # Google 使用首页策略（减少固定等待）
            if engine_name == "google":
                await page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=10000)
                # 等待搜索框出现而非固定等待
                try:
                    await page.wait_for_selector('input[name="q"], textarea[name="q"]', timeout=3000)
                except Exception:
                    await page.wait_for_timeout(500)

                if _deferred_stealth_script:
                    try:
                        await page.evaluate(_deferred_stealth_script)
                    except Exception:
                        pass

                search_box = await page.query_selector('input[name="q"], textarea[name="q"]')
                if search_box:
                    await search_box.fill(query)
                    await page.wait_for_timeout(300)
                    await search_box.press("Enter")
                    # 等待搜索结果出现，而非固定 5 秒
                    try:
                        await page.wait_for_selector("h3", timeout=10000)
                    except Exception:
                        pass

                    page_url = page.url
                    if "sorry" in page_url.lower():
                        logger.error(f"[QuickSearch][GOOGLE] CAPTCHA detected!")
                        await context.close()
                        return []

                    # 时间过滤
                    try:
                        _tr = self._get_time_range()
                        if _tr and _tr != 'none':
                            _cfg = self._load_time_filter_config()
                            _tbs = _cfg.get("time_range_params", {}).get(_tr, {}).get("google", "")
                            if _tbs and _tbs not in page_url:
                                _sep = "&" if "?" in page_url else "?"
                                _new_url = f"{page_url}{_sep}{_tbs}"
                                await page.goto(_new_url, wait_until="domcontentloaded", timeout=10000)
                                try:
                                    await page.wait_for_selector("h3", timeout=5000)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                else:
                    await page.goto(search_url, wait_until="commit", timeout=15000)
                    try:
                        await page.wait_for_selector("h3", timeout=8000)
                    except Exception:
                        pass
            else:
                goto_timeout = 5000 if engine_name == "sina" else 10000
                await page.goto(search_url, wait_until="domcontentloaded", timeout=goto_timeout)

                if _deferred_stealth_script:
                    try:
                        await page.evaluate(_deferred_stealth_script)
                    except Exception:
                        pass

            # 等待结果（非 Google，Google 已在上面等过了）
            if engine_name != "google":
                try:
                    if engine_name == "baidu":
                        await page.wait_for_selector(
                            "div.c-container, div.result, #content_left .result, div[class*='result']",
                            timeout=5000
                        )
                    elif engine_name == "sina":
                        await page.wait_for_selector(engine_config["result_selector"], timeout=4000)
                    else:
                        await page.wait_for_selector(engine_config["result_selector"], timeout=8000)
                except Exception:
                    pass

            elements = await page.query_selector_all(engine_config["result_selector"])

            for el in elements[:max_results_per_engine * 2]:
                try:
                    title_el = await el.query_selector(engine_config["title_selector"])
                    link_el = await el.query_selector(engine_config["link_selector"])

                    if not title_el or not link_el:
                        continue

                    title = (await title_el.inner_text()).strip()
                    if not title or len(title) < 5:
                        continue

                    link = await link_el.get_attribute("href")

                    # 百度真实链接
                    if engine_name == "baidu" and link:
                        real_link = await self._get_real_link_baidu(el, link)
                        if real_link:
                            link = real_link

                    snippet = ""
                    for sel in engine_config.get("snippet_selectors", []):
                        try:
                            snip_el = await el.query_selector(sel)
                            if snip_el:
                                snippet = (await snip_el.inner_text()).strip()
                                if snippet and len(snippet) > 15 and not re.match(r'^[\d\-\.\/\s]+$', snippet):
                                    break
                                else:
                                    snippet = ""
                        except:
                            continue

                    if not snippet:
                        try:
                            full_text = await el.inner_text()
                            full_text = full_text.replace(title, "").strip()
                            full_text = re.sub(r'\s+', ' ', full_text)
                            full_text = re.sub(r'^[\d\-\.\/\s]+', '', full_text)
                            if len(full_text) > 20:
                                snippet = full_text[:300]
                        except Exception:
                            pass

                    if title and link:
                        if self._should_filter(link, title):
                            continue

                        source = self._extract_source(link)
                        date = await self._extract_date(el, snippet)

                        results.append({
                            "title": title,
                            "link": link,
                            "snippet": snippet or "点击查看详情",
                            "source": source,
                            "date": date,
                            "engine": engine_name,
                        })
                except Exception:
                    continue

            # 关闭 context（不关闭 browser，留给池复用）
            await context.close()
            context = None

        except Exception as e:
            logger.error(f"[QuickSearch][{engine_name.upper()}] Search failed: {e}")
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

        return results

    def _search_google_with_selenium(self, query: str, max_results: int = 10, proxy_server: str = None) -> List[Dict[str, Any]]:
        """Selenium + undetected_chromedriver Google 搜索降级"""
        if not SELENIUM_AVAILABLE:
            return []

        results = []
        driver = None
        try:
            options = uc.ChromeOptions()
            if proxy_server:
                options.add_argument(f'--proxy-server={proxy_server}')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--window-size=1920,1080')
            options.add_argument('--disable-gpu')

            in_docker = os.path.exists('/.dockerenv') or os.environ.get('DOCKER_CONTAINER')
            if in_docker:
                options.add_argument('--headless=new')

            chrome_version = None
            try:
                import subprocess
                result = subprocess.run(['google-chrome', '--version'], capture_output=True, text=True)
                if result.returncode == 0:
                    chrome_version = int(result.stdout.strip().split()[-1].split('.')[0])
            except:
                pass

            driver = uc.Chrome(options=options, version_main=chrome_version, use_subprocess=True)
            search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
            driver.get(search_url)
            time.sleep(3)

            if "sorry" in driver.current_url.lower():
                return []

            h3_elements = driver.find_elements(By.TAG_NAME, "h3")
            for h3 in h3_elements[:max_results * 2]:
                try:
                    title = h3.text.strip()
                    if not title or len(title) < 5:
                        continue
                    try:
                        parent_a = h3.find_element(By.XPATH, "./ancestor::a")
                        link = parent_a.get_attribute("href")
                    except:
                        continue
                    if not link or "google.com" in link:
                        continue

                    snippet = ""
                    try:
                        container = h3.find_element(By.XPATH, "./ancestor::div[contains(@class, 'g') or @data-hveid]")
                        for sel in [".//div[contains(@class, 'VwiC3b')]", ".//div[contains(@style, '-webkit-line-clamp')]"]:
                            try:
                                snippet_el = container.find_element(By.XPATH, sel)
                                snippet = snippet_el.text.strip()
                                if snippet and len(snippet) > 20:
                                    break
                            except:
                                continue
                    except:
                        pass

                    results.append({
                        "title": title, "link": link,
                        "snippet": snippet or "点击查看详情",
                        "source": self._extract_source(link),
                        "engine": "google_selenium",
                    })
                    if len(results) >= max_results:
                        break
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"[QuickSearch][SELENIUM] Search failed: {e}")
        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass
        return results

    async def _search_google_fallback(self, query: str, max_results: int = 10, proxy_server: str = None) -> List[Dict[str, Any]]:
        """Google 降级链: Playwright -> Selenium"""
        playwright_results = await self._search_with_engine("google", query, max_results, proxy_server)
        if playwright_results:
            return playwright_results

        if SELENIUM_AVAILABLE:
            loop = asyncio.get_event_loop()
            selenium_results = await loop.run_in_executor(
                None, lambda: self._search_google_with_selenium(query, max_results, proxy_server)
            )
            if selenium_results:
                return selenium_results

        return []

    async def _search_duckduckgo_fast(self, query: str, max_results: int = 10, proxy: str = None) -> List[Dict[str, Any]]:
        """DuckDuckGo 快速搜索：优先 ddgs 库（无需浏览器），失败再 Playwright"""
        try:
            from duckduckgo_search import DDGS
            ddgs = DDGS(proxy=proxy, timeout=8)
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(None, lambda: list(ddgs.text(query, max_results=max_results)))
            if raw:
                results = []
                for r in raw:
                    href = r.get("href", "")
                    if not href:
                        continue
                    results.append({
                        "title": r.get("title", ""), "link": href,
                        "snippet": (r.get("body", "") or "")[:500],
                        "source": self._extract_source(href),
                        "engine": "duckduckgo", "date": "",
                    })
                if results:
                    return results
        except Exception as e:
            logger.warning(f"[QuickSearch][DUCKDUCKGO] ddgs library failed: {e}")

        return await self._search_with_engine("duckduckgo", query, max_results, proxy)

    async def _search_baidu_fallback(self, query: str, max_results: int = 10, proxy_server: str = None) -> List[Dict[str, Any]]:
        """Baidu 降级链: Playwright -> 搜狗"""
        baidu_results = await self._search_with_engine("baidu", query, max_results, proxy_server)
        if baidu_results:
            return baidu_results
        sogou_results = await self._search_with_engine("sogou", query, max_results, None)
        return sogou_results or []

    # ========== 辅助方法 ==========

    async def _get_real_link_baidu(self, element, baidu_link: str) -> Optional[str]:
        """尝试从百度结果中获取真实链接"""
        try:
            real_url = await element.get_attribute("mu")
            if real_url and real_url.startswith("http"):
                return real_url
            all_links = await element.query_selector_all("a[href]")
            for link in all_links:
                href = await link.get_attribute("href")
                if href and "baidu.com" not in href and href.startswith("http"):
                    return href
            cite_el = await element.query_selector("cite, span.c-showurl, a.c-showurl")
            if cite_el:
                cite_text = (await cite_el.inner_text()).strip()
                if cite_text and "." in cite_text:
                    if not cite_text.startswith("http"):
                        cite_text = "https://" + cite_text.split(" ")[0]
                    return cite_text
            return baidu_link
        except Exception:
            return baidu_link

    def _should_filter(self, link: str, title: str) -> bool:
        """判断是否应该过滤掉该结果"""
        if not link:
            return True
        for domain in self.FILTER_DOMAINS:
            if domain in link:
                return True
        filter_title_patterns = [
            r'股票行情.*走势图', r'股吧.*讨论', r'个股资金流向',
            r'股票价格_行情_走势', r'实时行情分析讨论', r'股吧-实时行情',
            r'_新浪股市汇', r'\(SH\d+\)\$',
        ]
        for pattern in filter_title_patterns:
            if re.search(pattern, title):
                return True
        return False

    def _extract_source(self, link: str) -> str:
        """从链接中提取来源名称"""
        if not link:
            return "未知来源"
        try:
            parsed = urlparse(link)
            domain = parsed.netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]
            for key, name in self.SOURCE_MAP.items():
                if key in domain:
                    return name
            parts = domain.split(".")
            if len(parts) >= 2:
                return parts[-2]
            return domain
        except Exception:
            return "未知来源"

    async def _extract_date(self, element, snippet: str) -> str:
        """从搜索结果中提取日期"""
        try:
            date_selectors = [
                "span.c-color-gray2", "span.c-color-gray",
                "span[class*='time']", "span[class*='date']",
                ".news-source span", ".source span",
            ]
            for sel in date_selectors:
                try:
                    date_el = await element.query_selector(sel)
                    if date_el:
                        date_text = (await date_el.inner_text()).strip()
                        if re.search(r'\d{1,2}月\d{1,2}日|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d+小时前|\d+分钟前|今天|昨天', date_text):
                            return date_text
                except:
                    continue
            if snippet:
                date_patterns = [
                    r'(\d{4}年\d{1,2}月\d{1,2}日)', r'(\d{1,2}月\d{1,2}日)',
                    r'(\d{4}-\d{1,2}-\d{1,2})', r'(\d+小时前)',
                    r'(\d+分钟前)', r'(今天|昨天)',
                ]
                for pattern in date_patterns:
                    match = re.search(pattern, snippet)
                    if match:
                        return match.group(1)
            return ""
        except Exception:
            return ""

    def _is_duplicate(self, new_result: Dict[str, Any], existing_results: List[Dict[str, Any]]) -> bool:
        """检查结果是否重复"""
        from difflib import SequenceMatcher
        new_link = new_result.get("link", "")
        new_title = new_result.get("title", "")
        for existing in existing_results:
            if new_link and new_link == existing.get("link", ""):
                return True
            if new_title and existing.get("title", ""):
                if SequenceMatcher(None, new_title, existing["title"]).ratio() > 0.9:
                    return True
        return False


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

    parser = argparse.ArgumentParser(description="Run QuickSearchSkill directly")
    parser.add_argument("--query", type=str, dest="query")
    args = parser.parse_args()
    for k, v in vars(args).items():
        if v is not None:
            params[k] = v

    async def run():
        skill = QuickSearchSkill()
        result = await skill.execute(params)
        out = result if isinstance(result, dict) else {"data": str(result)}
        print(json.dumps(out, ensure_ascii=False, default=str, indent=2))

    asyncio.run(run())


if __name__ == "__main__":
    _main()
