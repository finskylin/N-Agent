"""
Knowledge Source Registry — 知识源注册与读取接口

设计原则:
- agent_core 只定义 Protocol 接口和注册机制，不依赖任何外部存储（MinIO、MySQL、Redis）
- 具体 Reader 实现（LocalFileReader、MinioFileReader）在 app 层实现并注册
- 向量存储预留接口，暂不实现

使用方式:
```python
# app 层注册 (app/main.py 或 app/services/knowledge_file_service.py)
from agent_core.knowledge.source_registry import get_registry
from app.services.knowledge_readers import LocalFileReader, MinioFileReader

registry = get_registry()
registry.register("local", LocalFileReader(base_dir="/app/knowledge"))
registry.register("minio", MinioFileReader(bucket="knowledge"))

# agent_core 内部读取
from agent_core.knowledge.source_registry import get_registry

registry = get_registry()
content = await registry.read("local", "path/to/file.md")
sources = registry.list_sources()
```
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from loguru import logger


@dataclass
class KnowledgeSource:
    """知识源描述符"""
    name: str                          # 知识源名称，如 "local"、"minio"
    source_type: str                   # 类型：local | minio | vector | custom
    description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class KnowledgeSourceReader(Protocol):
    """知识源读取器协议（app 层实现）"""

    async def read(self, path: str, **kwargs) -> str:
        """
        读取指定路径的知识内容

        Args:
            path: 资源路径（文件路径、对象键、文档 ID 等）
            **kwargs: 额外参数（如 encoding、max_chars 等）

        Returns:
            文本内容，失败返回空字符串
        """
        ...

    async def list(self, prefix: str = "", **kwargs) -> List[str]:
        """
        列出指定前缀下的所有资源路径

        Args:
            prefix: 路径前缀过滤
            **kwargs: 额外参数

        Returns:
            资源路径列表
        """
        ...

    async def exists(self, path: str) -> bool:
        """检查路径是否存在"""
        ...


class VectorStoreReader(Protocol):
    """向量知识库读取器协议（预留，暂未实现）"""

    async def search(self, query: str, top_k: int = 5, **kwargs) -> List[Dict[str, Any]]:
        """
        语义搜索

        Args:
            query: 查询文本
            top_k: 返回最多 K 条
            **kwargs: 过滤条件（如 namespace、score_threshold）

        Returns:
            [{"content": str, "score": float, "metadata": dict}, ...]
        """
        ...


class KnowledgeSourceRegistry:
    """
    知识源注册表

    线程安全: 注册在启动时进行，运行期只读，无竞争问题。
    """

    def __init__(self):
        self._readers: Dict[str, KnowledgeSourceReader] = {}
        self._sources: Dict[str, KnowledgeSource] = {}
        self._vector_store: Optional[VectorStoreReader] = None

    def register(
        self,
        name: str,
        reader: KnowledgeSourceReader,
        source_type: str = "custom",
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        注册知识源读取器

        Args:
            name: 知识源名称（唯一标识）
            reader: 实现 KnowledgeSourceReader 协议的对象
            source_type: 类型标识（local | minio | vector | custom）
            description: 描述
            metadata: 额外元数据
        """
        self._readers[name] = reader
        self._sources[name] = KnowledgeSource(
            name=name,
            source_type=source_type,
            description=description,
            metadata=metadata or {},
        )
        logger.info(f"[KnowledgeSourceRegistry] Registered source: {name} ({source_type})")

    def unregister(self, name: str) -> None:
        """注销知识源"""
        self._readers.pop(name, None)
        self._sources.pop(name, None)
        logger.info(f"[KnowledgeSourceRegistry] Unregistered source: {name}")

    def register_vector_store(self, store: VectorStoreReader) -> None:
        """注册向量知识库（预留，暂未实现）"""
        self._vector_store = store
        logger.info("[KnowledgeSourceRegistry] Vector store registered")

    async def read(self, source_name: str, path: str, **kwargs) -> str:
        """
        从指定知识源读取内容

        Args:
            source_name: 注册时的名称
            path: 资源路径

        Returns:
            文本内容，来源不存在或读取失败返回空字符串
        """
        reader = self._readers.get(source_name)
        if reader is None:
            logger.warning(
                f"[KnowledgeSourceRegistry] Source not found: {source_name}"
            )
            return ""
        try:
            return await reader.read(path, **kwargs)
        except Exception as e:
            logger.warning(
                f"[KnowledgeSourceRegistry] Read failed ({source_name}/{path}): {e}"
            )
            return ""

    async def list(self, source_name: str, prefix: str = "", **kwargs) -> List[str]:
        """列出指定知识源的资源路径"""
        reader = self._readers.get(source_name)
        if reader is None:
            return []
        try:
            return await reader.list(prefix, **kwargs)
        except Exception as e:
            logger.warning(
                f"[KnowledgeSourceRegistry] List failed ({source_name}): {e}"
            )
            return []

    async def exists(self, source_name: str, path: str) -> bool:
        """检查指定知识源的路径是否存在"""
        reader = self._readers.get(source_name)
        if reader is None:
            return False
        try:
            return await reader.exists(path)
        except Exception as e:
            logger.debug(
                f"[KnowledgeSourceRegistry] Exists check failed ({source_name}/{path}): {e}"
            )
            return False

    async def search_vector(
        self, query: str, top_k: int = 5, **kwargs
    ) -> List[Dict[str, Any]]:
        """语义搜索（需先注册向量知识库）"""
        if self._vector_store is None:
            logger.debug("[KnowledgeSourceRegistry] No vector store registered")
            return []
        try:
            return await self._vector_store.search(query, top_k=top_k, **kwargs)
        except Exception as e:
            logger.warning(f"[KnowledgeSourceRegistry] Vector search failed: {e}")
            return []

    def list_sources(self) -> List[KnowledgeSource]:
        """列出所有已注册知识源"""
        return list(self._sources.values())

    def has_source(self, name: str) -> bool:
        """检查知识源是否已注册"""
        return name in self._readers

    def has_vector_store(self) -> bool:
        """检查向量知识库是否已注册"""
        return self._vector_store is not None


# ── 全局单例 ──

_global_registry: Optional[KnowledgeSourceRegistry] = None


def get_registry() -> KnowledgeSourceRegistry:
    """获取全局知识源注册表（懒初始化）"""
    global _global_registry
    if _global_registry is None:
        _global_registry = KnowledgeSourceRegistry()
    return _global_registry


def reset_registry() -> None:
    """重置全局注册表（仅测试用）"""
    global _global_registry
    _global_registry = None
