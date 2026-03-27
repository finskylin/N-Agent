"""
Knowledge Source Readers — 知识源读取器具体实现

实现 agent_core.knowledge.source_registry.KnowledgeSourceReader 协议:
- LocalFileReader: 从本地文件系统读取（支持 txt/md/json/csv）
- MinioFileReader: 从 MinIO 对象存储读取

这些实现在 app 层，可依赖 MinIO、配置等外部服务。
agent_core 层只依赖 Protocol 接口，不依赖这些实现。

使用方式（app/main.py 启动时注册）:
    from agent_core.knowledge.source_registry import get_registry
    from app.services.knowledge_readers import LocalFileReader, MinioFileReader

    registry = get_registry()
    registry.register("local", LocalFileReader(base_dir="knowledge"), source_type="local")
    registry.register("minio", MinioFileReader(), source_type="minio")
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, List

from loguru import logger


class LocalFileReader:
    """
    本地文件系统知识源读取器

    支持格式: .txt, .md, .json, .csv, .yaml, .yml
    """

    def __init__(self, base_dir: str = "knowledge", max_chars: int = 100_000):
        self._base_dir = Path(base_dir)
        self._max_chars = max_chars

    async def read(self, path: str, **kwargs) -> str:
        """读取本地文件内容"""
        full_path = self._base_dir / path
        try:
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(
                None, self._read_sync, full_path,
            )
            max_chars = kwargs.get("max_chars", self._max_chars)
            if len(content) > max_chars:
                content = content[:max_chars] + "\n...(truncated)"
            return content
        except FileNotFoundError:
            logger.debug(f"[LocalFileReader] File not found: {full_path}")
            return ""
        except Exception as e:
            logger.warning(f"[LocalFileReader] Read failed ({path}): {e}")
            return ""

    @staticmethod
    def _read_sync(full_path: Path) -> str:
        return full_path.read_text(encoding="utf-8", errors="replace")

    async def list(self, prefix: str = "", **kwargs) -> List[str]:
        """列出本地文件路径（相对于 base_dir）"""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._list_sync, prefix)
        except Exception as e:
            logger.warning(f"[LocalFileReader] List failed: {e}")
            return []

    def _list_sync(self, prefix: str) -> List[str]:
        base = self._base_dir
        if prefix:
            base = base / prefix
        if not base.exists():
            return []
        result = []
        for p in base.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                try:
                    rel = p.relative_to(self._base_dir)
                    result.append(str(rel))
                except ValueError:
                    pass
        return sorted(result)

    async def exists(self, path: str) -> bool:
        """检查文件是否存在"""
        return (self._base_dir / path).is_file()


class MinioFileReader:
    """
    MinIO 对象存储知识源读取器

    依赖 app.config.settings 中的 MinIO 配置。
    """

    def __init__(
        self,
        bucket: str = "",
        endpoint: str = "",
        access_key: str = "",
        secret_key: str = "",
        secure: bool = False,
        max_chars: int = 100_000,
    ):
        self._bucket = bucket
        self._endpoint = endpoint
        self._access_key = access_key
        self._secret_key = secret_key
        self._secure = secure
        self._max_chars = max_chars
        self._client = None

    def _get_client(self):
        """懒初始化 MinIO 客户端"""
        if self._client is not None:
            return self._client
        try:
            from minio import Minio
            from app.config import settings

            endpoint = self._endpoint or getattr(settings, "minio_endpoint", "")
            access_key = self._access_key or getattr(settings, "minio_access_key", "")
            secret_key = self._secret_key or getattr(settings, "minio_secret_key", "")
            secure = self._secure or getattr(settings, "minio_secure", False)
            bucket = self._bucket or getattr(settings, "minio_knowledge_bucket", "knowledge")

            if not endpoint:
                logger.warning("[MinioFileReader] No MinIO endpoint configured")
                return None

            self._client = Minio(
                endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=secure,
            )
            self._bucket = bucket
            return self._client
        except ImportError:
            logger.debug("[MinioFileReader] minio package not installed")
            return None
        except Exception as e:
            logger.warning(f"[MinioFileReader] Client init failed: {e}")
            return None

    async def read(self, path: str, **kwargs) -> str:
        """从 MinIO 读取对象内容"""
        client = self._get_client()
        if client is None:
            return ""
        try:
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(
                None, self._read_sync, client, path,
            )
            max_chars = kwargs.get("max_chars", self._max_chars)
            if len(content) > max_chars:
                content = content[:max_chars] + "\n...(truncated)"
            return content
        except Exception as e:
            logger.warning(f"[MinioFileReader] Read failed ({self._bucket}/{path}): {e}")
            return ""

    def _read_sync(self, client, path: str) -> str:
        response = client.get_object(self._bucket, path)
        try:
            return response.read().decode("utf-8", errors="replace")
        finally:
            response.close()
            response.release_conn()

    async def list(self, prefix: str = "", **kwargs) -> List[str]:
        """列出 MinIO 对象路径"""
        client = self._get_client()
        if client is None:
            return []
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._list_sync, client, prefix,
            )
        except Exception as e:
            logger.warning(f"[MinioFileReader] List failed: {e}")
            return []

    def _list_sync(self, client, prefix: str) -> List[str]:
        objects = client.list_objects(self._bucket, prefix=prefix, recursive=True)
        return [obj.object_name for obj in objects]

    async def exists(self, path: str) -> bool:
        """检查 MinIO 对象是否存在"""
        client = self._get_client()
        if client is None:
            return False
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._exists_sync, client, path,
            )
        except Exception:
            return False

    def _exists_sync(self, client, path: str) -> bool:
        try:
            client.stat_object(self._bucket, path)
            return True
        except Exception:
            return False
