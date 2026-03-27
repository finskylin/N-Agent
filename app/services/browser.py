"""
Browser Service for Web Crawling & Searching
Wraps Playwright with Proxy Support and Search Engine Logic
"""
import asyncio
import logging
from typing import Dict, Any, List, Optional
from urllib.parse import quote

# 尝试导入 Playwright
try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

logger = logging.getLogger(__name__)

class BrowserService:
    def __init__(self, proxy_url: str = "http://127.0.0.1:7890"):
        self.proxy_url = proxy_url
        self.browser: Optional[Browser] = None
        self.playwright = None
        
    async def _init_browser(self, headless: bool = True):
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError("Playwright is not installed. Please install it.")
            
        if not self.browser:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=headless,
                proxy={"server": self.proxy_url} if self.proxy_url else None,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def search(self, query: str, engine: str = "google", limit: int = 5) -> List[Dict[str, str]]:
        """
        Perform a search on a search engine.
        Engines: google (default, priority), baidu, sina, bing
        All searches go through proxy 127.0.0.1:7890
        """
        await self._init_browser()
        context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        results = []
        
        try:
            if engine == "google":
                url = f"https://www.google.com/search?q={quote(query)}"
                selector = "div.g"
                title_sel = "h3"
                link_sel = "a"
                desc_sel = "div.VwiC3b" # dynamic, might change
                
            elif engine == "baidu":
                url = f"https://www.baidu.com/s?wd={quote(query)}"
                selector = "div.result.c-container"
                title_sel = "h3.t"
                link_sel = "a"
                
            elif engine == "sina":
                 # Sina search often redirects or is complex, usually just news
                 url = f"https://search.sina.com.cn/?q={quote(query)}&c=news"
                 selector = "div.box-result"
                 title_sel = "h2 a"
                 link_sel = "h2 a"
                 
            else:
                # Default to Google
                url = f"https://www.google.com/search?q={quote(query)}"
                selector = "div.g"
                title_sel = "h3"
                link_sel = "a"

            logger.info(f"Searching {engine}: {url}")
            await page.goto(url, timeout=30000)
            await page.wait_for_load_state("domcontentloaded")
            
            # Simple extraction logic
            elements = await page.query_selector_all(selector)
            
            for el in elements[:limit]:
                try:
                    title_el = await el.query_selector(title_sel)
                    link_el = await el.query_selector(link_sel)
                    
                    if title_el and link_el:
                        title = await title_el.inner_text()
                        href = await link_el.get_attribute("href")
                        
                        if href and href.startswith("http"):
                            results.append({
                                "title": title,
                                "url": href,
                                "source": engine
                            })
                except Exception as e:
                    continue
                    
        except Exception as e:
            logger.error(f"Search failed: {e}")
            results.append({"error": str(e)})
            
        finally:
            await page.close()
            await context.close()
            
        return results

    async def crawl_page(self, url: str) -> Dict[str, Any]:
        """Crawl a specific page content"""
        await self._init_browser()
        page = await self.browser.new_page()
        data = {"url": url}
        
        try:
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            
            data["title"] = await page.title()
            # Clean text extraction
            data["content"] = await page.evaluate("""() => {
                return document.body.innerText;
            }""")
            data["html"] = await page.content()
            
        except Exception as e:
            data["error"] = str(e)
            
        finally:
            await page.close()
            
        return data

# Singleton or factory usage
browser_service = BrowserService(proxy_url="http://127.0.0.1:7890")
