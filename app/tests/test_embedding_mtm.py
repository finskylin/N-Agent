"""
MTM 语义向量召回测试

测试覆盖：
1. EmbeddingClient.embed() — 调用智谱 embedding-3 API，验证返回向量维度和归一化
2. EmbeddingClient to_blob/from_blob 往返
3. EmbeddingClient.cosine_batch() — 相似文本得分 > 无关文本
4. MidTermMemory 端到端：promote 存 embedding → recall_semantic 按语义排序
5. 降级路径：embedding_client=None 时 recall() 正常工作

运行方式:
    cd output_project/sthg_agent_service
    python -m pytest app/tests/test_embedding_mtm.py -v -s
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import pytest
import pytest_asyncio

# 让 agent_core 可 import（BASE_DIR = sthg_agent_service/）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 加载 .env（sthg_agent_service/.env）
_env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_path):
    from dotenv import load_dotenv
    load_dotenv(_env_path, override=True)


# ─────────────────────────────────────────
# Test 1: EmbeddingClient — API 连通 + 向量维度
# ─────────────────────────────────────────
class TestEmbeddingClient:
    def test_config_loaded(self):
        """验证环境变量已正确加载"""
        api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")
        assert api_key, "EMBEDDING_API_KEY 未配置，请检查 .env"
        dim = int(os.getenv("EMBEDDING_DIM", "1536"))
        assert dim == 2048, f"期望 EMBEDDING_DIM=2048，实际={dim}"
        print(f"\n[Config] model={os.getenv('EMBEDDING_MODEL')}, dim={dim}")

    @pytest.mark.asyncio
    async def test_embed_returns_vector(self):
        """调用 API，验证返回 numpy 向量且维度正确"""
        import numpy as np
        from agent_core.memory.embedding_client import EmbeddingClient

        client = EmbeddingClient()
        assert client.enabled, "EmbeddingClient 未启用（API Key 未配置）"

        vec = await client.embed("贵州茅台股票分析")
        assert vec is not None, "embed() 返回 None，API 调用失败"

        expected_dim = int(os.getenv("EMBEDDING_DIM", "1536"))
        assert vec.shape == (expected_dim,), f"维度错误: 期望({expected_dim},)，实际{vec.shape}"
        assert vec.dtype == np.float32

        # 验证已归一化（单位向量，范数≈1）
        norm = float(np.linalg.norm(vec))
        assert abs(norm - 1.0) < 1e-5, f"向量未归一化，范数={norm:.6f}"
        print(f"\n[embed] shape={vec.shape}, norm={norm:.6f}, first5={vec[:5]}")

    @pytest.mark.asyncio
    async def test_embed_empty_returns_none(self):
        """空字符串应返回 None"""
        from agent_core.memory.embedding_client import EmbeddingClient
        client = EmbeddingClient()
        result = await client.embed("")
        assert result is None

    def test_blob_roundtrip(self):
        """to_blob / from_blob 往返，数值误差 < 1e-6"""
        import numpy as np
        from agent_core.memory.embedding_client import EmbeddingClient

        client = EmbeddingClient()
        original = np.random.randn(2048).astype(np.float32)
        blob = client.to_blob(original)
        restored = client.from_blob(blob)

        assert restored.shape == original.shape
        assert float(np.max(np.abs(original - restored))) < 1e-6
        print(f"\n[blob] roundtrip OK, blob_size={len(blob)} bytes")

    @pytest.mark.asyncio
    async def test_cosine_similar_gt_dissimilar(self):
        """相似文本的余弦相似度 > 无关文本"""
        import numpy as np
        from agent_core.memory.embedding_client import EmbeddingClient

        client = EmbeddingClient()
        assert client.enabled

        query = "分析茅台股票今天的走势"
        similar = "贵州茅台近期行情分析与投资建议"
        dissimilar = "今天天气怎么样，适合出去玩吗"

        vecs = await asyncio.gather(
            client.embed(query),
            client.embed(similar),
            client.embed(dissimilar),
        )
        query_vec, similar_vec, dissimilar_vec = vecs
        assert all(v is not None for v in vecs), "部分 embed 调用返回 None"

        score_similar = float(np.dot(query_vec, similar_vec))
        score_dissimilar = float(np.dot(query_vec, dissimilar_vec))

        print(f"\n[cosine] similar={score_similar:.4f}, dissimilar={score_dissimilar:.4f}")
        assert score_similar > score_dissimilar, (
            f"语义排序错误: similar={score_similar:.4f} 应 > dissimilar={score_dissimilar:.4f}"
        )

    @pytest.mark.asyncio
    async def test_cosine_batch(self):
        """cosine_batch 批量计算结果与逐一计算一致"""
        import numpy as np
        from agent_core.memory.embedding_client import EmbeddingClient

        client = EmbeddingClient()
        assert client.enabled

        texts = [
            "贵州茅台股票分析",
            "市场整体行情走势",
            "今日天气预报",
        ]
        query = "茅台今天怎么样"

        vecs = await asyncio.gather(*[client.embed(t) for t in texts])
        query_vec = await client.embed(query)

        page_vecs = np.stack(vecs)  # shape=(3, 2048)
        batch_scores = client.cosine_batch(query_vec, page_vecs)

        # 逐一验证
        for i, (text, vec) in enumerate(zip(texts, vecs)):
            single = float(np.dot(query_vec, vec))
            batch = float(batch_scores[i])
            assert abs(single - batch) < 1e-5, f"[{i}] single={single}, batch={batch}"
            print(f"  [{i}] '{text[:15]}' → {batch:.4f}")

        # 茅台相关的分数应最高
        assert batch_scores[0] == max(batch_scores), "茅台相关文本得分应最高"


# ─────────────────────────────────────────
# Test 2: MidTermMemory 端到端语义召回
# ─────────────────────────────────────────
class TestMTMSemanticRecall:

    @pytest_asyncio.fixture
    async def db_and_mtm(self, tmp_path):
        """创建临时 SQLite + MTM 实例"""
        from agent_core.session.context_db import SessionContextDB
        from agent_core.memory.mid_term_memory import MidTermMemory
        from agent_core.memory.embedding_client import EmbeddingClient

        db_path = str(tmp_path / "test_memory.db")
        db = SessionContextDB(db_path=db_path)
        await db._ensure_init()

        client = EmbeddingClient()
        mtm = MidTermMemory(
            sqlite_db=db,
            user_id=1,
            instance_id="test",
            embedding_client=client if client.enabled else None,
        )
        return db, mtm, client

    @pytest.mark.asyncio
    async def test_promote_stores_embedding(self, db_and_mtm):
        """promote() 后 mtm_pages 表中应存有 summary_embedding BLOB"""
        db, mtm, client = db_and_mtm
        if not client.enabled:
            pytest.skip("EmbeddingClient 未启用")

        page_id = await mtm.promote(
            session_id="s1",
            summary="对贵州茅台进行了详细分析，近期PE估值偏高，建议观望",
            topics=["股票分析", "贵州茅台", "估值"],
            entities=["贵州茅台", "PE"],
        )

        pages = await db.get_mtm_pages_with_embedding(user_id=1, instance_id="test")
        assert len(pages) == 1
        p = pages[0]
        assert p["page_id"] == page_id
        assert p["summary_embedding"] is not None, "summary_embedding 未写入"
        assert len(p["summary_embedding"]) == 2048 * 4, (
            f"BLOB 长度错误: {len(p['summary_embedding'])} != {2048*4}"
        )
        print(f"\n[promote] page_id={page_id}, embedding_blob={len(p['summary_embedding'])} bytes")

    @pytest.mark.asyncio
    async def test_recall_semantic_ranks_correctly(self, db_and_mtm):
        """
        存入3条 MTM 页面：
        - 茅台分析
        - 宁德时代分析
        - 天气预报
        查询"茅台今天怎么样"，茅台分析应排第一
        """
        db, mtm, client = db_and_mtm
        if not client.enabled:
            pytest.skip("EmbeddingClient 未启用")

        summaries = [
            ("贵州茅台今日行情分析，白酒板块整体上涨，茅台涨幅1.2%", ["股票分析","茅台","白酒"], ["贵州茅台"]),
            ("宁德时代电池技术突破，新能源汽车产业链受益", ["新能源","电池","宁德时代"], ["宁德时代"]),
            ("北京今日天气晴朗，气温22度，适合户外活动", ["天气","北京"], ["北京"]),
        ]

        for summary, topics, entities in summaries:
            await mtm.promote(session_id="s1", summary=summary, topics=topics, entities=entities)

        import numpy as np
        query_vec = await client.embed("茅台今天怎么样")
        assert query_vec is not None

        pages = await mtm.recall_semantic(query_vec, top_k=3)
        assert len(pages) > 0

        print(f"\n[recall_semantic] top-{len(pages)} results:")
        for i, p in enumerate(pages):
            print(f"  [{i+1}] {p.summary[:40]}...")

        # 第一条应是茅台相关
        assert "茅台" in pages[0].summary or "白酒" in pages[0].summary, (
            f"Top-1 应为茅台相关，实际: {pages[0].summary[:50]}"
        )

    @pytest.mark.asyncio
    async def test_fallback_without_embedding_client(self, tmp_path):
        """embedding_client=None 时，recall() 仍能正常返回（热度排序）"""
        from agent_core.session.context_db import SessionContextDB
        from agent_core.memory.mid_term_memory import MidTermMemory

        db = SessionContextDB(db_path=str(tmp_path / "fallback.db"))
        await db._ensure_init()

        mtm = MidTermMemory(
            sqlite_db=db, user_id=1, instance_id="test",
            embedding_client=None,
        )

        await mtm.promote(
            session_id="s1",
            summary="茅台股票分析",
            topics=["茅台", "股票"],
            entities=["贵州茅台"],
        )
        await mtm.promote(
            session_id="s1",
            summary="宁德时代分析",
            topics=["宁德时代", "新能源"],
            entities=["宁德时代"],
        )

        pages = await mtm.recall(query_topics=["茅台"], top_k=5)
        assert len(pages) > 0
        print(f"\n[fallback] recall() returned {len(pages)} pages (heat-based)")

    @pytest.mark.asyncio
    async def test_memory_retriever_semantic_path(self, db_and_mtm):
        """MemoryRetriever.retrieve() 走语义路径端到端"""
        from agent_core.memory.memory_retriever import MemoryRetriever
        from agent_core.memory.long_term_memory import UserProfileStore

        db, mtm, client = db_and_mtm
        if not client.enabled:
            pytest.skip("EmbeddingClient 未启用")

        # 写入 MTM 数据
        await mtm.promote(
            session_id="s1",
            summary="上次分析了茅台，认为当前PE偏高，短期建议观望",
            topics=["茅台分析", "PE估值"],
            entities=["贵州茅台"],
        )

        profile_store = UserProfileStore(sqlite_db=db, user_id=1, instance_id="test")

        retriever = MemoryRetriever(
            mtm=mtm,
            profile_store=profile_store,
            embedding_client=client,
        )

        mem_ctx = await retriever.retrieve(
            session_id="s1",
            query="茅台现在值得买吗",
        )

        print(f"\n[retriever] mtm_count={mem_ctx.source_stats['mtm_count']}")
        print(f"  summaries: {mem_ctx.mtm_summaries}")
        assert mem_ctx.source_stats["mtm_count"] > 0, "语义召回应返回至少1条记忆"
        assert any("茅台" in s for s in mem_ctx.mtm_summaries), "召回内容应包含茅台相关记忆"
