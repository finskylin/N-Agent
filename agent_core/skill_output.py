"""
Skill 统一数据输出结构

定义 SkillOutput 和 CollectedItem 数据类，用于：
- 统一 Skill 执行后的输出格式
- 支持 UI 渲染、MySQL 持久化、MinIO 存储
- 提供下载链接生成能力

设计文档: doc_design/agent2/data_store.md
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional
import json
import uuid


@dataclass
class CollectedItem:
    """
    单条采集数据

    用于记录每次数据采集的详细信息，包括来源、内容和置信度标签
    """

    # === 来源信息 ===
    source_url: str = ""                          # 数据来源 URL
    source_name: str = ""                         # 来源名称 (如 "维基百科", "路透社")
    source_type: str = "unknown"                  # 来源类型 (official/media/community/academic/etc)

    # === 内容 ===
    title: str = ""                               # 标题
    content: str = ""                             # 正文内容
    snippet: str = ""                             # 摘要/片段

    # === 元数据 ===
    data_time: Optional[datetime] = None          # 数据时间（内容发布时间）
    fetch_time: Optional[datetime] = None         # 抓取时间
    content_type: str = "text"                    # 内容类型 (text/json/html/pdf/image)

    # === 置信度标签 ===
    authority_score: float = 0.5                  # 权威性评分 (0-1)
    labels: Dict[str, str] = field(default_factory=dict)  # 打标结果

    # === 扩展数据 ===
    extra: Dict[str, Any] = field(default_factory=dict)   # 其他扩展字段

    def to_dict(self) -> Dict[str, Any]:
        """转换为可序列化的字典"""
        result = {
            "source_url": self.source_url,
            "source_name": self.source_name,
            "source_type": self.source_type,
            "title": self.title,
            "content": self.content,
            "snippet": self.snippet,
            "data_time": self.data_time.isoformat() if self.data_time else None,
            "fetch_time": self.fetch_time.isoformat() if self.fetch_time else None,
            "content_type": self.content_type,
            "authority_score": self.authority_score,
            "labels": self.labels,
            "extra": self.extra,
        }
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CollectedItem":
        """从字典创建实例"""
        data_time = None
        if data.get("data_time"):
            try:
                data_time = datetime.fromisoformat(data["data_time"])
            except (ValueError, TypeError):
                pass

        fetch_time = None
        if data.get("fetch_time"):
            try:
                fetch_time = datetime.fromisoformat(data["fetch_time"])
            except (ValueError, TypeError):
                pass

        return cls(
            source_url=data.get("source_url", ""),
            source_name=data.get("source_name", ""),
            source_type=data.get("source_type", "unknown"),
            title=data.get("title", ""),
            content=data.get("content", ""),
            snippet=data.get("snippet", ""),
            data_time=data_time,
            fetch_time=fetch_time,
            content_type=data.get("content_type", "text"),
            authority_score=data.get("authority_score", 0.5),
            labels=data.get("labels", {}),
            extra=data.get("extra", {}),
        )


@dataclass
class SkillOutput:
    """
    Skill 统一输出结构

    所有 Skill 执行完成后应返回此结构，用于：
    1. 前端 UI 渲染 (ui_components)
    2. MySQL 持久化
    3. MinIO 大数据存储
    4. 报告生成
    """

    # === 元信息 ===
    skill_name: str                               # 技能名称
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 执行 ID (UUID)
    user_id: int = 1                              # 用户 ID
    session_id: str = ""                          # 会话 ID
    query: str = ""                               # 原始查询
    executed_at: datetime = field(default_factory=datetime.now)  # 执行时间
    duration_ms: int = 0                          # 执行耗时（毫秒）

    # === 核心数据 ===
    raw_data: List[CollectedItem] = field(default_factory=list)  # 原始采集数据列表

    # === UI 渲染 ===
    ui_components: List[Dict[str, Any]] = field(default_factory=list)  # 前端渲染组件配置

    # === 状态 ===
    success: bool = True
    error: Optional[str] = None

    # === 存储信息（由 OutputProcessor 填充）===
    db_id: Optional[int] = None                   # MySQL 记录 ID
    raw_data_url: Optional[str] = None            # MinIO 下载链接（大数据时）

    def to_dict(self) -> Dict[str, Any]:
        """转换为可序列化的字典"""
        return {
            "skill_name": self.skill_name,
            "execution_id": self.execution_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "query": self.query,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
            "duration_ms": self.duration_ms,
            "raw_data": [item.to_dict() for item in self.raw_data],
            "ui_components": self.ui_components,
            "success": self.success,
            "error": self.error,
            "db_id": self.db_id,
            "raw_data_url": self.raw_data_url,
        }

    def to_json(self) -> str:
        """序列化为 JSON 字符串"""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillOutput":
        """从字典创建实例"""
        executed_at = datetime.now()
        if data.get("executed_at"):
            try:
                executed_at = datetime.fromisoformat(data["executed_at"])
            except (ValueError, TypeError):
                pass

        raw_data = []
        for item_data in data.get("raw_data", []):
            raw_data.append(CollectedItem.from_dict(item_data))

        return cls(
            skill_name=data.get("skill_name", ""),
            execution_id=data.get("execution_id", str(uuid.uuid4())),
            user_id=data.get("user_id", 1),
            session_id=data.get("session_id", ""),
            query=data.get("query", ""),
            executed_at=executed_at,
            duration_ms=data.get("duration_ms", 0),
            raw_data=raw_data,
            ui_components=data.get("ui_components", []),
            success=data.get("success", True),
            error=data.get("error"),
            db_id=data.get("db_id"),
            raw_data_url=data.get("raw_data_url"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> "SkillOutput":
        """从 JSON 字符串创建实例"""
        return cls.from_dict(json.loads(json_str))

    @property
    def items_count(self) -> int:
        """数据条数"""
        return len(self.raw_data)

    @property
    def raw_data_size(self) -> int:
        """原始数据大小（字节）"""
        return len(self.serialize_raw_data())

    def serialize_raw_data(self) -> str:
        """序列化原始数据为 JSON"""
        return json.dumps(
            [item.to_dict() for item in self.raw_data],
            ensure_ascii=False
        )

    def get_raw_data_preview(self, limit: int = 3) -> List[Dict[str, Any]]:
        """获取原始数据预览（前 N 条）"""
        return [item.to_dict() for item in self.raw_data[:limit]]


@dataclass
class ProcessResult:
    """
    OutputProcessor 处理结果

    包含存储后的元信息和生成的事件
    """
    db_id: Optional[int] = None                   # MySQL 记录 ID
    download_url: Optional[str] = None            # MinIO 下载链接
    ui_components: List[Dict[str, Any]] = field(default_factory=list)
    sse_events: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "db_id": self.db_id,
            "download_url": self.download_url,
            "ui_components": self.ui_components,
            "sse_events": self.sse_events,
        }
