"""
全技能 HTML 页面渲染器 (SkillPageRenderer)

将一次 Agent 分析产出的所有 component_for_render 事件，
合并渲染为一个 MinIO 托管的静态 HTML 页面。

HTML 中的 JS/CSS 依赖全部引用 MinIO 上已有的 bundle 文件（启动时上传），
确保同域加载，无 CORS 问题。

用途:
  - 钉钉渠道：消息末尾附带 "查看完整分析页面" 链接
  - Web 前端：可直接嵌入 iframe 或新窗口打开

与 VueComponentRenderer（单组件 HTML）的区别:
  - VueComponentRenderer: 一个 HTML 页面渲染一个组件
  - SkillPageRenderer: 一个 HTML 页面渲染所有技能组件
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger


# 单页最多渲染的组件数量，避免页面过重
_MAX_COMPONENTS = 20


class SkillPageRenderer:
    """
    全技能 HTML 页面渲染器

    将 component_events[] 合并为单页 HTML 并上传 MinIO。
    包含各技能获取的数据组件 + LLM 完整分析报告。
    """

    def __init__(self):
        self._bundle_base_url = self._get_bundle_base_url()

    @staticmethod
    def _get_bundle_base_url() -> str:
        """
        获取组件 bundle 的 MinIO URL

        复用 VueComponentRenderer 的逻辑:
        1. 优先读 COMPONENT_BUNDLE_BASE_URL 环境变量 (启动时设置的 MinIO 地址)
        2. 降级到 agent-service 本地 static 路径
        """
        custom_url = os.getenv("COMPONENT_BUNDLE_BASE_URL")
        if custom_url:
            return custom_url.rstrip("/")

        try:
            from app.services.file_generator import get_local_ip, get_service_port
            # 使用 get_local_ip() 而非 get_external_host()
            # 因为 AGENT_EXTERNAL_HOST 可能是 localhost，
            # 而 skill page 通过 MinIO 外部访问，需要真实 IP
            host = get_local_ip()
            port = get_service_port()
            return f"http://{host}:{port}/static/components"
        except Exception:
            return "/static/components"

    def render(
        self,
        component_events: List[Dict[str, Any]],
        user_id: int,
        session_id: str,
        title: str = "",
        report_markdown: str = "",
        scene_type: str = "default",
        scene_tab: Dict[str, Any] = None,
        scene_updates: List[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        渲染并上传全技能 HTML 页面

        Args:
            component_events: bot_handler 收集的 component_for_render 事件列表
                每个元素结构: {"component": "stock_info_card", "data": {...}, "title": "..."}
            user_id: 用户 ID
            session_id: 会话 ID
            title: 页面标题
            report_markdown: LLM 输出的完整分析报告 Markdown 文本
            scene_type: 场景类型 (default, map, stock, intel, ...)
            scene_tab: 场景画布配置 (canvas_type, canvas_config 等)
            scene_updates: 场景图层/面板更新列表

        Returns:
            {"page_url": "http://...", "component_count": N, "success": True}
            失败时 {"success": False, "error": "..."}
        """
        if not component_events:
            return {"success": False, "error": "No component events to render"}

        try:
            # 限制组件数量
            events_to_render = component_events[:_MAX_COMPONENTS]

            # 生成 HTML
            html_content = self._build_html(
                component_events=events_to_render,
                title=title,
                report_markdown=report_markdown,
                scene_type=scene_type,
                scene_tab=scene_tab,
                scene_updates=scene_updates,
            )

            # 上传到 MinIO
            ts = int(time.time())
            safe_session = session_id.replace("/", "_").replace("\\", "_") if session_id else "unknown"
            file_name = f"pages/{safe_session}_{ts}.html"

            from app.services.file_generator import upload_file_to_local_storage
            page_url = upload_file_to_local_storage(
                file_data=html_content.encode("utf-8"),
                user_id=user_id,
                file_name=file_name,
                content_type="text/html; charset=utf-8",
            )

            if not page_url:
                return {"success": False, "error": "MinIO upload failed"}

            logger.info(
                f"[SkillPageRenderer] Page uploaded: {page_url} "
                f"(components={len(events_to_render)}, html_size={len(html_content)})"
            )

            return {
                "page_url": page_url,
                "component_count": len(events_to_render),
                "success": True,
            }

        except Exception as e:
            logger.error(f"[SkillPageRenderer] Render failed: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return {"success": False, "error": str(e)}

    @staticmethod
    def _load_js_preloads() -> str:
        """Load JS library tags from app/config/ui/js_preloads.json"""
        try:
            config_path = Path(__file__).resolve().parents[1] / "config" / "ui" / "js_preloads.json"
            if not config_path.exists():
                # Try from project root
                config_path = Path(__file__).resolve().parents[2] / "app" / "config" / "ui" / "js_preloads.json"
            if config_path.exists():
                import json as _json
                config = _json.loads(config_path.read_text(encoding="utf-8"))
                tags = []
                for lib in config.get("libraries", []):
                    if "css" in lib:
                        tags.append(f'    <link rel="stylesheet" href="{lib["css"]}"/>')
                    if "js" in lib:
                        tags.append(f'    <script src="{lib["js"]}"></script>')
                return "\n".join(tags)
        except Exception as e:
            logger.debug(f"[SkillPageRenderer] Failed to load JS preloads: {e}")
        # Fallback to hardcoded (all versions pinned)
        return """    <script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"></script>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/highlight.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.3/dist/mermaid.min.js"></script>"""

    @staticmethod
    def _load_tile_sources() -> str:
        """Load tile sources from app/config/ui/tile_sources.json as JSON string"""
        try:
            config_path = Path(__file__).resolve().parents[1] / "config" / "ui" / "tile_sources.json"
            if not config_path.exists():
                config_path = Path(__file__).resolve().parents[2] / "app" / "config" / "ui" / "tile_sources.json"
            if config_path.exists():
                return config_path.read_text(encoding="utf-8")
        except Exception:
            pass
        return '{}'

    @staticmethod
    def _classify_components(
        component_events: List[Dict[str, Any]],
        scene_type: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        根据 component_events 上的 layout_hint 分类组件。

        Returns:
            {
                "map_layers": [...],   # role=poi/layer → 地图图层数据
                "overlays": [...],     # role=overlay → 浮动面板 (Vue 组件)
                "standalone": [...],   # 其他 → 纵向卡片
            }
        """
        result = {"map_layers": [], "overlays": [], "standalone": []}
        if scene_type == "default":
            result["standalone"] = list(component_events)
            return result

        for evt in component_events:
            hint = evt.get("layout_hint", {})
            role = hint.get("role", "standalone") if hint else "standalone"
            if role in ("poi", "layer"):
                result["map_layers"].append(evt)
            elif role == "overlay":
                result["overlays"].append(evt)
            else:
                result["standalone"].append(evt)

        return result

    @staticmethod
    def _extract_geo_layers_from_components(
        map_layer_events: List[Dict[str, Any]],
        overlay_events: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        从 poi/layer/overlay 组件的 data 中提取可渲染的地图图层数据。
        支持: geojson, markers, polyline, map_data, geo_distribution 等字段。
        """
        layers = []
        all_events = list(map_layer_events) + list(overlay_events)
        for evt in all_events:
            data = evt.get("data", {})
            if not isinstance(data, dict):
                continue
            layer = {}
            # 直接的 geojson 字段
            if "geojson" in data:
                layer["geojson"] = data["geojson"]
            # geo_distribution（geopolitical_security skill 返回的 GeoJSON）
            if "geo_distribution" in data and isinstance(data["geo_distribution"], dict):
                geo = data["geo_distribution"]
                if geo.get("type") in ("FeatureCollection", "Feature"):
                    layer["geojson"] = geo
            # markers 列表
            if "markers" in data and isinstance(data["markers"], list):
                layer["markers"] = data["markers"]
            # map_data 嵌套结构（geo_search 等 skill）
            if "map_data" in data and isinstance(data["map_data"], dict):
                md = data["map_data"]
                if "geojson" in md:
                    layer.setdefault("geojson", md["geojson"])
                if "markers" in md:
                    layer.setdefault("markers", md["markers"])
                if "center" in md:
                    layer["center"] = md["center"]
                if "zoom" in md:
                    layer["zoom"] = md["zoom"]
            # polyline
            if "polyline" in data and isinstance(data["polyline"], list):
                layer["polyline"] = data["polyline"]
                if "color" in data:
                    layer["color"] = data["color"]
            # center / zoom
            if "center" in data and "center" not in layer:
                layer["center"] = data["center"]
            if "zoom" in data and "zoom" not in layer:
                layer["zoom"] = data["zoom"]

            if layer:
                layers.append(layer)
        return layers

    def _build_scene_section(
        self,
        scene_type: str,
        scene_tab: Dict[str, Any],
        scene_updates: List[Dict[str, Any]],
        overlay_events: List[Dict[str, Any]] = None,
        map_layer_events: List[Dict[str, Any]] = None,
    ) -> str:
        """
        Build scene-specific HTML section (canvas + overlay panels + layers).

        Args:
            scene_type: 场景类型
            scene_tab: 场景画布配置
            scene_updates: 独立的场景更新事件（旧路径）
            overlay_events: layout_hint.role=overlay 的组件列表（Vue 挂载）
            map_layer_events: layout_hint.role=poi/layer 的组件列表（地图数据）
        """
        canvas_type = scene_tab.get("canvas_type", "")
        canvas_config = scene_tab.get("canvas_config", {})
        tile_style = canvas_config.get("tile", "light")

        overlay_events = overlay_events or []
        map_layer_events = map_layer_events or []

        # Separate layers and panels from scene_updates（旧路径兼容）
        layers = []
        panels = []
        for update in (scene_updates or []):
            role = update.get("role", "standalone")
            if role == "layer":
                layers.append(update)
            elif role == "panel":
                panels.append(update)

        # 从 component_events 的 data 中提取地图图层
        extracted_layers = self._extract_geo_layers_from_components(
            map_layer_events, overlay_events,
        )

        tile_sources_json = self._load_tile_sources()
        all_layers = layers + [{"data": el} for el in extracted_layers]
        layers_json = json.dumps(all_layers, ensure_ascii=False, default=str)
        panels_json = json.dumps(panels, ensure_ascii=False, default=str)

        # Scene type badge
        scene_labels = {"map": "地图分析", "stock": "股票分析", "intel": "情报分析"}
        scene_label = scene_labels.get(scene_type, scene_type)

        # overlay 组件挂载点 HTML（在地图右侧面板中用 Vue 渲染）
        overlay_mount_html = ""
        for oi, evt in enumerate(overlay_events):
            evt_title = (evt.get("title", "") or evt.get("component", "")).replace("<", "&lt;").replace(">", "&gt;")
            overlay_mount_html += f'''
                <div class="overlay-panel-item">
                    <div class="overlay-panel-header" onclick="this.parentElement.classList.toggle('collapsed')">
                        <span>{evt_title}</span>
                        <span class="collapse-icon">▼</span>
                    </div>
                    <div class="overlay-panel-body">
                        <div class="component-mount" id="overlay-mount-{oi}">
                            <div class="vue-loading"><div class="spinner"></div><span>加载中...</span></div>
                        </div>
                    </div>
                </div>
'''
        overlay_events_json = json.dumps(
            [{"component": evt.get("component", "dynamic_card"),
              "data": evt.get("data", {}),
              "title": evt.get("title", "")}
             for evt in overlay_events],
            ensure_ascii=False, default=str,
        )

        return f'''
        <!-- 场景区域 -->
        <section class="scene-section">
            <div class="scene-header">
                <span class="scene-badge">{scene_label}</span>
            </div>
            <div class="scene-layout">
                <div class="scene-canvas" id="scene-canvas"></div>
                <div class="scene-overlay-panels" id="scene-overlay-panels">
{overlay_mount_html}
                </div>
            </div>
        </section>
        <style>
            .scene-section {{
                background: rgba(255,255,255,0.85);
                backdrop-filter: blur(20px);
                border: 1px solid rgba(255,255,255,0.6);
                border-radius: var(--radius-md, 12px);
                overflow: hidden;
                margin-bottom: 20px;
                box-shadow: 0 4px 6px -1px rgba(0,0,0,0.02);
            }}
            .scene-header {{
                padding: 12px 20px;
                background: rgba(248,249,252,0.8);
                border-bottom: 1px solid rgba(226,232,240,0.8);
            }}
            .scene-badge {{
                display: inline-block;
                padding: 4px 12px;
                border-radius: 20px;
                font-size: 12px;
                font-weight: 600;
                color: #fff;
                background: #0052cc;
            }}
            .scene-layout {{
                display: flex;
                min-height: 500px;
            }}
            .scene-canvas {{
                flex: 1;
                position: relative;
                min-height: 500px;
            }}
            .scene-overlay-panels {{
                width: 380px;
                border-left: 1px solid rgba(226,232,240,0.8);
                overflow-y: auto;
                max-height: 600px;
                background: rgba(248,249,252,0.6);
            }}
            .scene-overlay-panels:empty {{
                display: none;
            }}
            .overlay-panel-item {{
                border-bottom: 1px solid rgba(226,232,240,0.5);
            }}
            .overlay-panel-item.collapsed .overlay-panel-body {{
                display: none;
            }}
            .overlay-panel-item.collapsed .collapse-icon {{
                transform: rotate(-90deg);
            }}
            .overlay-panel-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 10px 16px;
                font-size: 13px;
                font-weight: 600;
                color: #334155;
                cursor: pointer;
                background: rgba(255,255,255,0.8);
                user-select: none;
            }}
            .overlay-panel-header:hover {{
                background: rgba(255,255,255,1);
            }}
            .collapse-icon {{
                font-size: 10px;
                color: #94a3b8;
                transition: transform 0.2s;
            }}
            .overlay-panel-body {{
                padding: 0;
            }}
            .overlay-panel-body .component-mount {{
                padding: 8px 12px;
                min-height: 40px;
            }}
            @media (max-width: 768px) {{
                .scene-layout {{ flex-direction: column; }}
                .scene-overlay-panels {{ width: 100%; border-left: none; border-top: 1px solid rgba(226,232,240,0.8); max-height: 400px; }}
            }}
        </style>
        <script>
        (function() {{
            var tileSources = {tile_sources_json};
            var layers = {layers_json};
            var panels = {panels_json};
            var canvasType = "{canvas_type}";
            var tileStyle = "{tile_style}";

            // Render old-style panels (backward compat)
            var panelsEl = document.getElementById("scene-panels");
            if (panelsEl && panels.length > 0) {{
                panels.forEach(function(p) {{
                    var div = document.createElement("div");
                    div.className = "panel-item";
                    var content = "";
                    if (typeof p.data === "object" && p.data !== null) {{
                        Object.keys(p.data).forEach(function(k) {{
                            content += "<div><strong>" + k + ":</strong> " + String(p.data[k]) + "</div>";
                        }});
                    }} else {{
                        content = String(p.data || "");
                    }}
                    div.innerHTML = "<h4>" + (p.title || p.skill_name || "面板") + "</h4><div class=\\"panel-data\\">" + content + "</div>";
                    panelsEl.appendChild(div);
                }});
            }}

            // Initialize canvas
            var canvasEl = document.getElementById("scene-canvas");
            if (!canvasEl) return;

            if (canvasType === "map" && typeof L !== "undefined") {{
                var tileConfig = tileSources[tileStyle] || tileSources["light"] || {{
                    url: "https://webrd0{{s}}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={{x}}&y={{y}}&z={{z}}",
                    attribution: "高德地图",
                    subdomains: "1234"
                }};
                var map = L.map(canvasEl).setView([30, 110], 4);
                L.tileLayer(tileConfig.url, {{
                    attribution: tileConfig.attribution || "",
                    subdomains: tileConfig.subdomains || "abc"
                }}).addTo(map);

                var bounds = L.latLngBounds();
                var hasData = false;

                layers.forEach(function(layer) {{
                    var d = layer.data || {{}};
                    if (d.geojson) {{
                        try {{
                            var gj = L.geoJSON(d.geojson, {{
                                style: function() {{ return {{color: "#0052cc", weight: 2, fillOpacity: 0.15}}; }},
                                pointToLayer: function(f, ll) {{
                                    bounds.extend(ll);
                                    hasData = true;
                                    return L.circleMarker(ll, {{radius: 6, color: "#dc2626", fillColor: "#ef4444", fillOpacity: 0.8}});
                                }},
                                onEachFeature: function(f, l) {{
                                    var props = f.properties || {{}};
                                    var popup = props.name || props.title || props.label || "";
                                    if (props.count) popup += " (" + props.count + ")";
                                    if (popup) l.bindPopup(popup);
                                    if (l.getBounds) {{
                                        try {{ bounds.extend(l.getBounds()); hasData = true; }} catch(e) {{}}
                                    }} else if (l.getLatLng) {{
                                        bounds.extend(l.getLatLng()); hasData = true;
                                    }}
                                }}
                            }});
                            gj.addTo(map);
                        }} catch(e) {{ console.warn("GeoJSON parse error:", e); }}
                    }}
                    if (d.markers && Array.isArray(d.markers)) {{
                        d.markers.forEach(function(m) {{
                            // 兼容多种 marker 坐标格式:
                            // 1. {{lat, lng}} — 直接使用
                            // 2. {{position: [lng, lat]}} — GeoJSON 经纬度顺序
                            // 3. {{coordinates: [lng, lat]}} — 同上
                            var lat, lng;
                            if (m.lat != null && m.lng != null) {{
                                lat = m.lat; lng = m.lng;
                            }} else if (Array.isArray(m.position) && m.position.length >= 2) {{
                                lng = m.position[0]; lat = m.position[1];
                            }} else if (Array.isArray(m.coordinates) && m.coordinates.length >= 2) {{
                                lng = m.coordinates[0]; lat = m.coordinates[1];
                            }}
                            if (lat != null && lng != null) {{
                                var popupText = m.popup || m.description || m.label || m.name || "";
                                var markerColor = m.color || "#0052cc";
                                var icon = L.divIcon({{
                                    className: "",
                                    html: '<div style="width:12px;height:12px;border-radius:50%;background:' + markerColor + ';border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,0.3)"></div>',
                                    iconSize: [12, 12],
                                    iconAnchor: [6, 6],
                                    popupAnchor: [0, -8]
                                }});
                                var marker = L.marker([lat, lng], {{icon: icon}});
                                if (popupText) marker.bindPopup(popupText);
                                marker.addTo(map);
                                bounds.extend([lat, lng]);
                                hasData = true;
                            }}
                        }});
                    }}
                    if (d.polyline && Array.isArray(d.polyline)) {{
                        var pl = L.polyline(d.polyline, {{color: d.color || "#0052cc", weight: 3}});
                        pl.addTo(map);
                        bounds.extend(pl.getBounds());
                        hasData = true;
                    }}
                    if (d.center) {{
                        var cLat, cLng;
                        if (d.center.lat != null && d.center.lng != null) {{
                            cLat = d.center.lat; cLng = d.center.lng;
                        }} else if (Array.isArray(d.center) && d.center.length >= 2) {{
                            // center: [lng, lat] — GeoJSON 经纬度顺序
                            cLng = d.center[0]; cLat = d.center[1];
                        }}
                        if (cLat != null && cLng != null && !hasData) {{
                            map.setView([cLat, cLng], d.zoom || 6);
                        }}
                    }}
                }});

                // Auto fit bounds
                setTimeout(function() {{
                    map.invalidateSize();
                    if (hasData && bounds.isValid()) {{
                        map.fitBounds(bounds, {{padding: [40, 40], maxZoom: 8}});
                    }}
                }}, 300);

            }} else if (canvasType === "chart" && typeof echarts !== "undefined") {{
                var chart = echarts.init(canvasEl);
                var option = {{ tooltip: {{}}, xAxis: {{ type: "category", data: [] }}, yAxis: {{ type: "value" }}, series: [] }};
                layers.forEach(function(layer) {{
                    var d = layer.data || {{}};
                    if (d.option) {{
                        option = d.option;
                    }} else if (d.series) {{
                        if (d.xAxis) option.xAxis.data = d.xAxis;
                        option.series = option.series.concat(d.series);
                    }}
                }});
                chart.setOption(option);
                window.addEventListener("resize", function() {{ chart.resize(); }});

            }} else if (canvasType === "graph" && typeof d3 !== "undefined") {{
                var width = canvasEl.clientWidth || 600;
                var height = canvasEl.clientHeight || 400;
                var svg = d3.select(canvasEl).append("svg").attr("width", width).attr("height", height);
                var nodes = [];
                var links = [];
                layers.forEach(function(layer) {{
                    var d = layer.data || {{}};
                    if (d.nodes) nodes = nodes.concat(d.nodes);
                    if (d.links || d.edges) links = links.concat(d.links || d.edges);
                }});
                if (nodes.length > 0) {{
                    var simulation = d3.forceSimulation(nodes)
                        .force("link", d3.forceLink(links).id(function(d) {{ return d.id; }}))
                        .force("charge", d3.forceManyBody().strength(-100))
                        .force("center", d3.forceCenter(width/2, height/2));
                    var link = svg.selectAll("line").data(links).enter().append("line")
                        .attr("stroke", "#94a3b8").attr("stroke-width", 1);
                    var node = svg.selectAll("circle").data(nodes).enter().append("circle")
                        .attr("r", 8).attr("fill", "#0052cc");
                    var label = svg.selectAll("text").data(nodes).enter().append("text")
                        .text(function(d) {{ return d.label || d.name || d.id || ""; }})
                        .attr("font-size", "10px").attr("dx", 12).attr("dy", 4);
                    simulation.on("tick", function() {{
                        link.attr("x1", function(d) {{ return d.source.x; }}).attr("y1", function(d) {{ return d.source.y; }})
                            .attr("x2", function(d) {{ return d.target.x; }}).attr("y2", function(d) {{ return d.target.y; }});
                        node.attr("cx", function(d) {{ return d.x; }}).attr("cy", function(d) {{ return d.y; }});
                        label.attr("x", function(d) {{ return d.x; }}).attr("y", function(d) {{ return d.y; }});
                    }});
                }}
            }} else {{
                canvasEl.innerHTML = "<div style=\\"padding:40px;text-align:center;color:#94a3b8\\">画布初始化中...</div>";
            }}

            // 注册 overlay 挂载函数（Vue/Bundle 在页面底部加载，此处先存数据，延迟挂载）
            window.__overlayEvents = {overlay_events_json};
            window.__mountOverlayComponents = function() {{
                var overlayEvents = window.__overlayEvents || [];
                if (overlayEvents.length === 0 || !window.SthgComponents) return;
                var Components = window.SthgComponents;
                var readyP = Components._readyPromise || Promise.resolve();
                readyP.then(function() {{
                    var compMap = Components.componentMap || {{}};
                    overlayEvents.forEach(function(evt, idx) {{
                        var mountEl = document.getElementById("overlay-mount-" + idx);
                        if (!mountEl) return;
                        var targetComp = compMap[evt.component] || compMap["dynamic_card"];
                        if (!targetComp) {{
                            mountEl.innerHTML = "<div style=\\"color:#94a3b8;padding:8px;font-size:12px\\">组件未注册: " + evt.component + "</div>";
                            return;
                        }}
                        try {{
                            var app = Vue.createApp({{
                                render: function() {{ return Vue.h(this.comp, {{ data: this.compData, compact: true }}); }},
                                data: function() {{ return {{ comp: Vue.markRaw(targetComp), compData: evt.data }}; }}
                            }});
                            app.mount(mountEl);
                        }} catch(e) {{
                            mountEl.innerHTML = "<div style=\\"color:#ef4444;padding:8px;font-size:12px\\">渲染失败: " + String(e) + "</div>";
                        }}
                    }});
                }});
            }};
        }})();
        </script>
'''

    def _build_html(
        self,
        component_events: List[Dict[str, Any]],
        title: str = "",
        report_markdown: str = "",
        scene_type: str = "default",
        scene_tab: Dict[str, Any] = None,
        scene_updates: List[Dict[str, Any]] = None,
    ) -> str:
        """生成完整的 HTML 页面字符串（技能组件数据 + LLM 分析报告）"""

        # 确定页面标题
        page_title = title
        if not page_title and component_events:
            page_title = component_events[0].get("title", "")
        if not page_title:
            page_title = "分析结果"
        page_title_escaped = page_title.replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

        # 时间戳
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # bundle URLs
        main_css_url = f"{self._bundle_base_url}/main.css"
        bundle_css_url = f"{self._bundle_base_url}/component-bundle.css"
        vue_js_url = f"{self._bundle_base_url}/vue.global.prod.js"
        bundle_js_url = f"{self._bundle_base_url}/component-bundle.umd.js"

        # 按 layout_hint 分类组件（场景模式 vs 默认模式）
        classified = self._classify_components(component_events, scene_type)
        # standalone 组件走纵向卡片渲染
        standalone_events = classified["standalone"]

        # 序列化 standalone 组件数据（用于 Vue 挂载）
        events_json = json.dumps(
            [
                {
                    "component": evt.get("component", "dynamic_card"),
                    "data": evt.get("data", {}),
                    "title": evt.get("title", ""),
                }
                for evt in standalone_events
            ],
            ensure_ascii=False,
            default=str,
        )

        # 生成 standalone 组件挂载点 HTML
        mount_points_html = ""
        for idx, evt in enumerate(standalone_events):
            evt_title = (evt.get("title", "") or evt.get("component", "")).replace("<", "&lt;").replace(">", "&gt;")
            mount_points_html += f'''
        <div class="component-section">
            <div class="section-title">{evt_title}</div>
            <div class="component-mount" id="mount-{idx}">
                <div class="vue-loading">
                    <div class="spinner"></div>
                    <span>组件加载中...</span>
                </div>
            </div>
        </div>
'''

        # 报告 Markdown 内容（序列化为 JSON 字符串供 JS 使用）
        report_json = json.dumps(report_markdown, ensure_ascii=False) if report_markdown else '""'
        # 是否有报告内容
        has_report = bool(report_markdown and report_markdown.strip())

        # 加载 JS 预加载标签
        js_preload_tags = self._load_js_preloads()

        # Scene-specific content (canvas + overlay panels + layers)
        scene_section_html = ""
        is_scene_mode = (
            scene_type and scene_type != "default"
            and (scene_tab or scene_updates or classified["overlays"] or classified["map_layers"])
        )
        if is_scene_mode:
            scene_section_html = self._build_scene_section(
                scene_type=scene_type,
                scene_tab=scene_tab or {},
                scene_updates=scene_updates or [],
                overlay_events=classified["overlays"],
                map_layer_events=classified["map_layers"],
            )

        return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_title_escaped}</title>

    <!-- 全局样式（CSS 变量 + 工具类，组件依赖） -->
    <link rel="stylesheet" href="{main_css_url}"
          onerror="console.warn('main.css not found')">

    <!-- 组件 bundle CSS（Vue 组件 scoped 样式） -->
    <link rel="stylesheet" href="{bundle_css_url}"
          onerror="console.warn('component-bundle.css not found')">

    <!-- 可视化库预加载（供组件内按需使用） -->
{js_preload_tags}

    <style>
        /* 覆盖 main.css 的布局类，适配独立页面（非 SPA） */
        body {{
            background-color: var(--bg-base, #f8f9fc);
            background-image:
                radial-gradient(circle at 15% 0%, rgba(0, 82, 204, 0.03) 0%, transparent 40%),
                radial-gradient(circle at 85% 0%, rgba(5, 150, 105, 0.03) 0%, transparent 40%);
            background-attachment: fixed;
            margin: 0;
            padding: 0;
        }}

        .page-container {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 24px 20px;
        }}

        /* 页头 */
        .page-header {{
            background: rgba(255, 255, 255, 0.85);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid rgba(255, 255, 255, 0.6);
            border-radius: var(--radius-md, 12px);
            padding: 24px 28px;
            margin-bottom: 24px;
            box-shadow: var(--shadow-sm, 0 4px 6px -1px rgba(15, 23, 42, 0.06));
        }}
        .page-header h1 {{
            font-size: 22px;
            font-weight: 700;
            color: var(--text-primary, #1e293b);
            margin-bottom: 6px;
            letter-spacing: -0.02em;
        }}
        .page-header .meta {{
            font-size: 13px;
            color: var(--text-tertiary, #94a3b8);
        }}

        /* 技能组件区域 */
        .components-area {{
            display: flex;
            flex-direction: column;
            gap: 20px;
        }}

        .component-section {{
            background: rgba(255, 255, 255, 0.85);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid rgba(255, 255, 255, 0.6);
            border-radius: var(--radius-md, 12px);
            overflow: hidden;
            box-shadow:
                0 4px 6px -1px rgba(0, 0, 0, 0.02),
                0 2px 4px -1px rgba(0, 0, 0, 0.02),
                inset 0 1px 0 rgba(255, 255, 255, 0.9);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }}
        .component-section:hover {{
            transform: translateY(-2px);
            box-shadow:
                0 10px 15px -3px rgba(0, 50, 150, 0.05),
                0 4px 6px -2px rgba(0, 50, 150, 0.02);
            background: rgba(255, 255, 255, 0.95);
        }}

        .section-title {{
            font-size: 15px;
            font-weight: 600;
            color: var(--text-primary, #334155);
            padding: 14px 20px;
            background: rgba(248, 249, 252, 0.8);
            border-bottom: 1px solid var(--border-color, rgba(226, 232, 240, 0.8));
        }}
        .component-mount {{
            padding: 16px;
            min-height: 60px;
        }}

        /* 加载动画 */
        .vue-loading {{
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            padding: 30px;
            color: var(--text-tertiary, #94a3b8);
            font-size: 14px;
        }}
        .vue-loading .spinner {{
            width: 18px; height: 18px;
            border: 2px solid var(--border-color, #e2e8f0);
            border-top-color: var(--primary-color, #0052cc);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}

        /* 渲染错误 */
        .render-error {{
            color: var(--danger-color, #ef4444);
            padding: 16px;
            border: 1px solid #fecaca;
            border-radius: var(--radius-sm, 8px);
            background: #fff5f5;
            font-size: 14px;
        }}

        /* 报告内容区域 */
        .report-section {{
            background: rgba(255, 255, 255, 0.85);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid rgba(255, 255, 255, 0.6);
            border-radius: var(--radius-md, 12px);
            overflow: hidden;
            box-shadow:
                0 4px 6px -1px rgba(0, 0, 0, 0.02),
                0 2px 4px -1px rgba(0, 0, 0, 0.02),
                inset 0 1px 0 rgba(255, 255, 255, 0.9);
            margin-top: 20px;
        }}
        .report-section .section-title {{
            font-size: 15px;
            font-weight: 600;
            color: var(--text-primary, #334155);
            padding: 14px 20px;
            background: rgba(248, 249, 252, 0.8);
            border-bottom: 1px solid var(--border-color, rgba(226, 232, 240, 0.8));
        }}
        .report-content {{
            padding: 24px 28px;
            font-size: 14px;
            line-height: 1.8;
            color: var(--text-primary, #1e293b);
        }}
        .report-content h1 {{ font-size: 20px; font-weight: 700; margin: 24px 0 12px; color: #0f172a; }}
        .report-content h2 {{ font-size: 17px; font-weight: 600; margin: 20px 0 10px; color: #1e293b; }}
        .report-content h3 {{ font-size: 15px; font-weight: 600; margin: 16px 0 8px; color: #334155; }}
        .report-content h4 {{ font-size: 14px; font-weight: 600; margin: 14px 0 6px; color: #475569; }}
        .report-content p {{ margin: 8px 0; }}
        .report-content ul, .report-content ol {{ margin: 8px 0; padding-left: 24px; }}
        .report-content li {{ margin: 4px 0; }}
        .report-content table {{
            width: 100%;
            border-collapse: collapse;
            margin: 12px 0;
            font-size: 13px;
        }}
        .report-content table th {{
            background: #f1f5f9;
            font-weight: 600;
            text-align: left;
            padding: 10px 12px;
            border: 1px solid #e2e8f0;
        }}
        .report-content table td {{
            padding: 8px 12px;
            border: 1px solid #e2e8f0;
        }}
        .report-content table tr:nth-child(even) {{
            background: #f8fafc;
        }}
        .report-content blockquote {{
            border-left: 3px solid #0052cc;
            margin: 12px 0;
            padding: 8px 16px;
            background: #f0f7ff;
            color: #334155;
            border-radius: 0 6px 6px 0;
        }}
        .report-content code {{
            background: #f1f5f9;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 13px;
            font-family: 'SF Mono', 'Fira Code', monospace;
        }}
        .report-content pre {{
            background: #1e293b;
            color: #e2e8f0;
            padding: 16px;
            border-radius: 8px;
            overflow-x: auto;
            margin: 12px 0;
        }}
        .report-content pre code {{
            background: transparent;
            padding: 0;
            color: inherit;
        }}
        .report-content hr {{
            border: none;
            border-top: 1px solid #e2e8f0;
            margin: 20px 0;
        }}
        .report-content strong {{ color: #0f172a; }}
        .report-content .mermaid {{
            display: flex;
            justify-content: center;
            margin: 16px 0;
        }}

        /* 页脚 */
        .page-footer {{
            margin-top: 32px;
            padding: 16px 0;
            border-top: 1px solid var(--border-color, rgba(226, 232, 240, 0.8));
            display: flex;
            justify-content: space-between;
            font-size: 12px;
            color: var(--text-tertiary, #94a3b8);
        }}

        /* 响应式 */
        @media (max-width: 768px) {{
            .page-container {{ padding: 16px 12px; }}
            .page-header h1 {{ font-size: 18px; }}
            .component-mount {{ padding: 12px; }}
            .report-content {{ padding: 16px; }}
            .page-footer {{ flex-direction: column; gap: 4px; }}
        }}
    </style>
</head>
<body>
    <div class="page-container">
        <!-- 页头 -->
        <header class="page-header">
            <h1>{page_title_escaped}</h1>
            <div class="meta">{now_str}</div>
        </header>

        <!-- 场景区域（非 default 场景时渲染） -->
{scene_section_html}

        <!-- 技能组件区域 -->
        <section class="components-area">
{mount_points_html}
        </section>

        <!-- 报告内容区域（LLM 完整分析） -->
        <section class="report-section" id="report-section" style="display:none;">
            <div class="section-title">综合分析报告</div>
            <div class="report-content" id="report-content"></div>
        </section>

        <!-- 页脚 -->
        <footer class="page-footer">
            <span>{now_str}</span>
            <span>数据来自各数据API实时获取，仅供参考</span>
        </footer>
    </div>

    <!-- Vue 3 Runtime -->
    <script src="{vue_js_url}"></script>

    <!-- 前端组件 Bundle -->
    <script src="{bundle_js_url}"></script>

    <!-- Marked / Mermaid 已在 <head> 的 js_preloads 中加载 -->

    <script>
    (function() {{
        // ========== 1. 渲染 Vue 组件 ==========
        var Components = window.SthgComponents;
        if (!Components) {{
            console.warn('[SkillPage] SthgComponents bundle not loaded');
        }} else {{
            var readyPromise = Components._readyPromise || Promise.resolve();
            readyPromise.then(function() {{
                var compMap = Components.componentMap || {{}};
                var events = {events_json};

                var mountedCount = 0;
                events.forEach(function(evt, idx) {{
                    var mountEl = document.getElementById('mount-' + idx);
                    if (!mountEl) return;

                    var targetComp = compMap[evt.component];
                    if (!targetComp) {{
                        targetComp = compMap['dynamic_card'];
                    }}
                    if (!targetComp) {{
                        mountEl.innerHTML = '<div class="render-error">'
                            + '组件未注册: ' + evt.component + '</div>';
                        return;
                    }}

                    try {{
                        var app = Vue.createApp({{
                            render: function() {{
                                return Vue.h(this.comp, {{ data: this.compData }});
                            }},
                            data: function() {{
                                return {{
                                    comp: Vue.markRaw(targetComp),
                                    compData: evt.data
                                }};
                            }}
                        }});
                        app.mount(mountEl);
                        mountedCount++;
                    }} catch(e) {{
                        mountEl.innerHTML = '<div class="render-error">'
                            + '组件渲染失败: ' + String(e) + '</div>';
                        console.error('Mount failed for', evt.component, e);
                    }}
                }});
            }}).catch(function(err) {{
                console.error('[SkillPage] Component ready promise rejected:', err);
            }});
        }}

        // ========== 1.5 渲染场景 overlay 组件（延迟挂载，Vue/Bundle 已就绪） ==========
        if (typeof window.__mountOverlayComponents === 'function') {{
            window.__mountOverlayComponents();
        }}

        // ========== 2. 渲染报告 Markdown ==========
        var reportMarkdown = {report_json};
        if (reportMarkdown && reportMarkdown.trim()) {{
            try {{
                // 配置 marked: mermaid 代码块 → <div class="mermaid">
                // marked v5+ 使用 extensions API；v4 及以下使用 renderer.code
                if (typeof marked.use === 'function') {{
                    // marked v5+ (v12/v15 等)：使用 extensions
                    marked.use({{
                        renderer: {{
                            code: function(token) {{
                                // v12+: token 是 {{ text, lang, escaped }}
                                var lang = (token && token.lang) || "";
                                var text = (token && token.text) || (typeof token === "string" ? token : "");
                                if (lang === "mermaid") {{
                                    return '<div class="mermaid">' + text + '</div>';
                                }}
                                return '<pre><code class="language-' + lang + '">' + text + '</code></pre>';
                            }}
                        }}
                    }});
                }} else {{
                    // marked v4 及以下：使用 renderer
                    var renderer = new marked.Renderer();
                    var origCode = renderer.code.bind(renderer);
                    renderer.code = function(code, language) {{
                        if (language === 'mermaid') {{
                            return '<div class="mermaid">' + code + '</div>';
                        }}
                        return origCode(code, language);
                    }};
                    marked.setOptions({{ renderer: renderer }});
                }}

                // 防御: 修复未闭合的代码块（LLM 截断/拼接时可能产生奇数个 ```）
                var backtickCount = (reportMarkdown.match(/```/g) || []).length;
                if (backtickCount % 2 !== 0) {{
                    reportMarkdown += '\\n```\\n';
                }}

                var html = marked.parse(reportMarkdown);
                document.getElementById('report-content').innerHTML = html;
                document.getElementById('report-section').style.display = '';

                // 初始化 Mermaid（逐块渲染，单个失败不影响其他）
                var mermaidBlocks = document.querySelectorAll('.report-content .mermaid');
                if (mermaidBlocks.length > 0 && typeof mermaid !== 'undefined') {{
                    mermaid.initialize({{ startOnLoad: false, theme: 'default', securityLevel: 'loose' }});
                    mermaidBlocks.forEach(function(block, idx) {{
                        try {{
                            var src = block.textContent || block.innerText;
                            mermaid.render('mermaid-svg-' + idx, src).then(function(result) {{
                                block.innerHTML = result.svg;
                            }}).catch(function(err) {{
                                console.warn('[Mermaid] Block ' + idx + ' render failed:', err);
                                block.innerHTML = '<pre style="background:#f8f9fc;padding:12px;border-radius:8px;'
                                    + 'font-size:12px;color:#64748b;overflow-x:auto;white-space:pre-wrap">'
                                    + src.replace(/</g,'&lt;') + '</pre>';
                            }});
                        }} catch(e) {{
                            console.warn('[Mermaid] Block ' + idx + ' sync error:', e);
                        }}
                    }});
                }}
            }} catch(e) {{
                console.error('[SkillPage] Report markdown render failed:', e);
            }}
        }}

        // 全部渲染完成标记
        setTimeout(function() {{
            document.body.setAttribute('data-rendered', 'true');
        }}, 1200);
    }})();
    </script>
</body>
</html>'''
