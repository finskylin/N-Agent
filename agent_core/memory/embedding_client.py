"""
EmbeddingClient — 文本向量化客户端

配置（.env）:
    EMBEDDING_API_URL: embedding 服务地址（默认 https://api.openai.com/v1/embeddings）
    EMBEDDING_API_KEY: API Key（默认读取 OPENAI_API_KEY）
    EMBEDDING_MODEL:   模型名称（默认 text-embedding-3-small，输出 1536 维）
    EMBEDDING_DIM:     向量维度（默认 1536）

支持 OpenAI 兼容接口。
向量统一归一化为单位长度，余弦相似度用点积代替。
"""
import os
from typing import Optional

import numpy as np
import httpx
from loguru import logger


class EmbeddingClient:
    def __init__(self):
        self._url = os.getenv("EMBEDDING_API_URL", "https://api.openai.com/v1/embeddings")
        self._key = os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        self._model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        self._dim = int(os.getenv("EMBEDDING_DIM", "1536"))
        self._timeout = 10.0

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    async def embed(self, text: str) -> Optional[np.ndarray]:
        """
        调用 embedding API，返回归一化向量 shape=(dim,) dtype=float32。
        失败时返回 None（调用方降级到热度排序）。
        """
        if not self._key:
            return None
        if not text or not text.strip():
            return None

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    self._url,
                    headers={
                        "Authorization": f"Bearer {self._key}",
                        "Content-Type": "application/json",
                    },
                    json={"input": text[:8000], "model": self._model},
                )
                resp.raise_for_status()
                data = resp.json()
                vec = np.array(data["data"][0]["embedding"], dtype=np.float32)
                return self._normalize(vec)
        except Exception as e:
            logger.warning(f"[EmbeddingClient] embed failed: {e}")
            return None

    def to_blob(self, vec: np.ndarray) -> bytes:
        """numpy float32 array → bytes（存 SQLite BLOB）"""
        return vec.astype(np.float32).tobytes()

    def from_blob(self, blob: bytes) -> np.ndarray:
        """bytes → numpy float32 array"""
        return np.frombuffer(blob, dtype=np.float32).copy()

    @staticmethod
    def cosine_batch(query_vec: np.ndarray, page_vecs: np.ndarray) -> np.ndarray:
        """
        批量余弦相似度（向量已归一化时等于点积）

        query_vec: shape=(dim,)
        page_vecs: shape=(N, dim)
        returns:   shape=(N,) float32
        """
        if page_vecs.ndim == 1:
            page_vecs = page_vecs.reshape(1, -1)
        # float64 计算避免 float32 大维度精度问题；errstate 抑制 numpy fp 状态位残留警告
        with np.errstate(over='ignore', divide='ignore', invalid='ignore'):
            return (page_vecs.astype(np.float64) @ query_vec.astype(np.float64)).astype(np.float32)

    @staticmethod
    def _normalize(vec: np.ndarray) -> np.ndarray:
        norm = np.linalg.norm(vec)
        if norm < 1e-10:
            return vec
        return (vec / norm).astype(np.float32)
