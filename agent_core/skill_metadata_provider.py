"""
SkillMetadataProvider -- Skill 元数据统一查询入口

核心思路:
- 只存 authority 一个语义字段（在 SKILL.md 中声明）
- 其他属性（llm_label, multi_source, display_name, key_params, cache_ttl）
  全部自动推断或从 SkillMetadata 中读取
"""
from typing import Dict, List, Optional, Any
from loguru import logger


class SkillMetadataProvider:
    """Skill 元数据提供者 — 从 SkillDiscovery 读取 + 自动推断"""

    # authority → 默认属性映射
    _AUTHORITY_DEFAULTS = {
        "official_primary": {
            "llm_label": False, "multi_source": False,
            "description": "官方一手数据（交易所/政府/披露）",
        },
        "authoritative_org": {
            "llm_label": False, "multi_source": False,
            "description": "权威机构数据（国际组织/官方API）",
        },
        "academic_thinktank": {
            "llm_label": False, "multi_source": False,
            "description": "学术/智库数据",
        },
        "event_database": {
            "llm_label": False, "multi_source": False,
            "description": "事件/情报数据库",
        },
        "analysis_prediction": {
            "llm_label": False, "multi_source": False,
            "description": "分析预测（模型输出）",
        },
        "dynamic_collection": {
            "llm_label": True, "multi_source": True,
            "description": "动态采集（需评估URL）",
        },
        "unknown": {
            "llm_label": False, "multi_source": False,
            "description": "未分类技能",
        },
    }

    def __init__(self, discovery=None):
        """
        初始化 Provider

        Args:
            discovery: SkillDiscovery 实例（延迟设置也可以）
        """
        self._discovery = discovery

    def set_discovery(self, discovery) -> None:
        """设置或更新 SkillDiscovery 实例"""
        self._discovery = discovery

    def _get_metadata(self, name: str):
        """获取指定技能的 SkillMetadata"""
        if not self._discovery:
            return None
        return self._discovery.get_by_name(name)

    def _get_authority(self, name: str) -> str:
        """获取技能的 authority 值"""
        meta = self._get_metadata(name)
        if meta:
            return meta.authority
        return "unknown"

    def _get_authority_defaults(self, authority: str) -> Dict[str, Any]:
        """获取 authority 对应的默认属性"""
        return self._AUTHORITY_DEFAULTS.get(
            authority, self._AUTHORITY_DEFAULTS["unknown"]
        )

    # === 对外查询接口 ===

    def get_display_name(self, name: str) -> str:
        """获取技能的中文显示名（thinking_chain 用）"""
        meta = self._get_metadata(name)
        if meta and meta.display_name:
            return meta.display_name
        return name

    def get_key_params(self, name: str) -> List[str]:
        """获取技能的关键参数列表（message_compressor 用）"""
        meta = self._get_metadata(name)
        if meta and meta.key_params:
            return meta.key_params
        # 通用默认
        return ["query", "ts_code", "code", "url", "action", "topic"]

    def needs_llm_labeling(self, name: str) -> bool:
        """判断是否需要 LLM 打标（labeler 用）"""
        authority = self._get_authority(name)
        defaults = self._get_authority_defaults(authority)
        return defaults.get("llm_label", False)

    def get_cache_ttl(self, name: str) -> int:
        """获取技能的缓存 TTL（base.py ContextReuseStrategy 用）"""
        meta = self._get_metadata(name)
        if meta and meta.cache_ttl != 300:
            return meta.cache_ttl
        # 默认 300 秒
        return 300

    def get_dynamic_labeling_skills(self) -> set:
        """获取需要动态 URL 打标的技能集合（native_agent 用）"""
        result = set()
        if not self._discovery:
            return {"web_search", "osint_search", "deep_analysis"}

        for skill in self._discovery.get_all():
            authority = skill.authority
            defaults = self._get_authority_defaults(authority)
            if defaults.get("llm_label", False) or defaults.get("multi_source", False):
                result.add(skill.name)

        # 确保动态采集类技能始终包含
        if not result:
            result = {"web_search", "osint_search", "deep_analysis"}

        return result

    def get_all_display_names(self) -> Dict[str, str]:
        """获取所有技能的中文名映射（批量使用）"""
        if not self._discovery:
            return {}
        return {
            skill.name: skill.display_name or skill.name
            for skill in self._discovery.get_all()
        }

    def get_all_key_params(self) -> Dict[str, List[str]]:
        """获取所有技能的关键参数映射（批量使用）"""
        if not self._discovery:
            return {}
        result = {}
        for skill in self._discovery.get_all():
            if skill.key_params:
                result[skill.name] = skill.key_params
        return result

    def get_all_cache_ttls(self) -> Dict[str, int]:
        """获取所有技能的缓存 TTL 映射"""
        if not self._discovery:
            return {}
        result = {}
        for skill in self._discovery.get_all():
            if skill.cache_ttl != 300:
                result[skill.name] = skill.cache_ttl
        return result


# === 全局单例 ===

_provider: Optional[SkillMetadataProvider] = None


def get_skill_metadata_provider() -> SkillMetadataProvider:
    """获取全局 SkillMetadataProvider 单例"""
    global _provider
    if _provider is None:
        _provider = SkillMetadataProvider()
    return _provider


def init_skill_metadata_provider(discovery) -> SkillMetadataProvider:
    """初始化全局 SkillMetadataProvider（在 Agent 启动时调用）"""
    global _provider
    _provider = SkillMetadataProvider(discovery)
    logger.info("[SkillMetadataProvider] Initialized with discovery")
    return _provider
