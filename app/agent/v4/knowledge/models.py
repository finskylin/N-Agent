"""
知识条目数据模型
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Any, Optional


@dataclass
class KnowledgeEntry:
    """知识条目"""
    entry_id: str                              # 唯一ID
    user_id: str                               # 用户ID
    category: str                              # 分类: fact/insight/data/conclusion

    # 核心内容
    question: str                              # 提炼后的问题
    answer: str                                # 简洁的答案
    key_points: List[str] = field(default_factory=list)  # 关键要点

    # 数据关联
    entities: List[str] = field(default_factory=list)    # 涉及实体（股票代码、公司名等）
    topics: List[str] = field(default_factory=list)      # 主题标签
    data_snapshot: Dict[str, Any] = field(default_factory=dict)  # 关键数据快照

    # 来源追溯
    report_id: str = ""                        # 关联报告ID
    source_urls: List[str] = field(default_factory=list)  # 数据来源URL
    confidence: float = 0.85                   # 置信度

    # 时间信息
    created_at: datetime = field(default_factory=datetime.now)
    valid_until: Optional[datetime] = None     # 有效期（实时数据可能过期）

    def to_dict(self) -> Dict[str, Any]:
        """转为字典"""
        data = asdict(self)
        # 处理 datetime 序列化
        data["created_at"] = self.created_at.isoformat() if self.created_at else None
        data["valid_until"] = self.valid_until.isoformat() if self.valid_until else None
        return data

    def to_text(self) -> str:
        """转为纯文本格式（便于全文检索）"""
        lines = [
            f"# {self.question}",
            "",
            self.answer,
            "",
            "## 关键要点",
            *[f"- {p}" for p in self.key_points],
            "",
            f"实体: {', '.join(self.entities)}",
            f"主题: {', '.join(self.topics)}",
            f"置信度: {self.confidence:.0%}",
            f"创建时间: {self.created_at.isoformat() if self.created_at else 'N/A'}",
        ]
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeEntry":
        """从字典创建"""
        # 处理 datetime 反序列化
        if isinstance(data.get("created_at"), str):
            data["created_at"] = datetime.fromisoformat(data["created_at"])
        if isinstance(data.get("valid_until"), str) and data["valid_until"]:
            data["valid_until"] = datetime.fromisoformat(data["valid_until"])

        return cls(**data)
