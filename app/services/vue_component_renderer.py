"""
Vue 组件 → 独立 HTML 渲染器

利用前端构建产物 (component-bundle.umd.js) 在独立 HTML 页面中
动态渲染 Vue 组件。

用途:
  - 钉钉渠道组件截图（Playwright 打开 HTML → 截图 PNG）
  - 组件交互预览链接（用户点击直接在浏览器中查看）

与前端 DynamicComponent.vue 共享同一个 component-registry.js，
保证 "改 Vue → HTML 输出自动跟随"。
"""
import json
import os
from typing import Any, Dict, Optional

from loguru import logger


class VueComponentRenderer:
    """
    Vue 组件 → 独立 HTML 渲染器

    生成一个自包含的 HTML 页面，引用:
      1. Vue 3 CDN
      2. 前端组件 bundle (component-bundle.umd.js)
      3. 组件 CSS (component-bundle.css)
    传入组件名 + 数据，即可渲染任意已注册的 Vue 组件。
    """

    def __init__(self):
        self._bundle_base_url = self._get_bundle_base_url()

    @staticmethod
    def _get_bundle_base_url() -> str:
        """
        获取组件 bundle 的基础 URL（绝对路径）

        bundle 由前端项目 `npm run build:components` 构建，
        通过 agent-service 的 /static/components/ 路由提供。

        使用绝对 URL（如 http://192.168.10.4:8000/static/components），
        因为 HTML 可能被上传到 MinIO 并从不同的 host 打开，
        相对路径 /static/components 会指向 MinIO 而非 agent-service。
        """
        # 优先使用环境变量指定的 URL（用于特殊部署场景）
        custom_url = os.getenv("COMPONENT_BUNDLE_BASE_URL")
        if custom_url:
            return custom_url.rstrip("/")

        # 使用绝对 URL，确保从 MinIO 打开的 HTML 也能加载 bundle
        # 使用 get_local_ip() 而非 get_external_host()
        # 因为 AGENT_EXTERNAL_HOST 可能是 localhost，
        # 而 HTML 通过 MinIO 外部访问，需要真实 IP
        from app.services.file_generator import get_local_ip, get_service_port
        host = get_local_ip()
        port = get_service_port()
        return f"http://{host}:{port}/static/components"

    def render_html(
        self,
        component: str,
        data: Dict[str, Any],
        title: str = "",
        width: int = 1200,
        theme: str = "light",
    ) -> str:
        """
        生成包含 Vue 组件的独立 HTML 页面

        Args:
            component: 组件名称（V4UISelector 输出的，如 "data_table"）
            data: 组件数据（V4UISelector 输出的 data 字段）
            title: 页面标题
            width: 渲染宽度（px）
            theme: 主题 "light" / "dark"

        Returns:
            完整 HTML 字符串
        """
        data_json = json.dumps(data, ensure_ascii=False, default=str)
        component_json = json.dumps(component)
        title_escaped = (title or component).replace("<", "&lt;").replace(">", "&gt;")

        bundle_js_url = f"{self._bundle_base_url}/component-bundle.umd.js"
        main_css_url = f"{self._bundle_base_url}/main.css"
        bundle_css_url = f"{self._bundle_base_url}/component-bundle.css"
        vue_js_url = f"{self._bundle_base_url}/vue.global.prod.js"

        bg_color = "#1a1a2e" if theme == "dark" else "#ffffff"
        text_color = "#e2e8f0" if theme == "dark" else "#1e293b"

        logger.info(
            f"[VueComponentRenderer] Rendering component={component}, "
            f"data_size={len(data_json)}, bundle={bundle_js_url}"
        )

        return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title_escaped}</title>
    <!-- 全局样式（CSS 变量 + 工具类，组件依赖） -->
    <link rel="stylesheet" href="{main_css_url}" onerror="console.warn('main.css not found, proceeding without it')">
    <!-- 组件 bundle CSS（ECharts/样式等） -->
    <link rel="stylesheet" href="{bundle_css_url}" onerror="console.warn('component-bundle.css not found, proceeding without it')">

    <!-- 可视化库预加载（供组件内按需使用） -->
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
        #app {{
            max-width: {width}px;
            margin: 0 auto;
        }}
        .vue-loading {{
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            padding: 40px;
            color: #94a3b8;
            font-size: 14px;
        }}
        .vue-loading .spinner {{
            width: 20px; height: 20px;
            border: 2px solid #e2e8f0;
            border-top-color: #3b82f6;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        .render-error {{
            color: #ef4444;
            padding: 20px;
            border: 1px solid #fecaca;
            border-radius: 8px;
            background: #fff5f5;
            font-size: 14px;
        }}
        .render-error .error-title {{
            font-weight: 600;
            margin-bottom: 8px;
        }}
    </style>
</head>
<body>
    <div id="app">
        <div class="vue-loading">
            <div class="spinner"></div>
            <span>组件加载中...</span>
        </div>
    </div>

    <!-- Vue 3 Runtime -->
    <script src="{vue_js_url}"></script>

    <!-- 前端组件 Bundle (包含所有组件定义 + 依赖) -->
    <script src="{bundle_js_url}"></script>

    <script>
    (function() {{
        var componentName = {component_json};
        var componentData = {data_json};

        // 检查 bundle 是否加载成功
        var Components = window.SthgComponents;
        if (!Components) {{
            document.getElementById('app').innerHTML =
                '<div class="render-error">' +
                '<div class="error-title">组件库加载失败</div>' +
                '<div>请确认 component-bundle.umd.js 已正确构建和部署</div>' +
                '</div>';
            document.body.setAttribute('data-render-error', 'bundle-not-loaded');
            return;
        }}

        // 等待组件解析完成（bundle 中组件是异步 resolve 的）
        var readyPromise = Components._readyPromise || Promise.resolve();
        readyPromise.then(function() {{
            var compMap = Components.componentMap || {{}};
            var targetComponent = compMap[componentName];

            if (!targetComponent) {{
                // 降级：尝试 dynamic_card
                targetComponent = compMap['dynamic_card'];
            }}

            if (!targetComponent) {{
                document.getElementById('app').innerHTML =
                    '<div class="render-error">' +
                    '<div class="error-title">组件未注册: ' + componentName + '</div>' +
                    '<div>请在 component-registry.js 中添加该组件</div>' +
                    '</div>';
                document.body.setAttribute('data-render-error', 'component-not-found');
                return;
            }}

            // 创建 Vue app 并挂载
            var app = Vue.createApp({{
                render: function() {{
                    return Vue.h(this.comp, {{ data: this.compData }});
                }},
                data: function() {{
                    return {{
                        comp: Vue.markRaw(targetComponent),
                        compData: componentData
                    }};
                }}
            }});

            app.mount('#app');

            // 渲染完成标记（供 Playwright 检测）
            // 给组件内部的异步操作（ECharts 初始化等）一些时间
            setTimeout(function() {{
                document.body.setAttribute('data-rendered', 'true');
            }}, 800);

        }}).catch(function(err) {{
            document.getElementById('app').innerHTML =
                '<div class="render-error">' +
                '<div class="error-title">组件解析失败</div>' +
                '<div>' + String(err) + '</div>' +
                '</div>';
            document.body.setAttribute('data-render-error', 'resolve-failed');
        }});
    }})();
    </script>
</body>
</html>'''
