"""
CLISessionStoreCore — 纯 SQLite CLI Session 存储

Core 版本：仅使用 SessionContextDB（SQLite），无 Redis/MySQL 依赖。
上层 Enhanced 版本可继承此类，叠加 Redis + MySQL 层。
"""
import json
from typing import Optional, TYPE_CHECKING
from loguru import logger

if TYPE_CHECKING:
    from .context_db import SessionContextDB


class CLISessionStoreCore:
    """CLI Session ID 存储 — 纯 SQLite 版本"""

    def __init__(
        self,
        user_id: int = 1,
        instance_id: str = "default",
        sqlite_db: Optional["SessionContextDB"] = None,
        # 兼容旧参数（忽略 Redis/TTL）
        ttl: int = 0,
        **kwargs,
    ):
        self._user_id = user_id
        self._instance_id = instance_id
        self._sqlite = sqlite_db

    async def get(self, session_id: str) -> Optional[str]:
        """获取 CLI session ID"""
        if not self._sqlite:
            return None
        try:
            value = await self._sqlite.get_cli_session(
                session_id, self._user_id, self._instance_id,
            )
            if value:
                logger.debug(f"[CLISessionStoreCore] Found cli_session for {session_id}")
            return value
        except Exception as e:
            logger.warning(f"[CLISessionStoreCore] get failed: {e}")
            return None

    async def save(self, session_id: str, cli_session_id: str):
        """保存 CLI session ID"""
        if not self._sqlite:
            return
        try:
            await self._sqlite.save_cli_session(
                session_id, self._user_id, self._instance_id, cli_session_id,
            )
            logger.info(
                f"[CLISessionStoreCore] Saved cli_session={cli_session_id[:20]}... "
                f"for session={session_id}"
            )
        except Exception as e:
            logger.error(f"[CLISessionStoreCore] save failed: {e}")

    async def clear(self, session_id: str):
        """清除 CLI session ID"""
        if not self._sqlite:
            return
        try:
            await self._sqlite.delete_cli_session(
                session_id, self._user_id, self._instance_id,
            )
            logger.info(f"[CLISessionStoreCore] Cleared cli_session for {session_id}")
        except Exception as e:
            logger.warning(f"[CLISessionStoreCore] clear failed: {e}")

    async def exists(self, session_id: str) -> bool:
        """检查是否存在 CLI session ID"""
        if not self._sqlite:
            return False
        try:
            value = await self._sqlite.get_cli_session(
                session_id, self._user_id, self._instance_id,
            )
            return value is not None
        except Exception:
            return False
