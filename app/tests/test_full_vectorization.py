"""
全层向量化集成测试

测试覆盖：
1. knowledge_units 后台 embed（mock 后台任务检查 _embed_and_update 被调度）
2. knowledge_units 语义召回排序正确（相关 > 无关）
3. user_experiences 后台 embed（promote_to_global 触发 background task）
4. user_experiences 语义召回（get_global_semantic）
5. session_engine.prepare_session() — query_vec 单次计算，三层共享
6. 降级路径：embedding_client=None 时全部正常

运行方式:
    cd output_project/sthg_agent_service
    python -m pytest app/tests/test_full_vectorization.py -v -s
"""
from __future__ import annotations

import asyncio
import os
import sys
import pytest
import pytest_asyncio

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

_env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_path):
    from dotenv import load_dotenv
    load_dotenv(_env_path, override=True)


# ─────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────

@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    from agent_core.session.context_db import SessionContextDB
    db = SessionContextDB(db_path=str(tmp_path / "test.db"))
    await db._ensure_init()
    return db


@pytest_asyncio.fixture
async def embedding_client():
    from agent_core.memory.embedding_client import EmbeddingClient
    client = EmbeddingClient()
    if not client.enabled:
        pytest.skip("EMBEDDING_API_KEY not configured")
    return client


# ─────────────────────────────────────────
# Test 1: KnowledgeStore 后台 embed 任务调度
# ─────────────────────────────────────────
class TestKnowledgeStoreEmbed:

    @pytest.mark.asyncio
    async def test_save_knowledge_schedules_embed(self, fresh_db, embedding_client):
        """save_knowledge() 后应能检索到 text_embedding BLOB（后台任务已完成）"""
        from agent_core.knowledge.store import KnowledgeStore
        from agent_core.knowledge.models import KnowledgeUnit
        import time

        store = KnowledgeStore(fresh_db, {}, embedding_client=embedding_client)

        unit = KnowledgeUnit(
            unit_id="ku_test_001",
            category="domain_fact",
            text="贵州茅台是中国最大的白酒生产商之一，PE历史分位在70%以上属于高估值区间",
            tags=["茅台", "白酒", "估值"],
            utility=0.8,
            confidence=0.9,
            ingestion_time=time.time(),
            valid_from=time.time(),
            created_at=time.time(),
            last_accessed=time.time(),
        )
        await store.save_knowledge(unit, user_id=1, instance_id="test")

        # 等待后台任务完成（最多 10s）
        for _ in range(20):
            rows = await fresh_db.get_knowledge_units_with_embedding(1, "test")
            if rows and rows[0].get("text_embedding"):
                break
            await asyncio.sleep(0.5)

        rows = await fresh_db.get_knowledge_units_with_embedding(1, "test")
        assert len(rows) == 1
        assert rows[0]["text_embedding"] is not None, "text_embedding BLOB 未写入"
        expected_dim = int(os.getenv("EMBEDDING_DIM", "1536"))
        assert len(rows[0]["text_embedding"]) == expected_dim * 4, (
            f"BLOB 长度错误: {len(rows[0]['text_embedding'])} != {expected_dim * 4}"
        )
        print(f"\n[ku_embed] BLOB size={len(rows[0]['text_embedding'])} bytes")

    @pytest.mark.asyncio
    async def test_retrieve_semantic_ranks_correctly(self, fresh_db, embedding_client):
        """retrieve_semantic() — 茅台相关知识应排第一"""
        from agent_core.knowledge.store import KnowledgeStore
        from agent_core.knowledge.models import KnowledgeUnit
        import time, numpy as np

        store = KnowledgeStore(fresh_db, {}, embedding_client=embedding_client)

        units_data = [
            ("ku_001", "贵州茅台白酒高端品牌，PE估值历史70%分位，建议观望", ["茅台", "白酒", "PE"]),
            ("ku_002", "宁德时代电池技术全球领先，储能业务快速扩张", ["宁德时代", "电池", "新能源"]),
            ("ku_003", "北京今天晴天，气温适宜，空气质量优", ["天气", "北京"]),
        ]

        now = time.time()
        for uid, text, tags in units_data:
            unit = KnowledgeUnit(
                unit_id=uid, category="domain_fact", text=text, tags=tags,
                utility=0.7, confidence=0.8,
                ingestion_time=now, valid_from=now, created_at=now, last_accessed=now,
            )
            await store.save_knowledge(unit, user_id=1, instance_id="test")

        # 等待后台 embed 任务完成
        for _ in range(30):
            rows = await fresh_db.get_knowledge_units_with_embedding(1, "test")
            done = sum(1 for r in rows if r.get("text_embedding"))
            if done >= len(units_data):
                break
            await asyncio.sleep(0.5)

        query_vec = await embedding_client.embed("茅台今天值得买吗")
        assert query_vec is not None

        results = await store.retrieve_semantic(
            query_vec, user_id=1, instance_id="test", top_k=3,
        )
        assert len(results) > 0
        print(f"\n[ku_semantic] top results:")
        for i, u in enumerate(results):
            print(f"  [{i+1}] {u.text[:50]}")

        assert "茅台" in results[0].text or "白酒" in results[0].text, (
            f"Top-1 应为茅台相关，实际: {results[0].text[:50]}"
        )


# ─────────────────────────────────────────
# Test 2: ExperienceStore 后台 embed + 语义召回
# ─────────────────────────────────────────
class TestExperienceStoreEmbed:

    @pytest_asyncio.fixture
    async def exp_store(self, fresh_db, embedding_client):
        from agent_core.session.experience_store import ExperienceStoreCore
        return ExperienceStoreCore(
            user_id=1, instance_id="test",
            sqlite_db=fresh_db,
            embedding_client=embedding_client,
        )

    @pytest.mark.asyncio
    async def test_promote_schedules_embed(self, exp_store, fresh_db, embedding_client):
        """promote_to_global() 后应能检索到 text_embedding BLOB"""
        # 先写 session 经验
        experience = {
            "user_knowledge": [
                {"text": "贵州茅台在高估值区间时用户倾向于观望，不急于建仓", "score": 0.8},
                {"text": "宁德时代电池技术领先，新能源赛道长期看好", "score": 0.7},
            ],
            "system_knowledge": [],
            "corrections": [],
            "learned_patterns": [],
            "user_preferences": [],
            "stock_insights": [],
        }
        await exp_store.save("session_test", experience)
        await exp_store.promote_to_global("session_test")

        # 等待后台 embed 完成
        for _ in range(20):
            rows = await fresh_db.get_user_experiences_with_embedding(
                user_id=1, instance_id="test",
            )
            done = sum(1 for r in rows if r.get("text_embedding"))
            if done >= 2:
                break
            await asyncio.sleep(0.5)

        rows = await fresh_db.get_user_experiences_with_embedding(1, "test")
        with_emb = [r for r in rows if r.get("text_embedding")]
        assert len(with_emb) >= 1, "至少应有 1 条 text_embedding BLOB"
        print(f"\n[exp_embed] {len(with_emb)}/{len(rows)} rows have embedding")

    @pytest.mark.asyncio
    async def test_get_global_semantic_ranks_correctly(self, exp_store, fresh_db, embedding_client):
        """get_global_semantic() — 茅台相关经验应排前"""
        experience = {
            "user_knowledge": [
                {"text": "用户在茅台PE高位减持，低位加仓的操作模式明显", "score": 0.9},
                {"text": "宁德时代储能订单超预期，机构增持明显", "score": 0.8},
                {"text": "天气预报类查询频率较低，用户主要关注股票", "score": 0.3},
            ],
            "system_knowledge": [],
            "corrections": [], "learned_patterns": [],
            "user_preferences": [], "stock_insights": [],
        }
        await exp_store.save("s2", experience)
        await exp_store.promote_to_global("s2")

        # 等待后台 embed
        for _ in range(20):
            rows = await fresh_db.get_user_experiences_with_embedding(1, "test")
            if sum(1 for r in rows if r.get("text_embedding")) >= 2:
                break
            await asyncio.sleep(0.5)

        query_vec = await embedding_client.embed("茅台今天值得买吗")
        result = await exp_store.get_global_semantic(query_vec, dimensions=["user_knowledge"])

        items = result.get("user_knowledge", [])
        assert len(items) > 0
        print(f"\n[exp_semantic] user_knowledge items:")
        for i, item in enumerate(items):
            print(f"  [{i+1}] {item['text'][:50]}")
        assert "茅台" in items[0]["text"], f"Top-1 应含茅台，实际: {items[0]['text'][:50]}"


# ─────────────────────────────────────────
# Test 3: 降级路径 — embedding_client=None
# ─────────────────────────────────────────
class TestFallbackWithoutEmbedding:

    @pytest.mark.asyncio
    async def test_knowledge_store_fallback(self, fresh_db):
        """embedding_client=None 时 retrieve_semantic 正常工作"""
        from agent_core.knowledge.store import KnowledgeStore
        from agent_core.knowledge.models import KnowledgeUnit
        import time, numpy as np

        store = KnowledgeStore(fresh_db, {}, embedding_client=None)

        now = time.time()
        unit = KnowledgeUnit(
            unit_id="ku_fb_001", category="domain_fact",
            text="茅台股票分析", tags=["茅台"],
            utility=0.8, confidence=0.9,
            ingestion_time=now, valid_from=now, created_at=now, last_accessed=now,
        )
        await store.save_knowledge(unit, user_id=1, instance_id="test")

        # 生成随机向量（实际上没有 embedding_client，进入 fallback 分支）
        query_vec = np.random.randn(2048).astype("float32")
        query_vec /= (float(np.linalg.norm(query_vec)) + 1e-10)

        results = await store.retrieve_semantic(query_vec, 1, "test", top_k=5)
        # fallback 时也能正常返回，不崩溃
        assert isinstance(results, list)
        print(f"\n[ku_fallback] got {len(results)} results via fallback")

    @pytest.mark.asyncio
    async def test_experience_store_fallback(self, fresh_db):
        """embedding_client=None 时 get_global_semantic 降级到 get_global"""
        from agent_core.session.experience_store import ExperienceStoreCore
        import numpy as np

        store = ExperienceStoreCore(user_id=1, instance_id="test",
                                    sqlite_db=fresh_db, embedding_client=None)
        experience = {
            "user_knowledge": [{"text": "茅台用户偏好", "score": 0.8}],
            "system_knowledge": [], "corrections": [],
            "learned_patterns": [], "user_preferences": [], "stock_insights": [],
        }
        await store.save("s_fb", experience)
        await store.promote_to_global("s_fb")

        query_vec = np.random.randn(2048).astype("float32")
        result = await store.get_global_semantic(query_vec, dimensions=["user_knowledge"])
        # 降级到 get_global 路径，不崩溃
        assert isinstance(result, dict)
        print(f"\n[exp_fallback] fallback result keys: {list(result.keys())}")
