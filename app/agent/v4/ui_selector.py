"""
UI Selector -- 基于 SKILL.md 元数据的 UI 组件选择器

职责:
1. 根据 SKILL.md 的 ui_components 字段选择 UI 组件
2. data_hints 匹配检查（has_ts_code, has_array, has_score 等）
3. condition: default 作为兜底
4. 返回 component + confidence + title + data

完全独立于 V3 的 UIResourceBinder。
"""
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple
from loguru import logger

from agent_core.skill_discovery import SkillDiscovery


@dataclass
class V4RenderInstruction:
    """V4 渲染指令"""
    component: str
    confidence: float
    title: str = ""
    data: Optional[Dict[str, Any]] = None
    is_dynamic: bool = False  # 是否为动态生成的组件


@dataclass
class DynamicComponentSpec:
    """动态组件规格（由 frontend_builder 生成）"""
    component_type: str  # "table", "card", "chart", "list", "timeline" 等
    template_code: str  # Vue/React 组件代码
    data: Dict[str, Any]  # 绑定数据
    preview_html: Optional[str] = None  # 预览 HTML


# data_hints -> 检查函数映射（可扩展）
_HINT_CHECKERS = {
    "has_ts_code": lambda d: _has_key(d, "ts_code"),
    "has_name": lambda d: _has_key(d, "name"),
    "has_score": lambda d: _has_key(d, "score") or _has_key(d, "total_score"),
    "has_dimensions": lambda d: _has_key(d, "dimensions") or _has_key(d, "scores"),
    "has_array": lambda d: _has_list_data(d),
    "has_flight_info": lambda d: _has_key(d, "flight") or _has_key(d, "icao"),
    "has_ship_info": lambda d: _has_key(d, "ship") or _has_key(d, "mmsi"),
    "has_insights": lambda d: _has_key(d, "insights") or _has_key(d, "highlights"),
    "query_type_flight": lambda d: _has_key(d, "query_type") and _key_equals(d, "query_type", "flight"),
    "query_type_ship": lambda d: _has_key(d, "query_type") and _key_equals(d, "query_type", "ship"),
    "query_type_search": lambda d: _has_key(d, "query_type") and _key_equals(d, "query_type", "search"),
    "has_ohlc": lambda d: _has_ohlc_data(d),
    "has_daily": lambda d: _has_key(d, "daily"),
    "has_main_net": lambda d: _has_key(d, "summary") and isinstance(d.get("summary"), dict) and "main_net_5d" in d.get("summary", {}),
    "has_chart_data": lambda d: _has_key(d, "chart") or _has_key(d, "series"),
    "has_table_data": lambda d: _has_key(d, "rows") or _has_key(d, "columns") or _has_key(d, "items"),
    "has_prediction": lambda d: _has_key(d, "prediction") or _has_key(d, "forecast"),
    "has_indicators": lambda d: _has_key(d, "indicators") or _has_key(d, "ma") or _has_key(d, "macd"),
    "has_download_url": lambda d: _has_key(d, "download_url") or _has_key(d, "file_path"),
    # web_search intelligent mode hints
    "is_intelligent_result": lambda d: _has_key(d, "is_intelligent_result") or _has_key(d, "intelligent_data"),
    "has_collected_data": lambda d: _has_intelligent_data(d),
    # web_search results hints
    "has_results": lambda d: _has_key(d, "results") or _has_key(d, "_raw_results") or _has_key(d, "search_results"),
    "has_components": lambda d: _has_key(d, "components"),
    # geo_search hints
    "has_locations": lambda d: _has_key(d, "locations"),
    "has_coordinates": lambda d: (
        _has_key(d, "coordinates")
        or _has_key(d, "center")  # map_bindary 直接返回 center 字段
        or (_has_key(d, "primary_location") and isinstance(d.get("primary_location"), dict)
            and ("lat" in d.get("primary_location", {}) or "lng" in d.get("primary_location", {})))
        or (_has_key(d, "map_data") and isinstance(d.get("map_data"), dict)
            and "center" in d.get("map_data", {}))
    ),
    "has_street_view_base64": lambda d: _has_key(d, "street_view_base64") and d.get("street_view_base64") is not None,
    "has_geojson": lambda d: (
        _has_key(d, "geojson")
        or (_has_key(d, "boundary_geojson") and d.get("boundary_geojson") is not None)
        or (_has_key(d, "map_data") and isinstance(d.get("map_data"), dict)
            and d.get("map_data", {}).get("geojson") is not None)
    ),
    "has_boundary": lambda d: (
        (_has_key(d, "boundary_geojson") and d.get("boundary_geojson") is not None)
        or (_has_key(d, "map_data") and isinstance(d.get("map_data"), dict)
            and d.get("map_data", {}).get("geojson") is not None)
    ),
    "has_nearby": lambda d: _has_key(d, "nearby_locations") and bool(d.get("nearby_locations")),
    "has_circles": lambda d: (
        _has_key(d, "map_data") and isinstance(d.get("map_data"), dict)
        and bool(d.get("map_data", {}).get("circles"))
    ),
    # map_bindary hints
    "has_markers": lambda d: (
        _has_key(d, "markers")
        or (_has_key(d, "map_data") and isinstance(d.get("map_data"), dict)
            and bool(d.get("map_data", {}).get("markers")))
    ),
    "has_geo_data": lambda d: (
        _has_key(d, "geojson") or _has_key(d, "boundary_geojson")
        or _has_key(d, "map_data") or _has_key(d, "markers")
    ),
    # weather / terrain / 3d hints
    "has_weather_data": lambda d: _has_key(d, "current") or _has_key(d, "weather"),
    "has_temperature": lambda d: (
        _has_key(d, "temperature")
        or (isinstance(d.get("current"), dict) and "temperature" in d.get("current", {}))
    ),
    "has_forecast": lambda d: _has_key(d, "hourly_forecast") or _has_key(d, "daily_forecast"),
    "has_daily_forecast": lambda d: _has_key(d, "daily_forecast"),
    "has_alerts": lambda d: _has_key(d, "alerts") or _has_key(d, "typhoon_tracks"),
    "has_tile_layers": lambda d: (
        _has_key(d, "tile_layers")
        or (isinstance(d.get("map_data"), dict) and bool(d.get("map_data", {}).get("tile_layers")))
    ),
    "has_elevation_profile": lambda d: _has_key(d, "elevation_profile") or _has_key(d, "elevation"),
    "has_embed_url": lambda d: _has_key(d, "embed_url") or _has_key(d, "embed_urls"),
    "has_3d_model": lambda d: _has_key(d, "tileset_url") or _has_key(d, "model_type"),
    # miroflow / artifact_view / markdown_card hints
    "has_html": lambda d: _has_key(d, "html_content") or _has_key(d, "markdown_content") or _has_key(d, "html"),
    "has_content": lambda d: _has_key(d, "markdown_content") or _has_key(d, "content") or _has_key(d, "html_content"),
    "has_markdown": lambda d: _has_key(d, "markdown_content") or _has_key(d, "markdown"),
    "has_report": lambda d: (
        _has_key(d, "has_report") and d.get("has_report") is True
    ) or _has_key(d, "report"),
    "has_analysis": lambda d: (
        _has_key(d, "has_markdown") and d.get("has_markdown") is True
    ) or _has_key(d, "analysis") or _has_key(d, "markdown_content"),
    "has_markdown_content": lambda d: _has_key(d, "markdown_content"),
}


def _validate_component_data_quality(component_name: str, comp_data: Dict[str, Any]) -> bool:
    """
    验证组件数据是否有足够的内容质量。
    Layer 1 提取 for_ui.components 时，仅检查 key 存在是不够的，
    需要确认核心数据字段非空，避免空壳组件被选中而跳过 Layer 2/3。

    Returns: True 表示数据质量合格，False 表示应跳过该组件
    """
    validator = _COMPONENT_QUALITY_VALIDATORS.get(component_name)
    if validator is None:
        # 无特定校验规则的组件，默认通过（保持向后兼容）
        return True
    try:
        return validator(comp_data)
    except Exception:
        return False


# 组件数据质量校验函数映射
# 每个 lambda 检查该组件渲染所需的核心数据是否非空
_COMPONENT_QUALITY_VALIDATORS = {
    "insight_highlights": lambda d: (
        isinstance(d.get("insights"), list)
        and len(d.get("insights", [])) > 0
        and isinstance(d["insights"][0], dict)
        and bool(d["insights"][0].get("title") or d["insights"][0].get("content"))
    ),
    "data_table": lambda d: (
        (isinstance(d.get("rows"), list) and len(d.get("rows", [])) > 0)
        or (isinstance(d.get("items"), list) and len(d.get("items", [])) > 0)
        or (isinstance(d.get("columns"), list) and len(d.get("columns", [])) > 0)
    ),
    "intelligent_search_result": lambda d: (
        isinstance(d.get("intelligent_data"), dict)
        and isinstance(d["intelligent_data"].get("collected_data"), list)
        and len(d["intelligent_data"].get("collected_data", [])) > 0
    ) or (
        isinstance(d.get("collected_data"), list)
        and len(d.get("collected_data", [])) > 0
    ),
    "flight_tracker_card": lambda d: bool(d.get("icao") or d.get("callsign") or d.get("flight")),
    "ship_tracker_card": lambda d: bool(d.get("mmsi") or d.get("ship_name") or d.get("ship")),
    "map_bindary_view": lambda d: (
        bool(d.get("markers") or d.get("geojson") or d.get("tracks")
             or d.get("trajectories") or d.get("map_data") or d.get("center"))
    ),
    "price_chart": lambda d: (
        # price_chart 需要 OHLC K线数据，情感时间线等非K线数据不适用
        _has_ohlc_data(d)
        or (isinstance(d.get("data"), list) and len(d.get("data", [])) > 0
            and isinstance(d["data"][0], dict)
            and any(k in d["data"][0] for k in ("open", "high", "low", "close")))
    ),
    "sentiment_line_chart": lambda d: (
        # Layer 1: for_ui 中 data 为 chart_data 列表
        (isinstance(d.get("data"), list) and len(d.get("data", [])) > 0)
        # Layer 2: 原始数据中 tone_timeline 为时间序列
        or (isinstance(d.get("tone_timeline"), list) and len(d.get("tone_timeline", [])) > 0)
    ),
    "html_render": lambda d: bool(d.get("preview_html")),
    "dynamic_renderer": lambda d: bool(d.get("preview_html")),
    "code_preview": lambda d: bool(d.get("preview_html") or d.get("generated_code")),
}


class V4UISelector:
    """
    V4 UI 组件选择器

    基于 SKILL.md 的 ui_components 字段进行组件选择。
    当没有匹配的预定义组件时，支持 fallback 到动态组件生成。
    """

    def __init__(self, discovery: SkillDiscovery, enable_dynamic_fallback: bool = True):
        self._discovery = discovery
        self._enable_dynamic_fallback = enable_dynamic_fallback
        # 动态组件生成器（懒加载）
        self._dynamic_generator = None

    def get_skill_metadata(self, skill_name: str) -> Optional[Dict]:
        """
        获取 skill 的完整 metadata（含 ui_components + scene_roles）

        用于 hook_manager 读取 scene_roles 声明来确定组件布局。

        Returns:
            {"ui_components": [...], "display_name": "..."} 或 None
        """
        meta = self._discovery.get_by_name(skill_name)
        if not meta:
            return None
        return {
            "ui_components": meta.ui_components or [],
            "display_name": meta.display_name or skill_name,
        }

    def select_all_matching_components(
        self, skill_name: str, data: Dict[str, Any]
    ) -> List[V4RenderInstruction]:
        """
        返回 skill 所有匹配的 ui_components（通用多组件匹配）

        与 select_component() 不同，此方法返回所有匹配的组件（不只是最佳匹配），
        适用于一个 skill 需要同时输出多个 UI 组件的场景
        （如 geo_search 同时输出 geo_search_card + map_bindary_view）。

        查找优先级:
        1. 嵌入的 for_ui.components（skill 自己声明）
        2. SKILL.md data_hints 匹配（所有满足阈值的组件）
        3. condition: default 的组件

        Returns:
            匹配的 V4RenderInstruction 列表
        """
        # 优先检查嵌入的 for_ui.components
        embedded = self._extract_all_ui_components_from_data(skill_name, data)
        if embedded:
            return embedded

        metadata = self._discovery.get_by_name(skill_name)
        if not metadata or not metadata.ui_components:
            return []

        transformed = self._transform_data_for_ui(skill_name, data)
        matches = []          # (priority, V4RenderInstruction)
        default_matches = []  # condition=="default" 的兜底
        for comp_def in metadata.ui_components:
            comp_name = comp_def.get("component", "")
            if not comp_name:
                continue
            hints = comp_def.get("data_hints", [])
            condition = comp_def.get("condition", "")
            priority = comp_def.get("priority", 99)
            if condition == "default":
                default_matches.append(V4RenderInstruction(
                    component=comp_name, confidence=0.5,
                    title=metadata.display_name, data=transformed,
                ))
            elif hints:
                conf = self._check_hints(hints, transformed)
                if conf >= 0.5:
                    # Layer 2 质量校验：data_hints 匹配后，检查数据是否真正适合该组件
                    if not _validate_component_data_quality(comp_name, transformed):
                        logger.warning(
                            f"[UISelector][Layer2] Skipped '{comp_name}' for '{skill_name}': "
                            f"data_hints matched (conf={conf:.2f}) but data quality insufficient"
                        )
                        continue
                    matches.append((priority, V4RenderInstruction(
                        component=comp_name, confidence=conf,
                        title=metadata.display_name, data=transformed,
                    )))

        if matches:
            # 按 priority 排序，只保留最优先级的组件（允许同 priority 多个）
            matches.sort(key=lambda x: x[0])
            best_priority = matches[0][0]
            result = [inst for pri, inst in matches if pri == best_priority]
            return result

        return default_matches

    def select_component(
        self,
        skill_name: str,
        data: Dict[str, Any],
    ) -> Optional[V4RenderInstruction]:
        """
        为技能输出选择 UI 组件

        Args:
            skill_name: 技能名称
            data: 技能输出数据

        Returns:
            V4RenderInstruction 或 None（无匹配组件时）
        """
        # 1. 首先尝试从数据中提取 for_ui.components (web_search 等技能的标准返回格式)
        ui_component = self._extract_ui_component_from_data(skill_name, data)
        if ui_component:
            return ui_component

        metadata = self._discovery.get_by_name(skill_name)
        if not metadata or not metadata.ui_components:
            # 如果没有元数据，尝试智能推断
            return self._infer_component_from_data(skill_name, data)

        best_match: Optional[V4RenderInstruction] = None
        best_confidence = 0.0
        default_component: Optional[V4RenderInstruction] = None

        # 转换数据格式以匹配 UI 组件期望
        transformed_data = self._transform_data_for_ui(skill_name, data)

        for comp_def in metadata.ui_components:
            component_name = comp_def.get("component", "")
            if not component_name:
                continue

            condition = comp_def.get("condition", "")
            hints = comp_def.get("data_hints", [])

            # condition: default 作为兜底
            if condition == "default":
                default_confidence = 0.5 if not hints else self._check_hints(hints, transformed_data)
                default_component = V4RenderInstruction(
                    component=component_name,
                    confidence=max(default_confidence, 0.5),
                    title=metadata.display_name,
                    data=transformed_data,
                )
                continue

            # 有 hints 的组件：按匹配比例计算 confidence
            if hints:
                confidence = self._check_hints(hints, transformed_data)
                if confidence > best_confidence:
                    # Layer 2 质量校验：data_hints 匹配后，检查数据是否真正适合该组件
                    if not _validate_component_data_quality(component_name, transformed_data):
                        logger.warning(
                            f"[UISelector][Layer2] Skipped '{component_name}' for '{skill_name}': "
                            f"data_hints matched (conf={confidence:.2f}) but data quality insufficient"
                        )
                        continue
                    best_confidence = confidence
                    best_match = V4RenderInstruction(
                        component=component_name,
                        confidence=confidence,
                        title=metadata.display_name,
                        data=transformed_data,
                    )

        # 优先返回最佳匹配（confidence >= 0.5）
        if best_match and best_match.confidence >= 0.5:
            return best_match

        # 兜底: default 组件
        if default_component:
            return default_component

        # 动态组件 fallback：当没有匹配的预定义组件时，尝试生成动态组件
        if self._enable_dynamic_fallback:
            dynamic_instruction = self._generate_dynamic_component(skill_name, data)
            if dynamic_instruction:
                return dynamic_instruction

        return None

    def get_display_info(self, skill_name: str) -> Dict[str, str]:
        """获取技能的显示名称和描述"""
        metadata = self._discovery.get_by_name(skill_name)
        if metadata:
            return {
                "display_name": metadata.display_name or skill_name,
                "description": metadata.description or "",
            }
        return {
            "display_name": skill_name,
            "description": "",
        }

    def _extract_ui_component_from_data(
        self, skill_name: str, data: Any
    ) -> Optional[V4RenderInstruction]:
        """
        从数据中提取预定义的 UI 组件配置

        web_search 等技能会返回 {"for_llm": {...}, "for_ui": {"components": [...]}} 格式
        """
        if not isinstance(data, dict):
            return None

        # 检查 for_ui.components 格式
        for_ui = data.get("for_ui", {})
        if isinstance(for_ui, dict):
            components = for_ui.get("components", [])
            if components and isinstance(components, list) and len(components) > 0:
                first_comp = components[0]
                if isinstance(first_comp, dict):
                    component_name = first_comp.get("component", "")
                    comp_data = first_comp.get("data", {})
                    if component_name and comp_data:
                        if not _validate_component_data_quality(component_name, comp_data):
                            logger.warning(
                                f"[UISelector] Skipped component '{component_name}' for '{skill_name}': "
                                f"data quality insufficient"
                            )
                        else:
                            logger.debug(f"[UISelector] Extracted UI from for_ui.components: {component_name}")
                            return V4RenderInstruction(
                                component=component_name,
                                confidence=0.9,
                                title=comp_data.get("title", skill_name),
                                data=comp_data,
                            )

        # 检查顶层 components 格式
        components = data.get("components", [])
        if components and isinstance(components, list) and len(components) > 0:
            first_comp = components[0]
            if isinstance(first_comp, dict):
                component_name = first_comp.get("component", "")
                comp_data = first_comp.get("data", {})
                if component_name and comp_data:
                    if not _validate_component_data_quality(component_name, comp_data):
                        logger.warning(
                            f"[UISelector] Skipped component '{component_name}' for '{skill_name}': "
                            f"data quality insufficient"
                        )
                    else:
                        logger.debug(f"[UISelector] Extracted UI from components: {component_name}")
                        return V4RenderInstruction(
                            component=component_name,
                            confidence=0.9,
                            title=comp_data.get("title", skill_name),
                            data=comp_data,
                        )

        return None

    def _extract_all_ui_components_from_data(self, skill_name: str, data: Any) -> List[V4RenderInstruction]:
        """
        从 skill 返回数据中提取所有 UI 组件配置（支持多组件返回）

        与 _extract_ui_component_from_data 不同，此方法返回 for_ui.components 中的
        所有组件而非仅第一个，适用于需要同时展示多个 UI 组件的场景
        （如 flight_tracker_card + map_binary_view + insight_highlights）。

        Returns: List[V4RenderInstruction]
        """
        results = []
        # Unwrap list-wrapped data (e.g. flight_tracker returns [{results, components}])
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            data = data[0]
        if not isinstance(data, dict):
            return results

        # 检查 for_ui.components 格式
        skipped = []
        for_ui = data.get("for_ui", {})
        if isinstance(for_ui, dict):
            components = for_ui.get("components", [])
            if components and isinstance(components, list):
                for comp in components:
                    if isinstance(comp, dict):
                        component_name = comp.get("component", "")
                        comp_data = comp.get("data", {})
                        if component_name and comp_data:
                            if not _validate_component_data_quality(component_name, comp_data):
                                skipped.append(component_name)
                                logger.warning(
                                    f"[UISelector] Skipped component '{component_name}' for '{skill_name}': "
                                    f"data quality insufficient, falling through to Layer 2/3"
                                )
                                continue
                            results.append(V4RenderInstruction(
                                component=component_name,
                                confidence=0.9,
                                title=comp_data.get("title", skill_name),
                                data=comp_data,
                            ))

        # 检查顶层 components 格式（仅在 for_ui 未命中时）
        if not results:
            components = data.get("components", [])
            if components and isinstance(components, list):
                for comp in components:
                    if isinstance(comp, dict):
                        component_name = comp.get("component", "")
                        comp_data = comp.get("data", {})
                        if component_name and comp_data:
                            if not _validate_component_data_quality(component_name, comp_data):
                                skipped.append(component_name)
                                logger.warning(
                                    f"[UISelector] Skipped component '{component_name}' for '{skill_name}': "
                                    f"data quality insufficient, falling through to Layer 2/3"
                                )
                                continue
                            results.append(V4RenderInstruction(
                                component=component_name,
                                confidence=0.9,
                                title=comp_data.get("title", skill_name),
                                data=comp_data,
                            ))

        if results:
            logger.debug(f"[UISelector] Extracted {len(results)} UI components from data for '{skill_name}': "
                         f"{[r.component for r in results]}"
                         f"{f', skipped={skipped}' if skipped else ''}")
        elif skipped:
            logger.info(f"[UISelector] All {len(skipped)} components skipped for '{skill_name}' "
                        f"due to quality check: {skipped}, falling through to Layer 2/3")

        return results

    def _transform_data_for_ui(self, skill_name: str, data: Any) -> Dict[str, Any]:
        """
        转换数据格式以匹配 UI 组件的期望格式

        不同技能的输出格式不同，需要转换成 UI 组件期望的格式
        """
        if not isinstance(data, dict):
            return {"raw_data": data}

        # web_search 数据转换
        if skill_name in ("web_search", "mcp__sthg_v4_skills__web_search"):
            return self._transform_web_search_data(data)

        # 通用技能数据转换：确保 items/columns 格式的数据能正确渲染为表格
        return self._transform_skill_data(data)

    def _transform_skill_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        通用技能数据转换：确保数据能匹配 UI 组件期望的格式

        处理的情况：
        1. 已有 items + columns → 标准 data_table 格式，直接返回
        2. 有 data 字段包含字典列表 → 转换为 items + columns 格式
        3. 有 daily 字段包含字典列表 → 转换为 items + columns 格式
        4. 其他 → 直接返回
        """
        # 1. 如果已经有 items 和 columns，确保 rows 也可用（data_table 兼容）
        if "items" in data and isinstance(data["items"], list):
            if "columns" in data and isinstance(data["columns"], list):
                # 标准格式，已经可用
                if "rows" not in data:
                    data["rows"] = data["items"]
                return data

        # 2. 检查常见的数组字段并转换为 data_table 格式
        for array_key in ["data", "daily", "records", "list", "results"]:
            if array_key in data and isinstance(data[array_key], list):
                array_data = data[array_key]
                if len(array_data) > 0 and isinstance(array_data[0], dict):
                    # 提取列定义
                    columns = self._extract_columns_from_dict_list(array_data)
                    # 提取元数据（非列表/字典的简单值）
                    meta = {}
                    for k, v in data.items():
                        if k != array_key and isinstance(v, (str, int, float, bool)):
                            meta[k] = v
                    # 把转换后的结构合并回原数据
                    data["rows"] = array_data[:100]
                    data["items"] = array_data[:100]
                    if "columns" not in data:
                        data["columns"] = columns
                    return data

        return data

    def _transform_web_search_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        转换 web_search 数据为 InsightHighlights 组件期望的格式

        web_search 返回格式:
        - for_llm.results: 搜索结果列表
        - intelligent_data.collected_data: 智能搜索收集的数据
        - _raw_results: 原始搜索结果
        """
        insights = []

        # 1. 尝试从 intelligent_data.collected_data 提取
        intelligent_data = data.get("intelligent_data", {})
        if isinstance(intelligent_data, dict):
            collected_data = intelligent_data.get("collected_data", [])
            if collected_data:
                for item in collected_data:
                    if isinstance(item, dict):
                        insights.append({
                            "type": "news",
                            "title": item.get("title") or item.get("data", "")[:100],
                            "content": item.get("data", ""),
                            "source": self._extract_domain(item.get("source", "")),
                            "link": item.get("source", ""),
                            "date": "",
                        })
                if insights:
                    return {
                        "query": data.get("query", ""),
                        "query_type": "search",
                        "insights": insights,
                        "total_count": len(insights),
                    }

        # 2. 尝试从 for_llm.results 提取
        for_llm = data.get("for_llm", {})
        if isinstance(for_llm, dict):
            results = for_llm.get("results", [])
            if results:
                for item in results:
                    if isinstance(item, dict):
                        insights.append({
                            "type": "news",
                            "title": item.get("title", ""),
                            "content": item.get("snippet", "") or item.get("full_content", "")[:500],
                            "source": item.get("source", "") or self._extract_domain(item.get("link", "")),
                            "link": item.get("link", ""),
                            "date": item.get("date", ""),
                        })
                if insights:
                    return {
                        "query": for_llm.get("query", data.get("query", "")),
                        "query_type": for_llm.get("query_type", "search"),
                        "insights": insights,
                        "total_count": len(insights),
                    }

        # 3. 尝试从 _raw_results 提取
        raw_results = data.get("_raw_results", [])
        if raw_results:
            for item in raw_results:
                if isinstance(item, dict):
                    insights.append({
                        "type": "news",
                        "title": item.get("title", ""),
                        "content": item.get("snippet", "") or item.get("description", ""),
                        "source": item.get("source", "") or self._extract_domain(item.get("link", "")),
                        "link": item.get("link", ""),
                        "date": item.get("date", ""),
                    })
            if insights:
                return {
                    "query": data.get("query", ""),
                    "query_type": "search",
                    "insights": insights,
                    "total_count": len(insights),
                }

        # 4. 尝试从 results 提取
        results = data.get("results", [])
        if results:
            for item in results:
                if isinstance(item, dict):
                    insights.append({
                        "type": "news",
                        "title": item.get("title", ""),
                        "content": item.get("snippet", "") or item.get("description", ""),
                        "source": item.get("source", "") or self._extract_domain(item.get("link", "")),
                        "link": item.get("link", ""),
                        "date": item.get("date", ""),
                    })
            if insights:
                return {
                    "query": data.get("query", ""),
                    "query_type": "search",
                    "insights": insights,
                    "total_count": len(insights),
                }

        # 5. 如果都没有，返回原始数据
        return data

    def _extract_domain(self, url: str) -> str:
        """从 URL 中提取域名"""
        if not url:
            return ""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            domain = parsed.netloc
            # 移除 www. 前缀
            if domain.startswith("www."):
                domain = domain[4:]
            return domain
        except Exception:
            return ""

    def _infer_component_from_data(
        self, skill_name: str, data: Any
    ) -> Optional[V4RenderInstruction]:
        """
        根据数据结构智能推断 UI 组件

        优先级：
        1. insights 字段 -> insight_highlights
        2. 列表数据 -> data_table
        3. 动态组件 fallback -> dynamic_card/dynamic_table/等
        """
        if not isinstance(data, dict):
            # 如果数据不是字典，尝试动态组件生成
            if self._enable_dynamic_fallback:
                return self._generate_dynamic_component(skill_name, data)
            return None

        # 检查是否有 insights 字段
        if "insights" in data:
            return V4RenderInstruction(
                component="insight_highlights",
                confidence=0.8,
                title=skill_name,
                data=data,
            )

        # 检查是否有列表数据
        for key, value in data.items():
            if isinstance(value, list) and len(value) > 0:
                first_item = value[0]
                if isinstance(first_item, dict):
                    # 有结构化列表数据，使用 data_table
                    return V4RenderInstruction(
                        component="data_table",
                        confidence=0.6,
                        title=skill_name,
                        data={"rows": value},
                    )

        # 动态组件 fallback：当智能推断失败时，尝试生成动态组件
        if self._enable_dynamic_fallback:
            return self._generate_dynamic_component(skill_name, data)

        return None

    def _generate_dynamic_component(
        self, skill_name: str, data: Any
    ) -> Optional[V4RenderInstruction]:
        """
        动态生成 UI 组件（当没有匹配的预定义组件时）

        该方法基于数据结构分析，自动选择合适的组件类型并生成渲染指令。
        支持的动态组件类型:
        - dynamic_card: 通用卡片组件，展示键值对数据
        - dynamic_table: 动态表格，展示列表数据
        - dynamic_json_tree: JSON 树形展示，用于复杂嵌套数据
        - dynamic_key_value: 键值对展示

        Args:
            skill_name: 技能名称
            data: 技能输出数据

        Returns:
            V4RenderInstruction 或 None
        """
        if data is None:
            return None

        logger.info(f"[UISelector] Generating dynamic component for {skill_name}")

        try:
            # 分析数据结构，决定使用哪种动态组件
            component_type, processed_data = self._analyze_data_for_dynamic_component(data)

            if component_type:
                # 获取技能的显示名称
                display_info = self.get_display_info(skill_name)
                title = display_info.get("display_name", skill_name)

                logger.info(
                    f"[UISelector] Dynamic component generated: type={component_type}, "
                    f"skill={skill_name}"
                )

                return V4RenderInstruction(
                    component=component_type,
                    confidence=0.7,  # 动态组件的置信度稍低于预定义组件
                    title=title,
                    data=processed_data,
                    is_dynamic=True,
                )

        except Exception as e:
            logger.warning(f"[UISelector] Dynamic component generation failed: {e}")
            import traceback
            logger.debug(traceback.format_exc())

        return None

    def _analyze_data_for_dynamic_component(
        self, data: Any
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """
        分析数据结构，决定使用哪种动态组件

        Returns:
            (component_type, processed_data) 或 (None, None)
        """
        if data is None:
            return None, None

        # 1. 字符串类型 -> 简单文本卡片
        if isinstance(data, str):
            return "dynamic_text", {"content": data}

        # 2. 列表类型
        if isinstance(data, list):
            if len(data) == 0:
                return "dynamic_empty", {"message": "暂无数据"}

            first_item = data[0]

            # 2.1 列表中是字典 -> 表格
            if isinstance(first_item, dict):
                # 提取列定义
                columns = self._extract_columns_from_dict_list(data)
                return "dynamic_table", {
                    "columns": columns,
                    "rows": data[:100],  # 限制行数
                    "total": len(data),
                }

            # 2.2 列表中是简单类型 -> 简单列表
            return "dynamic_list", {
                "items": data[:50],
                "total": len(data),
            }

        # 3. 字典类型
        if isinstance(data, dict):
            # 3.1 检查是否有明确的数据字段
            for list_key in ["items", "results", "data", "rows", "records", "list"]:
                if list_key in data and isinstance(data[list_key], list):
                    list_data = data[list_key]
                    if len(list_data) > 0 and isinstance(list_data[0], dict):
                        columns = self._extract_columns_from_dict_list(list_data)
                        # 保留其他元数据
                        meta = {k: v for k, v in data.items() if k != list_key and not isinstance(v, (list, dict))}
                        return "dynamic_table", {
                            "columns": columns,
                            "rows": list_data[:100],
                            "total": len(list_data),
                            "meta": meta,
                        }

            # 3.2 检查是否是简单的键值对（没有嵌套的复杂结构）
            simple_kvs = {}
            complex_parts = {}
            for k, v in data.items():
                if isinstance(v, (str, int, float, bool, type(None))):
                    simple_kvs[k] = v
                else:
                    complex_parts[k] = v

            # 如果大部分是简单值，使用键值对卡片
            if len(simple_kvs) > len(complex_parts) and len(simple_kvs) > 0:
                return "dynamic_card", {
                    "fields": self._format_key_value_pairs(simple_kvs),
                    "nested": complex_parts if complex_parts else None,
                }

            # 3.3 复杂嵌套结构 -> JSON 树
            return "dynamic_json_tree", {
                "data": data,
                "collapsed_depth": 2,
            }

        # 4. 其他类型
        return "dynamic_text", {"content": str(data)}

    def _extract_columns_from_dict_list(self, data: List[Dict]) -> List[Dict[str, str]]:
        """
        从字典列表中提取列定义
        """
        if not data:
            return []

        # 收集所有出现的键
        all_keys = set()
        for item in data[:10]:  # 只检查前10条
            if isinstance(item, dict):
                all_keys.update(item.keys())

        # 常见字段的显示名称映射
        field_labels = {
            "ts_code": "代码",
            "name": "名称",
            "title": "标题",
            "date": "日期",
            "trade_date": "交易日",
            "price": "价格",
            "close": "收盘价",
            "open": "开盘价",
            "high": "最高价",
            "low": "最低价",
            "volume": "成交量",
            "amount": "成交额",
            "change": "涨跌",
            "pct_chg": "涨跌幅(%)",
            "pe": "市盈率",
            "pb": "市净率",
            "total_mv": "总市值",
            "circ_mv": "流通市值",
            "industry": "行业",
            "area": "地区",
            "market": "市场",
            "status": "状态",
            "url": "链接",
            "link": "链接",
            "source": "来源",
            "content": "内容",
            "description": "描述",
            "score": "评分",
            "value": "数值",
        }

        columns = []
        for key in sorted(all_keys):
            label = field_labels.get(key, key)
            columns.append({
                "key": key,
                "label": label,
                "width": "auto",
            })

        return columns

    def _format_key_value_pairs(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        格式化键值对用于显示
        """
        # 字段标签映射
        field_labels = {
            "ts_code": "股票代码",
            "symbol": "证券代码",
            "name": "名称",
            "stock_name": "股票名称",
            "industry": "所属行业",
            "area": "地区",
            "market": "市场",
            "price": "当前价格",
            "close": "收盘价",
            "pre_close": "昨收价",
            "open": "开盘价",
            "high": "最高价",
            "low": "最低价",
            "change": "涨跌额",
            "pct_chg": "涨跌幅(%)",
            "pe": "市盈率(PE)",
            "pe_ttm": "市盈率TTM",
            "pb": "市净率(PB)",
            "total_mv": "总市值",
            "circ_mv": "流通市值",
            "volume": "成交量",
            "vol": "成交量(股)",
            "amount": "成交额",
            "turnover": "换手率",
            "turnover_rate": "换手率(%)",
            "update_time": "更新时间",
            "trade_date": "交易日期",
            "score": "评分",
            "total_score": "总评分",
            "rating": "评级",
            "direction": "方向",
            "probability": "概率",
            "confidence": "置信度",
            "roe": "净资产收益率(ROE)",
            "eps": "每股收益(EPS)",
            "revenue": "营业收入",
            "net_profit": "净利润",
            "gross_margin": "毛利率",
            "net_margin": "净利率",
            "dividend_yield": "股息率",
            "main_net_inflow": "主力净流入",
            "sentiment_score": "情绪分数",
        }

        formatted = []
        for key, value in data.items():
            if value is None:
                continue
            label = field_labels.get(key, key)
            # 格式化值
            if isinstance(value, float):
                if abs(value) >= 100000000:  # 亿
                    formatted_value = f"{value/100000000:.2f}亿"
                elif abs(value) >= 10000:  # 万
                    formatted_value = f"{value/10000:.2f}万"
                else:
                    formatted_value = f"{value:.4f}" if abs(value) < 1 else f"{value:.2f}"
            else:
                formatted_value = str(value)

            formatted.append({
                "key": key,
                "label": label,
                "value": formatted_value,
                "raw_value": value,
            })

        return formatted

    @staticmethod
    def _check_hints(hints: List[str], data: Dict[str, Any]) -> float:
        """
        检查 data_hints 匹配度

        Returns:
            0.0 ~ 1.0 的 confidence 值（匹配的 hint 比例）
        """
        if not hints:
            return 0.5

        matched = 0
        total = len(hints)

        for hint in hints:
            checker = _HINT_CHECKERS.get(hint)
            if checker:
                try:
                    if checker(data):
                        matched += 1
                except Exception:
                    pass
            else:
                # 未知的 hint，尝试作为 has_{key} 处理
                if hint.startswith("has_"):
                    key = hint[4:]
                    if _has_key(data, key):
                        matched += 1

        return matched / total if total > 0 else 0.0


# ========== 辅助函数 ==========

def _has_key(data: Any, key: str) -> bool:
    """检查数据中是否有指定键"""
    if isinstance(data, dict):
        if key in data:
            return True
        # 检查 content 嵌套
        content = data.get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    try:
                        import json
                        parsed = json.loads(item.get("text", ""))
                        if isinstance(parsed, dict) and key in parsed:
                            return True
                    except (json.JSONDecodeError, TypeError):
                        pass
    return False


def _has_list_data(data: Any) -> bool:
    """检查数据中是否有列表/数组数据"""
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and len(v) > 0:
                return True
    return False


def _key_equals(data: Any, key: str, value: str) -> bool:
    """检查数据中指定键的值"""
    if isinstance(data, dict):
        return str(data.get(key, "")).lower() == value.lower()
    return False


def _has_ohlc_data(data: Any) -> bool:
    """检查数据中是否有 OHLC（开高低收）数据"""
    if isinstance(data, dict):
        # 直接检查顶层
        if all(k in data for k in ("open", "high", "low", "close")):
            return True
        # 检查 data/items/rows 数组中的第一条记录
        for key in ("data", "items", "rows"):
            arr = data.get(key)
            if isinstance(arr, list) and len(arr) > 0:
                first = arr[0]
                if isinstance(first, dict) and all(k in first for k in ("open", "high", "low", "close")):
                    return True
    return False


def _has_intelligent_data(data: Any) -> bool:
    """检查数据中是否有智能搜索的 collected_data"""
    if isinstance(data, dict):
        # 直接检查 intelligent_data.collected_data
        intelligent_data = data.get("intelligent_data", {})
        if isinstance(intelligent_data, dict):
            collected = intelligent_data.get("collected_data", [])
            if isinstance(collected, list) and len(collected) > 0:
                return True
        # 或者检查顶层的 collected_data
        if data.get("collected_data"):
            return True
    return False
