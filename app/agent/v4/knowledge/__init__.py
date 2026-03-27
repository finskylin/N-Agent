"""
知识库模块

提供知识抽取、存储和检索能力：
- KnowledgeExtractor: 从对话中抽取知识条目
- KnowledgeStore: MinIO 存储实现
- KnowledgeEntry: 知识条目数据模型

使用:
    from app.agent.v4.knowledge import (
        KnowledgeExtractor,
        KnowledgeStore,
        KnowledgeEntry,
    )
"""

from .extractor import KnowledgeExtractor
from .store import KnowledgeStore
from .models import KnowledgeEntry

__all__ = [
    "KnowledgeExtractor",
    "KnowledgeStore",
    "KnowledgeEntry",
]
