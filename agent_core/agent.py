"""
V4 Agent 核心数据结构 -- V4AgentRequest + DataCollector

从 app/agent/v4/native_agent.py 抽取的纯数据类，
不依赖 FastAPI/Redis/MySQL。
"""
import re
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from datetime import datetime
from loguru import logger


# @skill 提及正则
_SKILL_MENTION_RE = re.compile(r"@([\w-]+)")

# render_mode 枚举常量
RENDER_MODE_AUTO = "auto"
RENDER_MODE_TEXT_ONLY = "text_only"

# channel 渠道常量
CHANNEL_WEB = "web"
CHANNEL_DINGTALK = "dingtalk"
CHANNEL_FEISHU = "feishu"
CHANNEL_API = "api"


@dataclass
class V4AgentRequest:
    """V4 Agent 请求"""
    message: str
    session_id: str = "default"
    user_id: Optional[int] = None
    ts_code: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, str]] = field(default_factory=list)
    output_format: Optional[str] = "markdown"
    knowledge_dir: Optional[str] = None
    extra_knowledge_dirs: List[str] = field(default_factory=list)
    render_mode: str = RENDER_MODE_AUTO
    auto_approve_plan: bool = False
    force_skills: List[str] = field(default_factory=list)
    skill_params: Dict[str, Any] = field(default_factory=dict)
    enable_report: bool = True
    channel: str = CHANNEL_WEB
    scene_type: Optional[str] = None
    report_lang: str = "zh"
    langfuse_trace: Any = None
    attached_files: List[Dict[str, Any]] = field(default_factory=list)
    callbacks: Any = None
    skip_memory: bool = False  # eval 模式：跳过记忆/蒸馏/反思等后处理写入


@dataclass
class DataCollector:
    """数据收集器，用于跟踪技能执行和数据来源"""
    skill_results: List[Dict[str, Any]] = field(default_factory=list)
    data_sources: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    last_web_search_mode: Optional[str] = None
    last_web_search_query: Optional[str] = None
    used_intelligent_mode: bool = False
    used_session_context: bool = False
    raw_data_urls: List[str] = field(default_factory=list)
    quality_focus: Optional[Dict[str, float]] = None
    quality_gaps: List[str] = field(default_factory=list)

    # SkillMetadataProvider 引用（延迟注入）
    _metadata_provider: Any = field(default=None, repr=False)

    def set_metadata_provider(self, provider) -> None:
        """注入 SkillMetadataProvider"""
        self._metadata_provider = provider

    def add_skill_result(self, skill_name: str, result: Any, duration_ms: int = 0):
        """添加技能执行结果"""
        self.skill_results.append({
            "skill": skill_name,
            "data": result if isinstance(result, dict) else {"result": str(result)},
            "timestamp": datetime.now().isoformat(),
            "duration_ms": duration_ms,
        })

    def add_data_source(self, name: str, url: str = None, source_type: str = "skill",
                        fetch_time: str = None, skill_name: str = None, **kwargs):
        """添加数据来源"""
        self.data_sources.append({
            "name": name,
            "url": url or "",
            "type": source_type,
            "fetch_time": fetch_time or datetime.now().strftime("%Y-%m-%d %H:%M"),
            "skill_name": skill_name,
        })

    def add_tool_call(self, name: str, input_data: Dict = None):
        """添加工具调用记录"""
        self.tool_calls.append({
            "name": name,
            "input": input_data or {},
            "timestamp": datetime.now().isoformat(),
        })

    def has_data(self) -> bool:
        """是否有收集到的数据"""
        return len(self.skill_results) > 0 or len(self.data_sources) > 0 or self.used_session_context

    def _get_executed_skill_names(self) -> List[str]:
        """获取所有已执行的 Skill 名称列表"""
        names = set()
        for r in self.skill_results:
            name = r.get("skill", "")
            if name:
                names.add(name)
        for s in self.data_sources:
            name = s.get("skill_name", "")
            if name:
                names.add(name)
        return list(names)

