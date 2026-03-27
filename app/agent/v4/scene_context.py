"""
SceneContext — 场景化 Tab 系统核心数据结构

职责:
1. 管理场景类型推断（LLM 驱动）
2. 管理 Tab 生命周期（创建/复用/切换）
3. 组件布局决策（从 SKILL.md scene_roles 声明读取，无需 LLM）
4. 为前端 SceneTabManager 提供 SSE 事件数据

场景类型（从 app/config/ui/scenes.json 加载）:
- default: 默认（通用分析，组件纵向堆叠）
- map: 地图分析（涉及空间位置的所有分析）
"""
import asyncio
import json
import uuid
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple
from loguru import logger


# ========== 数据结构 ==========

@dataclass
class SceneLayer:
    """场景图层（地图叠加数据）"""
    layer_id: str
    layer_type: str  # markers / polyline / polygon / heatmap / geojson
    data: Any = None
    style: Dict[str, Any] = field(default_factory=dict)
    visible: bool = True
    z_index: int = 0


@dataclass
class ScenePanel:
    """场景面板（侧栏/底栏信息面板）"""
    panel_id: str
    panel_type: str  # info / timeline / metrics / list / chart
    title: str = ""
    data: Any = None
    position: str = "right"  # right / bottom / left


@dataclass
class SceneTab:
    """场景 Tab"""
    tab_id: str
    scene_type: str
    title: str
    created_at: float = field(default_factory=time.time)
    layers: List[SceneLayer] = field(default_factory=list)
    panels: List[ScenePanel] = field(default_factory=list)
    standalone_components: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tab_id": self.tab_id,
            "scene_type": self.scene_type,
            "title": self.title,
            "created_at": self.created_at,
            "layers": [
                {
                    "layer_id": l.layer_id,
                    "layer_type": l.layer_type,
                    "data": l.data,
                    "style": l.style,
                    "visible": l.visible,
                    "z_index": l.z_index,
                }
                for l in self.layers
            ],
            "panels": [
                {
                    "panel_id": p.panel_id,
                    "panel_type": p.panel_type,
                    "title": p.title,
                    "data": p.data,
                    "position": p.position,
                }
                for p in self.panels
            ],
            "standalone_components": self.standalone_components,
        }


# ========== 场景配置加载 ==========

_scene_config_cache: Optional[Dict[str, Any]] = None


def _load_scene_config() -> Dict[str, Any]:
    """
    从 app/config/ui/scenes.json 加载场景配置，构建 labels 和 canvas 映射。
    结果缓存，只加载一次。

    Returns:
        {
            "labels": {"default": "默认", "map": "地图分析", ...},
            "canvas": {"default": None, "map": "map", ...},
            "raw": [原始 scenes 列表]
        }
    """
    global _scene_config_cache
    if _scene_config_cache is not None:
        return _scene_config_cache

    config_path = Path(__file__).resolve().parent.parent.parent / "config" / "ui" / "scenes.json"
    labels: Dict[str, str] = {}
    canvas: Dict[str, Optional[str]] = {}
    raw: List[Dict[str, Any]] = []

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        raw = data.get("scenes", [])
        for scene in raw:
            scene_type = scene["type"]
            labels[scene_type] = scene.get("label", scene_type)
            canvas[scene_type] = scene.get("canvas_type")
        logger.info(f"[SceneContext] Loaded {len(raw)} scene definitions from {config_path}")
    except FileNotFoundError:
        logger.warning(f"[SceneContext] scenes.json not found at {config_path}, using empty config")
    except Exception as e:
        logger.error(f"[SceneContext] Failed to load scenes.json: {e}")

    # 加载地图类 skill 列表（LLM 推断失败时的规则兜底）
    map_skills: List[str] = data.get("map_skills", []) if raw else []

    _scene_config_cache = {"labels": labels, "canvas": canvas, "raw": raw, "map_skills": map_skills}
    return _scene_config_cache


def _reset_scene_config_cache():
    """Reset cached scene config (for testing or config reload)"""
    global _scene_config_cache
    _scene_config_cache = None


# ========== SceneContext ==========

class SceneContext:
    """
    场景上下文 — 管理单次请求的场景状态

    生命周期:
    1. build_hooks() 时创建
    2. 首个 Skill 完成后触发 infer_scene()（同时判断 Tab 复用）
    3. 后续 Skill 通过 classify_component_layout() 从 SKILL.md 声明读取布局
    4. Stop hook 中持久化到 session metadata
    """

    def __init__(self, user_specified_scene: Optional[str] = None):
        # 场景类型（推断或指定）
        self.scene_type: Optional[str] = user_specified_scene
        self.user_specified_scene: Optional[str] = user_specified_scene
        self.canvas_type: Optional[str] = None
        self.canvas_config: Dict[str, Any] = {}

        # Tab 管理
        self.active_tab_id: Optional[str] = None

        # 推断状态
        self._inferred: bool = bool(user_specified_scene)
        self._infer_lock = asyncio.Lock()

        # 观察到的技能
        self.observed_skills: List[str] = []
        self._role_map: Dict[str, str] = {}  # skill_name -> role

        # 待推送的数据
        self._pending_layers: List[SceneLayer] = []
        self._pending_panels: List[ScenePanel] = []

        # LLM 推断的 Tab 复用 ID
        self.reuse_tab_id: Optional[str] = None
        # LLM 推断的场景标题
        self._inferred_title: str = ""

        # 如果用户指定了场景，从配置中查找画布类型
        if user_specified_scene:
            cfg = _load_scene_config()
            if user_specified_scene in cfg["canvas"]:
                self.canvas_type = cfg["canvas"][user_specified_scene]

    def preset_from_phase0(self, scene_info: Dict):
        """从 Phase 0 预设场景信息，跳过独立 LLM 推断"""
        if self.user_specified_scene:
            return
        self.scene_type = scene_info.get("scene_type", "default")
        cfg = _load_scene_config()
        self.canvas_type = cfg["canvas"].get(self.scene_type)
        self.canvas_config = {"tile": scene_info.get("tile", "street")}
        self.reuse_tab_id = scene_info.get("reuse_tab_id")
        self._inferred_title = scene_info.get("title", "")
        self._inferred = True
        logger.info(
            f"[SceneContext] Preset from Phase 0: scene={self.scene_type}, "
            f"title={self._inferred_title}, reuse_tab={self.reuse_tab_id}"
        )

    async def infer_scene(
        self,
        query: str,
        skill_names: List[str],
        api_key: str = "",
        base_url: str = "",
        fast_model: str = "",
        existing_tabs: Optional[List[Dict]] = None,
    ) -> str:
        """
        通过 LLM 推断场景类型，同时判断是否复用已有 Tab

        Args:
            query: 用户查询
            skill_names: 已调用的技能名列表
            api_key: (已弃用) API Key，由 llm_provider 内部管理
            base_url: (已弃用) API Base URL，由 llm_provider 内部管理
            fast_model: (已弃用) 模型名，由 llm_provider 内部管理
            existing_tabs: 已有的场景 Tab 列表（用于 LLM 判断 Tab 复用）

        Returns:
            推断的场景类型
        """
        async with self._infer_lock:
            if self._inferred:
                return self.scene_type or "default"

            try:
                from app.prompts.loader import load_prompt

                # 构建 prompt（增加已有 Tab 信息供 LLM 判断复用）
                tabs_info = ""
                if existing_tabs:
                    tab_lines = []
                    for t in existing_tabs:
                        tab_lines.append(
                            f"  - {t['tab_id']}: {t.get('scene_type', 'default')} 场景, "
                            f"标题 \"{t.get('title', '')}\""
                        )
                    tabs_info = "\n已有场景 Tab:\n" + "\n".join(tab_lines)

                prompt = (
                    f"用户查询: {query}\n"
                    f"已调用技能: {', '.join(skill_names)}"
                    f"{tabs_info}"
                )

                system_prompt = load_prompt("scene_infer_system")

                result = await self._call_llm(
                    prompt=prompt,
                    system=system_prompt,
                    api_key=api_key,
                    base_url=base_url,
                    model=fast_model,
                    max_tokens=300,
                )

                parsed = self._parse_json(result)
                inferred_type = parsed.get("scene_type", "default")
                confidence = parsed.get("confidence", 0.0)

                if confidence < 0.5:
                    inferred_type = "default"

                cfg = _load_scene_config()
                self.scene_type = inferred_type
                self.canvas_type = cfg["canvas"].get(inferred_type)
                self.canvas_config = {"tile": parsed.get("tile", "street")}
                self.reuse_tab_id = parsed.get("reuse_tab_id")  # LLM 指定的复用 Tab
                self._inferred_title = parsed.get("title", "")
                self._inferred = True

                logger.info(
                    f"[SceneContext] Inferred scene: {inferred_type} "
                    f"(conf={confidence}, title={parsed.get('title', '')}, "
                    f"tile={self.canvas_config.get('tile')}, reuse_tab={self.reuse_tab_id})"
                )
                return inferred_type

            except Exception as e:
                logger.warning(f"[SceneContext] Scene inference failed: {e}")
                # 规则兜底：根据 skill_names 判断是否为地图类场景
                cfg = _load_scene_config()
                map_skills_set = set(cfg.get("map_skills", []))
                if map_skills_set and any(s in map_skills_set for s in skill_names):
                    self.scene_type = "map"
                    self.canvas_type = cfg["canvas"].get("map")
                    self.canvas_config = {"tile": "street"}
                    logger.info(
                        f"[SceneContext] Fallback to map by skill_names: "
                        f"{[s for s in skill_names if s in map_skills_set]}"
                    )
                else:
                    self.scene_type = "default"
                self._inferred = True
                return self.scene_type

    def classify_component_layout(
        self,
        component_name: str,
        skill_metadata: Optional[Dict] = None,
        data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        根据数据特征智能判定组件在场景中的布局角色。
        **数据驱动**，不硬编码组件名 → 角色映射。

        三模式渲染（map 场景）:
        - poi:        经纬度点打标记 — L.marker + popup 详情
        - layer:      面/线/区域/轨迹覆盖 — L.geoJSON/polyline/circle/tileLayer
        - overlay:    无经纬度浮窗 — CSS 可拖拽折叠窗口

        判定优先级:
        1. 数据特征检测: layer > poi > overlay（轨迹优先于打点）
        2. SKILL.md scene_roles 声明（仅在数据无 geo 特征时作为兜底参考）
        3. 最终兜底 → map 场景 overlay / 其他 standalone

        Returns:
            {"role": "poi|layer|overlay|standalone", "position": "canvas|...", "z_index": 0}
            或 {} 表示 default 场景无布局提示
        """
        if not self.scene_type or self.scene_type == "default":
            return {}  # default 场景无布局提示

        # 优先级1: 数据特征检测（layer 优先于 poi — 轨迹比打点更重要）
        if self.scene_type == "map" and isinstance(data, dict):
            has_layer = self._has_layer_data(data)
            has_poi = self._has_poi_data(data)
            if has_layer:
                return self._build_layout_hint("layer")
            if has_poi:
                return self._build_layout_hint("poi")

        # 优先级2: SKILL.md scene_roles 声明（仅在数据无 geo 特征时作为兜底参考）
        if skill_metadata and skill_metadata.get("ui_components"):
            for comp_def in skill_metadata["ui_components"]:
                if comp_def.get("component") == component_name:
                    roles = comp_def.get("scene_roles", {})
                    role = roles.get(self.scene_type)
                    if role:
                        return self._build_layout_hint(role)

        # 优先级3: 最终兜底
        if self.scene_type == "map":
            return self._build_layout_hint("overlay")
        return self._build_layout_hint("standalone")

    def _build_layout_hint(self, role: str) -> Dict[str, Any]:
        """构建 layout_hint 字典（panel 已废弃，自动降级为 overlay）"""
        if role == "panel":
            role = "overlay"  # panel 已废弃，路由到 overlay
        position = {
            "poi": "canvas",
            "layer": "canvas",
            "overlay": "canvas",
            "standalone": "",
        }.get(role, "")
        z_index = {"poi": 100, "layer": 0, "overlay": 1001}.get(role, 0)
        return {"role": role, "position": position, "z_index": z_index}

    @staticmethod
    def _has_poi_data(data: dict) -> bool:
        """检测是否含 POI 打点数据（markers/locations/lat+lng 等经纬度点）"""
        # markers 数组
        markers = data.get("markers")
        if isinstance(markers, list) and markers:
            if isinstance(markers[0], dict) and ("lat" in markers[0] or "coordinates" in markers[0]):
                return True
        # locations 数组
        locs = data.get("locations")
        if isinstance(locs, list) and locs:
            if isinstance(locs[0], dict) and ("lat" in locs[0] or "lng" in locs[0]):
                return True
        # 单点坐标
        if data.get("lat") and (data.get("lng") or data.get("lon")):
            return True
        # position 对象
        pos = data.get("position")
        if isinstance(pos, dict) and "lat" in pos:
            return True
        # primary_location
        pl = data.get("primary_location")
        if isinstance(pl, dict) and "lat" in pl:
            return True
        # coordinates 对象
        coords = data.get("coordinates")
        if isinstance(coords, dict) and ("lat" in coords or "lng" in coords):
            return True
        # 嵌套 map_data.markers
        md = data.get("map_data")
        if isinstance(md, dict) and isinstance(md.get("markers"), list) and md["markers"]:
            return True
        # 深度扫描: 一层子字典中的 position/lat+lng
        # 典型: flight_tracker_card → data.flight_info.position.{lat, lon}
        #        ship_tracker_card  → data.ship_info.position.{lat, lon}
        for key, val in data.items():
            if not isinstance(val, dict):
                continue
            # 子字典含 lat + lng/lon
            if val.get("lat") and (val.get("lng") or val.get("lon")):
                return True
            # 子字典含 position 对象
            sub_pos = val.get("position")
            if isinstance(sub_pos, dict) and "lat" in sub_pos:
                return True
        return False

    @staticmethod
    def _has_layer_data(data: dict) -> bool:
        """检测是否含图层覆盖数据（geojson/polyline/areas/circles/tracks/tile_url/trace）"""
        # GeoJSON
        if data.get("geojson") or data.get("boundary_geojson"):
            return True
        # 嵌套 map_data 中的 geojson/areas/circles
        md = data.get("map_data")
        if isinstance(md, dict) and (md.get("geojson") or md.get("areas") or md.get("circles")):
            return True
        # 轨迹/区域/圆圈
        if data.get("tracks") or data.get("areas") or data.get("circles"):
            return True
        # trace 轨迹点列表（如航班历史轨迹）
        trace = data.get("trace")
        if isinstance(trace, list) and len(trace) >= 2:
            return True
        # polyline
        if data.get("polyline"):
            return True
        ri = data.get("route_info")
        if isinstance(ri, dict) and ri.get("polyline"):
            return True
        # tile_url
        if data.get("tile_url") or data.get("tiles"):
            return True
        # geo_distribution（GeoJSON FeatureCollection）
        gd = data.get("geo_distribution")
        if isinstance(gd, dict) and gd.get("type") == "FeatureCollection":
            return True
        # 深度扫描: 一层子字典中的 trace/tracks
        # 典型: flight_tracker_card → data.flight_info.trace
        for key, val in data.items():
            if not isinstance(val, dict):
                continue
            sub_trace = val.get("trace")
            if isinstance(sub_trace, list) and len(sub_trace) >= 2:
                return True
            if val.get("tracks"):
                return True
        return False

    async def classify_skill_data(
        self,
        skill_name: str,
        data: Any,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        fast_model: str = "claude-sonnet-4-20250514",
    ) -> Dict[str, Any]:
        """
        [DEPRECATED] 分类 Skill 数据在场景中的角色。
        新架构使用 classify_component_layout() 从 SKILL.md scene_roles 声明中读取布局。
        保留此方法以向后兼容。

        Returns:
            {"role": "layer|panel|standalone", "panel_type": "...", "title": "..."}
        """
        self.observed_skills.append(skill_name)

        # 快速规则判断（避免不必要的 LLM 调用）
        quick_role = self._quick_classify(skill_name, data)
        if quick_role:
            self._role_map[skill_name] = quick_role["role"]
            return quick_role

        # LLM 分类
        try:
            from app.prompts.loader import load_prompt

            data_summary = self._summarize_data(data)
            prompt = (
                f"技能名: {skill_name}\n"
                f"场景类型: {self.scene_type}\n"
                f"数据摘要: {data_summary}"
            )

            system_prompt = load_prompt(
                "scene_classify_system",
                scene_type=self.scene_type,
                canvas_type=self.canvas_type or "",
            )

            result = await self._call_llm(
                prompt=prompt,
                system=system_prompt,
                api_key=api_key,
                base_url=base_url,
                model=fast_model,
                max_tokens=200,
            )

            parsed = self._parse_json(result)
            role = parsed.get("role", "standalone")
            self._role_map[skill_name] = role
            return parsed

        except Exception as e:
            logger.warning(f"[SceneContext] Data classify failed: {e}")
            fallback = {"role": "standalone", "panel_type": "info", "title": skill_name}
            self._role_map[skill_name] = "standalone"
            return fallback

    def find_or_create_tab(
        self,
        existing_tabs: List[Dict[str, Any]],
        title: str = "",
        reuse_tab_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], bool]:
        """
        查找或创建 Tab

        Args:
            existing_tabs: 现有 Tab 列表
            title: 新建 Tab 的标题
            reuse_tab_id: LLM 指定复用的 Tab ID

        Returns:
            (tab_dict, is_new)
        """
        # 优先：LLM 指定了复用某个 Tab
        if reuse_tab_id:
            for tab in existing_tabs:
                if tab["tab_id"] == reuse_tab_id:
                    self.active_tab_id = tab["tab_id"]
                    return tab, False

        # 降级：无 LLM 指定时按 scene_type 查找（保持向后兼容）
        for tab in existing_tabs:
            if tab.get("scene_type") == self.scene_type:
                self.active_tab_id = tab["tab_id"]
                return tab, False

        # 创建新 Tab
        cfg = _load_scene_config()
        tab_id = f"tab_{uuid.uuid4().hex[:8]}"
        label = cfg["labels"].get(self.scene_type, self.scene_type)
        # 标题优先级: 传入 title > LLM 推断的简短标题 > 场景标签
        # 截断到 20 字以内，避免用户原始长 query 变成 tab 标题
        raw_title = title or self._inferred_title or label
        if len(raw_title) > 20:
            raw_title = raw_title[:18] + "…"
        new_tab = {
            "tab_id": tab_id,
            "scene_type": self.scene_type,
            "title": raw_title,
            "created_at": time.time(),
            "canvas_type": self.canvas_type,
            "canvas_config": self.canvas_config,
            "layers": [],
            "panels": [],
            "standalone_components": [],
        }
        existing_tabs.append(new_tab)
        self.active_tab_id = tab_id
        return new_tab, True

    def _quick_classify(self, skill_name: str, data: Any) -> Optional[Dict[str, Any]]:
        """[DEPRECATED] 快速规则分类（无需 LLM）。新架构使用 SKILL.md scene_roles。"""
        # 地图相关技能 → layer
        map_skills = {
            "map_bindary", "geo_search", "satellite_imagery",
            "flight_tracker", "military_balance",
            "weather_realtime", "weather_forecast", "weather_alerts",
            "weather_visualization", "terrain_elevation", "city_3d_model",
        }
        if skill_name in map_skills:
            return {"role": "layer", "panel_type": "", "title": skill_name}

        # 含 GeoJSON 或坐标数据 → layer
        if isinstance(data, dict):
            if "geojson" in data or "markers" in data or "coordinates" in data:
                return {"role": "layer", "panel_type": "", "title": skill_name}
            # 含 insights/highlights → overlay（panel 已废弃）
            if "insights" in data or "highlights" in data:
                return {"role": "overlay", "panel_type": "info", "title": skill_name}

        # 股票相关技能 → standalone (自有组件)
        stock_skills = {
            "stock_query", "realtime_quote", "historical_data",
            "technical_indicators", "rating", "prediction",
            "money_flow", "valuation_analysis",
        }
        if skill_name in stock_skills:
            return {"role": "standalone", "panel_type": "", "title": skill_name}

        return None

    def _summarize_data(self, data: Any) -> str:
        """生成数据摘要（用于 LLM 分类）"""
        if data is None:
            return "null"
        if isinstance(data, dict):
            keys = list(data.keys())[:20]
            return f"dict keys: {keys}"
        if isinstance(data, list):
            if data and isinstance(data[0], dict):
                return f"list[{len(data)}] of dicts, first keys: {list(data[0].keys())[:10]}"
            return f"list[{len(data)}]"
        return str(type(data).__name__)

    @staticmethod
    async def _call_llm(
        prompt: str,
        system: str,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        max_tokens: int = 200,
    ) -> str:
        """调用 LLM（带 failover）

        保留原始签名兼容，内部委托 llm_provider。
        api_key/base_url/model 参数不再使用，由 provider 内部管理。
        """
        from agent_core.agentloop.llm_provider import call_llm
        return await call_llm(
            prompt,
            use_small_fast=True,
            system=system,
            max_tokens=max_tokens,
            timeout=30.0,
            temperature=0.2,
        )

    @staticmethod
    def _parse_json(text: str) -> Dict[str, Any]:
        """从 LLM 返回中提取 JSON"""
        import re
        text = text.strip()
        # 尝试直接解析
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass
        # 尝试提取 ```json ... ```
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except (json.JSONDecodeError, ValueError):
                pass
        # 尝试提取裸 JSON
        match = re.search(r'\{[^{}]*\}', text)
        if match:
            try:
                return json.loads(match.group(0))
            except (json.JSONDecodeError, ValueError):
                pass
        return {}
