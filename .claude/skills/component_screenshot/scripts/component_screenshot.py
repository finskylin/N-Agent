"""
Component Screenshot Skill
组件截图技能

将任意 UI 组件数据渲染为 PNG 截图。
流程: V4UISelector 数据 → VueComponentRenderer 生成 HTML → 保存文件
      → agent-service 本地 Playwright 截图 → 可选上传钉钉

与前端 DynamicComponent.vue 共享同一个 component-registry.js，
保证 "改 Vue 组件 → HTML/PNG 输出自动跟随变化"。
"""

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse, quote

from loguru import logger


COMPONENTS_DIR = Path("app/data/generated/components")
SCREENSHOTS_DIR = Path("app/data/generated/screenshots")


# ── file_storage / URL helpers (no app dependency) ───────────────────────────

def _get_public_base_url() -> str:
    base = os.getenv("AGENT_PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        return base
    host = os.getenv("AGENT_EXTERNAL_HOST", "127.0.0.1")
    port = os.getenv("AGENT_SERVICE_PORT", "8000")
    return f"http://{host}:{port}"


def _get_service_port() -> int:
    try:
        return int(os.getenv("AGENT_SERVICE_PORT", os.getenv("PORT", "8000")))
    except ValueError:
        return 8000


def _build_download_url_for_path(path: Path) -> str:
    """为本地文件路径生成统一下载 URL。"""
    store_dir = Path(os.getenv("LOCAL_OBJECT_STORE_DIR", "data/object_storage")).resolve()
    gen_dir = Path(os.getenv("LOCAL_GENERATED_FILES_DIR", "data/generated")).resolve()
    resolved = path.resolve()
    for prefix, root in [("object_storage", store_dir), ("generated", gen_dir)]:
        try:
            relative = resolved.relative_to(root)
            token = f"{prefix}/{relative.as_posix()}"
            return f"{_get_public_base_url()}/api/files/download?path={quote(token, safe='/:_-.()')}"
        except ValueError:
            continue
    # fallback: just expose the filename
    return f"{_get_public_base_url()}/api/files/download/{path.name}"


def _get_bundle_base_url() -> str:
    custom_url = os.getenv("COMPONENT_BUNDLE_BASE_URL")
    if custom_url:
        return custom_url.rstrip("/")
    # Use env-based IP detection
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        host = s.getsockname()[0]
        s.close()
    except Exception:
        host = os.getenv("AGENT_EXTERNAL_HOST", "127.0.0.1")
    port = _get_service_port()
    return f"http://{host}:{port}/static/components"


def _render_vue_html(component: str, data: Dict[str, Any], title: str = "", width: int = 1200, theme: str = "light") -> str:
    """Inline VueComponentRenderer.render_html — no app dependency."""
    bundle_base = _get_bundle_base_url()
    data_json = json.dumps(data, ensure_ascii=False, default=str)
    component_json = json.dumps(component)
    title_escaped = (title or component).replace("<", "&lt;").replace(">", "&gt;")

    bundle_js_url = f"{bundle_base}/component-bundle.umd.js"
    main_css_url = f"{bundle_base}/main.css"
    bundle_css_url = f"{bundle_base}/component-bundle.css"
    vue_js_url = f"{bundle_base}/vue.global.prod.js"

    bg_color = "#1a1a2e" if theme == "dark" else "#ffffff"
    text_color = "#e2e8f0" if theme == "dark" else "#1e293b"

    logger.info(
        f"[ComponentScreenshot] Rendering component={component}, "
        f"data_size={len(data_json)}, bundle={bundle_js_url}"
    )

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title_escaped}</title>
    <link rel="stylesheet" href="{main_css_url}" onerror="console.warn('main.css not found, proceeding without it')">
    <link rel="stylesheet" href="{bundle_css_url}" onerror="console.warn('component-bundle.css not found, proceeding without it')">
    <script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"></script>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://cdn.jsdelivr.net/npm/highlight.js@11/highlight.min.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                         "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif;
            background: {bg_color};
            color: {text_color};
            padding: 20px;
        }}
        #app {{ max-width: {width}px; margin: 0 auto; }}
        .vue-loading {{
            display: flex; align-items: center; justify-content: center;
            gap: 10px; padding: 40px; color: #94a3b8; font-size: 14px;
        }}
        .vue-loading .spinner {{
            width: 20px; height: 20px;
            border: 2px solid #e2e8f0; border-top-color: #3b82f6;
            border-radius: 50%; animation: spin 0.8s linear infinite;
        }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        .render-error {{
            color: #ef4444; padding: 20px; border: 1px solid #fecaca;
            border-radius: 8px; background: #fff5f5; font-size: 14px;
        }}
        .render-error .error-title {{ font-weight: 600; margin-bottom: 8px; }}
    </style>
</head>
<body>
    <div id="app">
        <div class="vue-loading">
            <div class="spinner"></div>
            <span>组件加载中...</span>
        </div>
    </div>
    <script src="{vue_js_url}"></script>
    <script src="{bundle_js_url}"></script>
    <script>
    (function() {{
        var componentName = {component_json};
        var componentData = {data_json};
        var Components = window.SthgComponents;
        if (!Components) {{
            document.getElementById('app').innerHTML =
                '<div class="render-error"><div class="error-title">组件库加载失败</div>' +
                '<div>请确认 component-bundle.umd.js 已正确构建和部署</div></div>';
            document.body.setAttribute('data-render-error', 'bundle-not-loaded');
            return;
        }}
        var readyPromise = Components._readyPromise || Promise.resolve();
        readyPromise.then(function() {{
            var compMap = Components.componentMap || {{}};
            var targetComponent = compMap[componentName] || compMap['dynamic_card'];
            if (!targetComponent) {{
                document.getElementById('app').innerHTML =
                    '<div class="render-error"><div class="error-title">组件未注册: ' + componentName + '</div>' +
                    '<div>请在 component-registry.js 中添加该组件</div></div>';
                document.body.setAttribute('data-render-error', 'component-not-found');
                return;
            }}
            var app = Vue.createApp({{
                render: function() {{
                    return Vue.h(this.comp, {{ data: this.compData }});
                }},
                data() {{ return {{ comp: targetComponent, compData: componentData }}; }},
            }});
            app.mount('#app');
            document.body.setAttribute('data-rendered', 'true');
        }}).catch(function(err) {{
            document.getElementById('app').innerHTML =
                '<div class="render-error"><div class="error-title">组件渲染失败</div>' +
                '<div>' + String(err) + '</div></div>';
            document.body.setAttribute('data-render-error', String(err));
        }});
    }})();
    </script>
</body>
</html>'''


async def _dingtalk_upload_image(image_bytes: bytes, filename: str = "image.png") -> Optional[str]:
    """Inline DingTalkUploader.upload_image — no app dependency."""
    import httpx

    # Get access token
    client_id = os.getenv("DINGTALK_CLIENT_ID", "")
    client_secret = os.getenv("DINGTALK_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        logger.warning("[ComponentScreenshot] DingTalk credentials not configured")
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                json={"appKey": client_id, "appSecret": client_secret},
            )
            access_token = resp.json().get("accessToken", "")
    except Exception as e:
        logger.error(f"[ComponentScreenshot] DingTalk token error: {e}")
        return None

    if not access_token:
        return None

    ext = os.path.splitext(filename)[1].lower()
    content_type_map = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
    }
    content_type = content_type_map.get(ext, "application/octet-stream")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://oapi.dingtalk.com/media/upload",
                params={"access_token": access_token, "type": "image"},
                files={"media": (filename, image_bytes, content_type)},
            )
            response.raise_for_status()
            data = response.json()
            if data.get("errcode", 0) != 0:
                logger.error(f"[ComponentScreenshot] DingTalk upload error: {data}")
                return None
            return data.get("url") or data.get("media_id")
    except Exception as e:
        logger.error(f"[ComponentScreenshot] DingTalk upload failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────


class ComponentScreenshot:
    """
    组件截图服务

    通过内联 VueComponentRenderer 将组件数据渲染为 HTML，
    保存为独立 HTML 文件，然后通过 agent-service 本地 Playwright 截图为 PNG。
    """

    @staticmethod
    def _ensure_dirs() -> None:
        COMPONENTS_DIR.mkdir(parents=True, exist_ok=True)
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _slugify_name(name: str, default: str = "html_page") -> str:
        cleaned = re.sub(r"[^0-9A-Za-z_-]+", "_", (name or "").strip())
        cleaned = cleaned.strip("_")
        return cleaned[:48] or default

    @staticmethod
    def _build_public_download_url(path: Path) -> str:
        return _build_download_url_for_path(path)

    @staticmethod
    def _build_loopback_download_url(path: Path) -> str:
        public_url = ComponentScreenshot._build_public_download_url(path)
        parsed = urlparse(public_url)
        if parsed.path != "/api/files/download":
            return public_url
        path_and_query = parsed.path
        if parsed.query:
            path_and_query = f"{path_and_query}?{parsed.query}"
        return f"http://127.0.0.1:{_get_service_port()}{path_and_query}"

    @staticmethod
    def normalize_html_url_for_toolbox(html_url: str) -> str:
        """
        兼容旧方法名：将 HTML 地址规范化为 agent-service 本地 Playwright 可访问地址。
        """
        if not html_url:
            return ""

        normalized = html_url.strip()
        parsed = urlparse(normalized)
        path_and_query = parsed.path or ""
        if parsed.query:
            path_and_query = f"{path_and_query}?{parsed.query}"

        if path_and_query.startswith("/api/files/download"):
            external_host = os.getenv("AGENT_EXTERNAL_HOST", "127.0.0.1")
            service_port = _get_service_port()
            if parsed.hostname in {None, external_host, "127.0.0.1", "localhost", "host.docker.internal"}:
                return f"http://127.0.0.1:{service_port}{path_and_query}"

        return normalized

    @staticmethod
    def render_html(component: str, data: Dict[str, Any], title: str = "") -> Dict[str, Any]:
        ComponentScreenshot._ensure_dirs()

        html_content = _render_vue_html(component=component, data=data, title=title)

        token = uuid.uuid4().hex[:12]
        html_filename = f"{component}_{token}.html"
        html_path = COMPONENTS_DIR / html_filename
        html_path.write_text(html_content, encoding="utf-8")

        logger.info(f"[ComponentScreenshot] HTML rendered: {html_path} ({len(html_content)} bytes)")

        return {
            "html_path": str(html_path),
            "html_url": ComponentScreenshot._build_public_download_url(html_path),
            "internal_url": ComponentScreenshot._build_loopback_download_url(html_path),
            "token": token,
        }

    @staticmethod
    async def screenshot_via_toolbox(
        html_url: str,
        token: str,
        component: str = "component",
        wait_seconds: int = 3,
    ) -> Dict[str, Any]:
        """
        兼容旧方法名：通过 agent-service 本地 Playwright 截图。
        """
        return await ComponentScreenshot._screenshot_direct(
            html_url=html_url,
            token=token,
            component=component,
            wait_seconds=wait_seconds,
            ready_selector=None,
        )

    @staticmethod
    async def _screenshot_direct(
        html_url: str,
        token: str,
        component: str = "component",
        wait_seconds: int = 3,
        ready_selector: Optional[str] = '[data-rendered="true"]',
    ) -> Dict[str, Any]:
        normalized_url = ComponentScreenshot.normalize_html_url_for_toolbox(html_url)
        screenshot_filename = f"{component}_{token}.png"
        screenshot_path = SCREENSHOTS_DIR / screenshot_filename

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {
                "success": False,
                "error": "Playwright 未安装。请在 agent-service 环境中安装 playwright 并执行 playwright install chromium",
            }

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    device_scale_factor=2,
                )
                page = await context.new_page()
                await page.goto(normalized_url, wait_until="networkidle", timeout=30000)

                render_error = await page.get_attribute("body", "data-render-error")
                if render_error:
                    await context.close()
                    await browser.close()
                    return {"success": False, "error": f"Component render failed: {render_error}"}

                if ready_selector:
                    try:
                        await page.wait_for_selector(ready_selector, timeout=10000)
                    except Exception:
                        logger.info(
                            f"[ComponentScreenshot] Ready selector timeout, fallback to fixed wait: {ready_selector}"
                        )
                        await asyncio.sleep(wait_seconds)
                else:
                    await asyncio.sleep(wait_seconds)

                await asyncio.sleep(1)
                await page.screenshot(path=str(screenshot_path), full_page=True)
                await context.close()
                await browser.close()

            return {
                "screenshot_path": str(screenshot_path),
                "screenshot_url": ComponentScreenshot._build_public_download_url(screenshot_path),
                "success": True,
            }
        except Exception as exc:
            logger.error(f"[ComponentScreenshot] Screenshot failed: {exc}")
            return {"success": False, "error": str(exc)}

    @staticmethod
    async def screenshot_html_url(
        html_url: str,
        title: str = "",
        upload_to_dingtalk: bool = False,
        wait_seconds: int = 2,
    ) -> Dict[str, Any]:
        if not html_url:
            return {"success": False, "error": "html_url is required"}

        ComponentScreenshot._ensure_dirs()
        start_time = time.time()
        token = uuid.uuid4().hex[:12]
        component_name = ComponentScreenshot._slugify_name(title or "html_page")
        normalized_url = ComponentScreenshot.normalize_html_url_for_toolbox(html_url)

        screenshot_result = await ComponentScreenshot._screenshot_direct(
            html_url=normalized_url,
            token=token,
            component=component_name,
            wait_seconds=wait_seconds,
            ready_selector=None,
        )

        result = {
            "interactive_url": html_url,
            "normalized_html_url": normalized_url,
            "success": screenshot_result.get("success", False),
        }

        if screenshot_result.get("success"):
            result["screenshot_path"] = screenshot_result.get("screenshot_path", "")
            result["screenshot_url"] = screenshot_result.get("screenshot_url", "")

            if upload_to_dingtalk:
                try:
                    screenshot_path = screenshot_result.get("screenshot_path", "")
                    image_bytes = Path(screenshot_path).read_bytes() if screenshot_path else None
                    if image_bytes:
                        dingtalk_url = await _dingtalk_upload_image(
                            image_bytes,
                            f"{component_name}_{token}.png",
                        )
                        if dingtalk_url:
                            result["dingtalk_image_url"] = dingtalk_url
                except Exception as exc:
                    logger.warning(f"[ComponentScreenshot] DingTalk upload failed for html_url screenshot: {exc}")

        elapsed = time.time() - start_time
        result["elapsed_seconds"] = round(elapsed, 1)
        logger.info(
            f"[ComponentScreenshot] screenshot_html_url completed: "
            f"success={result['success']}, elapsed={elapsed:.1f}s, html_url={html_url}"
        )
        return result

    @staticmethod
    async def render_and_screenshot(
        component: str,
        data: Dict[str, Any],
        title: str = "",
        upload_to_dingtalk: bool = False,
    ) -> Dict[str, Any]:
        start_time = time.time()

        html_result = ComponentScreenshot.render_html(component, data, title)
        if "error" in html_result:
            return {"success": False, "error": html_result["error"]}

        token = html_result["token"]
        wait_seconds = 5 if "map" in component else 3
        screenshot_result = await ComponentScreenshot._screenshot_direct(
            html_url=html_result["internal_url"],
            token=token,
            component=component,
            wait_seconds=wait_seconds,
        )

        result = {
            "interactive_url": html_result["html_url"],
            "html_path": html_result["html_path"],
            "success": screenshot_result.get("success", False),
        }

        if screenshot_result.get("success"):
            result["screenshot_path"] = screenshot_result.get("screenshot_path", "")
            result["screenshot_url"] = screenshot_result.get("screenshot_url", "")

            if upload_to_dingtalk:
                try:
                    screenshot_path = screenshot_result.get("screenshot_path", "")
                    image_bytes = Path(screenshot_path).read_bytes() if screenshot_path else None
                    if image_bytes:
                        dingtalk_url = await _dingtalk_upload_image(
                            image_bytes,
                            f"{component}_{token}.png",
                        )
                        if dingtalk_url:
                            result["dingtalk_image_url"] = dingtalk_url
                            logger.info(f"[ComponentScreenshot] Uploaded to DingTalk: {dingtalk_url[:60]}...")
                except Exception as exc:
                    logger.warning(f"[ComponentScreenshot] DingTalk upload failed: {exc}")

        elapsed = time.time() - start_time
        result["elapsed_seconds"] = round(elapsed, 1)
        logger.info(
            f"[ComponentScreenshot] render_and_screenshot completed: "
            f"component={component}, success={result['success']}, elapsed={elapsed:.1f}s"
        )
        return result
