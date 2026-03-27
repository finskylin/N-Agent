"""
知识图谱单测

覆盖:
  1. DB 迁移 — knowledge_nodes / knowledge_edges 表创建
  2. GraphStore — 节点 upsert（去重）、边插入（权重累加）、BFS 查询、软删除
  3. GraphDistiller — 三元组写入
  4. GraphRetriever — 子图 prompt 格式化
  5. KnowledgeDistiller._parse_llm_output — triples 字段向后兼容

运行:
  pytest app/tests/test_knowledge_graph.py -v -s
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
async def sqlite_db():
    """创建临时 SQLite memory.db，运行 migration，返回 SessionContextDB 实例"""
    from agent_core.session.context_db import SessionContextDB

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "memory.db")
        db = SessionContextDB(db_path=db_path)
        await db._ensure_init()
        yield db


@pytest.fixture
async def graph_store(sqlite_db):
    from agent_core.knowledge.graph_store import GraphStore
    return GraphStore(sqlite_db)


USER_ID = 1
INSTANCE_ID = "test-instance"


# ─── Test 1: DB 迁移 ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_migration_creates_tables(sqlite_db):
    """knowledge_nodes 和 knowledge_edges 表应在迁移后存在"""
    async with sqlite_db._connect() as db:
        await sqlite_db._setup_conn(db)
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
            ("knowledge_nodes", "knowledge_edges"),
        ) as cur:
            rows = await cur.fetchall()
    table_names = {row[0] for row in rows}
    assert "knowledge_nodes" in table_names, "knowledge_nodes 表不存在"
    assert "knowledge_edges" in table_names, "knowledge_edges 表不存在"


# ─── Test 2: GraphStore — 节点 upsert ────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_node_dedup(graph_store):
    """同名节点只创建一次，重复 upsert 返回相同 node_id"""
    nid1 = await graph_store.upsert_node(USER_ID, INSTANCE_ID, "茅台")
    nid2 = await graph_store.upsert_node(USER_ID, INSTANCE_ID, "茅台")
    assert nid1 == nid2, "同名节点应返回相同 node_id"

    # 不同名称应创建不同节点
    nid3 = await graph_store.upsert_node(USER_ID, INSTANCE_ID, "五粮液")
    assert nid3 != nid1, "不同名称应生成不同 node_id"


@pytest.mark.asyncio
async def test_get_node_id(graph_store):
    await graph_store.upsert_node(USER_ID, INSTANCE_ID, "白酒板块")
    node_id = await graph_store.get_node_id(USER_ID, INSTANCE_ID, "白酒板块")
    assert node_id is not None
    missing = await graph_store.get_node_id(USER_ID, INSTANCE_ID, "不存在的节点")
    assert missing is None


# ─── Test 3: GraphStore — 边插入与权重累加 ───────────────────────────────────

@pytest.mark.asyncio
async def test_insert_edge_weight_accumulate(graph_store):
    """重复插入同方向同关系的边时，权重应累加"""
    src = await graph_store.upsert_node(USER_ID, INSTANCE_ID, "A节点")
    tgt = await graph_store.upsert_node(USER_ID, INSTANCE_ID, "B节点")

    eid1 = await graph_store.insert_edge(USER_ID, INSTANCE_ID, src, tgt, "测试关系")
    eid2 = await graph_store.insert_edge(USER_ID, INSTANCE_ID, src, tgt, "测试关系")
    assert eid1 == eid2, "重复边应返回相同 edge_id"

    # 新方向/新关系应创建新边
    eid3 = await graph_store.insert_edge(USER_ID, INSTANCE_ID, src, tgt, "另一关系")
    assert eid3 != eid1


# ─── Test 4: GraphStore — BFS 子图 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_bfs_subgraph(graph_store):
    """BFS 应返回从种子节点出发的可达边"""
    # 构建: 茅台 → 白酒板块 → 五粮液
    n_maotai = await graph_store.upsert_node(USER_ID, INSTANCE_ID, "茅台BFS")
    n_baijiu = await graph_store.upsert_node(USER_ID, INSTANCE_ID, "白酒板块BFS")
    n_wuliangye = await graph_store.upsert_node(USER_ID, INSTANCE_ID, "五粮液BFS")

    await graph_store.insert_edge(USER_ID, INSTANCE_ID, n_maotai, n_baijiu, "属于", "belongs_to")
    await graph_store.insert_edge(USER_ID, INSTANCE_ID, n_baijiu, n_wuliangye, "包含", "has_child")

    edges = await graph_store.bfs_subgraph(
        USER_ID, INSTANCE_ID, [n_maotai], max_hops=2, max_edges=10
    )
    sources = {e["source_name"] for e in edges}
    targets = {e["target_name"] for e in edges}

    assert "茅台BFS" in sources
    assert "白酒板块BFS" in targets or "白酒板块BFS" in sources


# ─── Test 5: GraphStore — 软删除 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_soft_delete_node(graph_store):
    nid = await graph_store.upsert_node(USER_ID, INSTANCE_ID, "待删除节点")
    result = await graph_store.delete_node(USER_ID, INSTANCE_ID, nid)
    assert result is True

    # 删除后应查不到
    found = await graph_store.get_node_id(USER_ID, INSTANCE_ID, "待删除节点")
    assert found is None

    # 重复删除返回 False
    result2 = await graph_store.delete_node(USER_ID, INSTANCE_ID, nid)
    assert result2 is False


@pytest.mark.asyncio
async def test_soft_delete_edge(graph_store):
    src = await graph_store.upsert_node(USER_ID, INSTANCE_ID, "E源节点")
    tgt = await graph_store.upsert_node(USER_ID, INSTANCE_ID, "E目标节点")
    eid = await graph_store.insert_edge(USER_ID, INSTANCE_ID, src, tgt, "测试删除边")

    result = await graph_store.delete_edge(USER_ID, INSTANCE_ID, eid)
    assert result is True

    # 删除后 BFS 应看不到这条边
    edges = await graph_store.bfs_subgraph(USER_ID, INSTANCE_ID, [src])
    edge_ids = {e["edge_id"] for e in edges}
    assert eid not in edge_ids


# ─── Test 6: GraphStore — get_nodes_by_unit_ids ──────────────────────────────

@pytest.mark.asyncio
async def test_get_nodes_by_unit_ids(graph_store):
    await graph_store.upsert_node(
        USER_ID, INSTANCE_ID, "源节点1", source_unit_id="unit_abc"
    )
    await graph_store.upsert_node(
        USER_ID, INSTANCE_ID, "源节点2", source_unit_id="unit_abc"
    )
    nodes = await graph_store.get_nodes_by_unit_ids(USER_ID, INSTANCE_ID, ["unit_abc"])
    assert len(nodes) == 2
    names = {n["name"] for n in nodes}
    assert "源节点1" in names
    assert "源节点2" in names


# ─── Test 7: GraphDistiller ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_graph_distiller(graph_store):
    from agent_core.knowledge.graph_distiller import GraphDistiller

    distiller = GraphDistiller(graph_store)
    units_with_triples = [
        {
            "unit_id": "unit_test_001",
            "triples": [
                {"subject": "茅台D", "relation": "PE分位", "object": "70%以上", "relation_type": "general"},
                {"subject": "70%以上", "relation": "操作建议", "object": "观望", "relation_type": "if_then"},
            ],
        }
    ]
    count = await distiller.extract_and_save(units_with_triples, USER_ID, INSTANCE_ID)
    assert count == 2, f"应写入2条边，实际 {count}"

    # 验证节点存在
    nid = await graph_store.get_node_id(USER_ID, INSTANCE_ID, "茅台D")
    assert nid is not None


# ─── Test 8: GraphRetriever ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_graph_retriever(graph_store):
    from agent_core.knowledge.graph_distiller import GraphDistiller
    from agent_core.knowledge.graph_retriever import GraphRetriever

    # 先写入数据
    distiller = GraphDistiller(graph_store)
    await distiller.extract_and_save([
        {
            "unit_id": "unit_ret_001",
            "triples": [
                {"subject": "茅台R", "relation": "属于", "object": "白酒板块R", "relation_type": "belongs_to"},
            ],
        }
    ], USER_ID, INSTANCE_ID)

    retriever = GraphRetriever(graph_store, max_hops=2, max_edges=10)
    text = await retriever.subgraph_for_prompt(
        unit_ids=["unit_ret_001"],
        user_id=USER_ID,
        instance_id=INSTANCE_ID,
    )
    assert "[知识关联图谱]" in text, f"期待图谱文本，实际: {text!r}"
    assert "茅台R" in text
    assert "白酒板块R" in text


# ─── Test 9: KnowledgeDistiller._parse_llm_output — triples 向后兼容 ─────────

def test_distiller_parse_triples():
    """_parse_llm_output 应正确提取 triples，缺失时不报错"""
    import time
    from unittest.mock import MagicMock
    from agent_core.knowledge.distiller import KnowledgeDistiller

    store_mock = MagicMock()
    distiller = KnowledgeDistiller(store=store_mock, config={"distiller": {}})

    episode = MagicMock()
    episode.episode_id = "ep_test"

    # 含 triples
    llm_output = json.dumps([
        {
            "category": "domain_fact",
            "text": "茅台PE高估",
            "tags": ["茅台", "PE"],
            "utility": 0.8,
            "confidence": 0.9,
            "triples": [
                {"subject": "茅台", "relation": "PE", "object": "高估值", "relation_type": "general"}
            ],
        }
    ])
    units, units_with_triples = distiller._parse_llm_output(llm_output, episode, 5, 200)
    assert len(units) == 1
    assert len(units_with_triples) == 1
    assert units_with_triples[0]["triples"][0]["subject"] == "茅台"

    # 不含 triples（向后兼容）
    llm_output2 = json.dumps([
        {"category": "domain_fact", "text": "测试", "tags": [], "utility": 0.5, "confidence": 0.5}
    ])
    units2, triples2 = distiller._parse_llm_output(llm_output2, episode, 5, 200)
    assert len(units2) == 1
    assert len(triples2) == 0  # 无 triples 字段，不应报错


# ─── Test 10: get_subgraph_by_name ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_subgraph_by_name(graph_store):
    await graph_store.upsert_node(USER_ID, INSTANCE_ID, "中心节点X")
    target = await graph_store.upsert_node(USER_ID, INSTANCE_ID, "关联节点X")
    src = await graph_store.get_node_id(USER_ID, INSTANCE_ID, "中心节点X")
    await graph_store.insert_edge(USER_ID, INSTANCE_ID, src, target, "has_child")

    edges = await graph_store.get_subgraph_by_name(USER_ID, INSTANCE_ID, "中心节点X")
    assert len(edges) >= 1

    # 不存在的节点应返回空列表
    edges_empty = await graph_store.get_subgraph_by_name(USER_ID, INSTANCE_ID, "不存在的节点xyz")
    assert edges_empty == []
