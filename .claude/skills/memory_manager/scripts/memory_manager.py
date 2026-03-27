"""
memory_manager — 用户三层记忆管理工具

操作：
  list   — 查看 MTM / experiences / knowledge_units（可按关键词过滤）
  add    — 向 experiences 或 knowledge_units 手动添加记录
  edit   — 修改已有记录文本
  delete — 软删除（knowledge 标记 valid_until；MTM/experiences 物理删除）

遵守 Skill 架构约束：
  - 不 import agent_core.*、app.*
  - 仅通过环境变量获取配置，直接操作 SQLite
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid
from typing import Optional

import aiosqlite


# ─── 配置 ────────────────────────────────────────────────────────────────────

def _get_db_path(user_id: int, instance_id: str) -> str:
    template = os.getenv(
        "V4_SQLITE_DB_PATH_TEMPLATE",
        "app/data/sessions/{instance_id}/memory.db",
    )
    return template.replace("{instance_id}", instance_id)


def _get_user_id() -> int:
    return int(os.getenv("V4_DEFAULT_USER_ID", "1"))


def _get_instance_id() -> str:
    return os.getenv(
        "AGENT_INSTANCE_ID",
        f"agent-{socket.gethostname()[:8]}",
    )


# ─── DB 工具 ──────────────────────────────────────────────────────────────────

async def _open_db(db_path: str) -> Optional[aiosqlite.Connection]:
    if not os.path.exists(db_path):
        return None
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    return db


# ─── list 操作 ────────────────────────────────────────────────────────────────

async def _list_mtm(db, user_id: int, instance_id: str, keyword: str) -> dict:
    sql = (
        "SELECT page_id, session_id, summary, topics, entities, "
        "visit_count, heat_score, created_at, last_access_at "
        "FROM mtm_pages "
        "WHERE user_id=? AND instance_id=? "
        "ORDER BY heat_score DESC LIMIT 30"
    )
    cursor = await db.execute(sql, (user_id, instance_id))
    rows = await cursor.fetchall()

    items = []
    for row in rows:
        d = dict(row)
        summary = d.get("summary", "")
        if keyword and keyword.lower() not in summary.lower():
            continue
        topics = d.get("topics", "[]")
        if isinstance(topics, str):
            try:
                topics = json.loads(topics)
            except Exception:
                topics = []
        items.append({
            "id": d["page_id"],
            "session_id": d.get("session_id", ""),
            "summary": summary[:200] + ("..." if len(summary) > 200 else ""),
            "topics": topics,
            "heat": round(d.get("heat_score", 0.0), 3),
            "visits": d.get("visit_count", 0),
            "created_at": _fmt_ts(d.get("created_at", 0)),
            "last_access": _fmt_ts(d.get("last_access_at", 0)),
        })

    return {
        "layer": "mtm",
        "total": len(items),
        "filter": keyword or None,
        "items": items,
    }


async def _list_experiences(db, user_id: int, instance_id: str, keyword: str, dimension: str) -> dict:
    if dimension:
        cursor = await db.execute(
            "SELECT dimension, text, score, source_session, updated_at "
            "FROM user_experiences "
            "WHERE user_id=? AND instance_id=? AND dimension=? "
            "ORDER BY score DESC LIMIT 50",
            (user_id, instance_id, dimension),
        )
    else:
        cursor = await db.execute(
            "SELECT dimension, text, score, source_session, updated_at "
            "FROM user_experiences "
            "WHERE user_id=? AND instance_id=? "
            "ORDER BY score DESC LIMIT 100",
            (user_id, instance_id),
        )
    rows = await cursor.fetchall()

    by_dim: dict = {}
    for row in rows:
        d = dict(row)
        text = d.get("text", "")
        if keyword and keyword.lower() not in text.lower():
            continue
        dim = d.get("dimension", "unknown")
        if dim not in by_dim:
            by_dim[dim] = []
        by_dim[dim].append({
            "text": text,
            "score": round(d.get("score", 0.5), 3),
            "source_session": d.get("source_session", ""),
            "updated_at": _fmt_ts(d.get("updated_at", 0)),
        })

    total = sum(len(v) for v in by_dim.values())
    return {
        "layer": "experiences",
        "total": total,
        "filter": keyword or None,
        "dimension_filter": dimension or None,
        "by_dimension": by_dim,
    }


async def _list_knowledge(db, user_id: int, instance_id: str, keyword: str) -> dict:
    cursor = await db.execute(
        "SELECT unit_id, category, text, tags, utility, confidence, "
        "access_count, hit_count, created_at, last_accessed "
        "FROM knowledge_units "
        "WHERE user_id=? AND instance_id=? AND valid_until IS NULL "
        "ORDER BY utility DESC, last_accessed DESC LIMIT 50",
        (user_id, instance_id),
    )
    rows = await cursor.fetchall()

    by_cat: dict = {}
    for row in rows:
        d = dict(row)
        text = d.get("text", "")
        if keyword and keyword.lower() not in text.lower():
            continue
        cat = d.get("category", "unknown")
        tags = d.get("tags", "[]")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = []
        if cat not in by_cat:
            by_cat[cat] = []
        by_cat[cat].append({
            "id": d["unit_id"],
            "text": text,
            "tags": tags,
            "utility": round(d.get("utility", 0.5), 3),
            "confidence": round(d.get("confidence", 0.5), 3),
            "access_count": d.get("access_count", 0),
            "created_at": _fmt_ts(d.get("created_at", 0)),
            "last_accessed": _fmt_ts(d.get("last_accessed", 0)),
        })

    total = sum(len(v) for v in by_cat.values())
    return {
        "layer": "knowledge",
        "total": total,
        "filter": keyword or None,
        "by_category": by_cat,
    }


# ─── add 操作 ─────────────────────────────────────────────────────────────────

async def _add_experience(db, user_id: int, instance_id: str, dimension: str, text: str) -> dict:
    valid_dims = {
        "user_preferences", "stock_insights", "learned_patterns",
        "corrections", "user_knowledge", "system_knowledge",
    }
    if dimension not in valid_dims:
        dimension = "user_knowledge"

    now = int(time.time())
    await db.execute(
        "INSERT INTO user_experiences "
        "(user_id, instance_id, dimension, text, score, source_session, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(instance_id, user_id, dimension, text) DO UPDATE SET "
        "score = MAX(excluded.score, user_experiences.score), "
        "updated_at = excluded.updated_at",
        (user_id, instance_id, dimension, text, 0.8, "manual", now, now),
    )
    await db.commit()
    return {
        "action": "add",
        "layer": "experiences",
        "dimension": dimension,
        "text": text,
        "status": "ok",
    }


async def _add_knowledge(
    db, user_id: int, instance_id: str,
    text: str, category: str, tags: list,
) -> dict:
    valid_cats = {"skill_insight", "domain_fact", "strategy_rule", "user_cognition"}
    if category not in valid_cats:
        category = "domain_fact"

    now = time.time()
    unit_id = f"ku_manual_{uuid.uuid4().hex[:12]}"
    tags_json = json.dumps(tags or [], ensure_ascii=False)

    await db.execute(
        """INSERT OR REPLACE INTO knowledge_units
           (unit_id, user_id, instance_id, category, text, tags,
            utility, confidence, access_count, hit_count,
            feedback_reinforcements, feedback_decays,
            event_time, ingestion_time, valid_from, valid_until,
            superseded_by, supersedes, update_reason,
            source_episode_id, created_at, last_accessed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0,
                   NULL, ?, ?, NULL, NULL, NULL, 'manual_add',
                   '', ?, ?)""",
        (unit_id, user_id, instance_id, category, text, tags_json,
         0.8, 0.8, now, now, now, now),
    )
    await db.commit()
    return {
        "action": "add",
        "layer": "knowledge",
        "unit_id": unit_id,
        "category": category,
        "text": text,
        "tags": tags or [],
        "status": "ok",
    }


# ─── edit 操作 ────────────────────────────────────────────────────────────────

async def _edit_mtm(db, user_id: int, instance_id: str, page_id: str, new_text: str) -> dict:
    cursor = await db.execute(
        "SELECT page_id FROM mtm_pages WHERE page_id=? AND user_id=? AND instance_id=?",
        (page_id, user_id, instance_id),
    )
    row = await cursor.fetchone()
    if not row:
        return {"status": "error", "message": f"MTM page '{page_id}' not found"}

    await db.execute(
        "UPDATE mtm_pages SET summary=? WHERE page_id=? AND user_id=? AND instance_id=?",
        (new_text, page_id, user_id, instance_id),
    )
    await db.commit()
    return {"action": "edit", "layer": "mtm", "page_id": page_id, "status": "ok"}


async def _edit_experience(
    db, user_id: int, instance_id: str, old_text: str, new_text: str, dimension: str,
) -> dict:
    if dimension:
        cursor = await db.execute(
            "SELECT rowid FROM user_experiences "
            "WHERE user_id=? AND instance_id=? AND dimension=? AND text=?",
            (user_id, instance_id, dimension, old_text),
        )
    else:
        cursor = await db.execute(
            "SELECT rowid, dimension FROM user_experiences "
            "WHERE user_id=? AND instance_id=? AND text=? LIMIT 1",
            (user_id, instance_id, old_text),
        )
    row = await cursor.fetchone()
    if not row:
        return {"status": "error", "message": f"Experience text not found: '{old_text[:50]}'"}

    d = dict(row)
    actual_dim = dimension or d.get("dimension", "")
    now = int(time.time())

    # 因为 (instance_id, user_id, dimension, text) 是主键，需要 delete + insert
    await db.execute(
        "DELETE FROM user_experiences "
        "WHERE user_id=? AND instance_id=? AND dimension=? AND text=?",
        (user_id, instance_id, actual_dim, old_text),
    )
    await db.execute(
        "INSERT INTO user_experiences "
        "(user_id, instance_id, dimension, text, score, source_session, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, instance_id, actual_dim, new_text, 0.8, "manual_edit", now, now),
    )
    await db.commit()
    return {
        "action": "edit",
        "layer": "experiences",
        "dimension": actual_dim,
        "old_text": old_text[:80],
        "new_text": new_text[:80],
        "status": "ok",
    }


async def _edit_knowledge(db, user_id: int, instance_id: str, unit_id: str, new_text: str) -> dict:
    cursor = await db.execute(
        "SELECT unit_id FROM knowledge_units WHERE unit_id=? AND user_id=? AND instance_id=?",
        (unit_id, user_id, instance_id),
    )
    row = await cursor.fetchone()
    if not row:
        return {"status": "error", "message": f"Knowledge unit '{unit_id}' not found"}

    await db.execute(
        "UPDATE knowledge_units SET text=?, update_reason='manual_edit' "
        "WHERE unit_id=? AND user_id=? AND instance_id=?",
        (new_text, unit_id, user_id, instance_id),
    )
    await db.commit()
    return {"action": "edit", "layer": "knowledge", "unit_id": unit_id, "status": "ok"}


# ─── delete 操作 ──────────────────────────────────────────────────────────────

async def _delete_mtm(db, user_id: int, instance_id: str, page_id: str) -> dict:
    cursor = await db.execute(
        "SELECT page_id FROM mtm_pages WHERE page_id=? AND user_id=? AND instance_id=?",
        (page_id, user_id, instance_id),
    )
    row = await cursor.fetchone()
    if not row:
        return {"status": "error", "message": f"MTM page '{page_id}' not found"}

    await db.execute(
        "DELETE FROM mtm_pages WHERE page_id=? AND user_id=? AND instance_id=?",
        (page_id, user_id, instance_id),
    )
    await db.commit()
    return {"action": "delete", "layer": "mtm", "page_id": page_id, "status": "ok"}


async def _delete_experience(
    db, user_id: int, instance_id: str, text: str, dimension: str,
) -> dict:
    if dimension:
        cursor = await db.execute(
            "SELECT rowid FROM user_experiences "
            "WHERE user_id=? AND instance_id=? AND dimension=? AND text=?",
            (user_id, instance_id, dimension, text),
        )
    else:
        cursor = await db.execute(
            "SELECT rowid FROM user_experiences "
            "WHERE user_id=? AND instance_id=? AND text=? LIMIT 1",
            (user_id, instance_id, text),
        )
    row = await cursor.fetchone()
    if not row:
        return {"status": "error", "message": f"Experience not found: '{text[:50]}'"}

    if dimension:
        await db.execute(
            "DELETE FROM user_experiences "
            "WHERE user_id=? AND instance_id=? AND dimension=? AND text=?",
            (user_id, instance_id, dimension, text),
        )
    else:
        await db.execute(
            "DELETE FROM user_experiences "
            "WHERE user_id=? AND instance_id=? AND text=?",
            (user_id, instance_id, text),
        )
    await db.commit()
    return {"action": "delete", "layer": "experiences", "text": text[:80], "status": "ok"}


async def _delete_knowledge(db, user_id: int, instance_id: str, unit_id: str) -> dict:
    cursor = await db.execute(
        "SELECT unit_id FROM knowledge_units "
        "WHERE unit_id=? AND user_id=? AND instance_id=? AND valid_until IS NULL",
        (unit_id, user_id, instance_id),
    )
    row = await cursor.fetchone()
    if not row:
        return {"status": "error", "message": f"Knowledge unit '{unit_id}' not found (or already deleted)"}

    now = time.time()
    await db.execute(
        "UPDATE knowledge_units SET valid_until=?, update_reason='manual_delete' "
        "WHERE unit_id=? AND user_id=? AND instance_id=?",
        (now, unit_id, user_id, instance_id),
    )
    await db.commit()
    return {"action": "delete", "layer": "knowledge", "unit_id": unit_id, "status": "ok (soft-deleted)"}


# ─── graph 操作 ───────────────────────────────────────────────────────────────

async def _graph_query(db, user_id: int, instance_id: str, node_name: str) -> dict:
    """查询某节点的 BFS 子图（最多2跳，30条边）"""
    # 查找节点 ID
    cursor = await db.execute(
        "SELECT node_id FROM knowledge_nodes "
        "WHERE instance_id=? AND user_id=? AND name=? AND valid_until IS NULL LIMIT 1",
        (instance_id, user_id, node_name),
    )
    row = await cursor.fetchone()
    if not row:
        # 尝试模糊匹配
        cursor = await db.execute(
            "SELECT node_id, name FROM knowledge_nodes "
            "WHERE instance_id=? AND user_id=? AND name LIKE ? AND valid_until IS NULL LIMIT 5",
            (instance_id, user_id, f"%{node_name}%"),
        )
        rows = await cursor.fetchall()
        if not rows:
            return {"status": "not_found", "message": f"未找到节点: {node_name}"}
        candidates = [dict(r)["name"] for r in rows]
        return {
            "status": "not_found",
            "message": f"未找到节点 '{node_name}'，相似节点: {candidates}",
        }

    node_id = dict(row)["node_id"]

    # BFS 子图（SQLite 递归 CTE，2跳30条边）
    query = """
WITH RECURSIVE subgraph(node_id, depth) AS (
    SELECT node_id, 0 FROM knowledge_nodes
    WHERE instance_id=? AND user_id=? AND node_id=? AND valid_until IS NULL
    UNION
    SELECT ke.target_node_id, sg.depth + 1
    FROM knowledge_edges ke JOIN subgraph sg ON ke.source_node_id = sg.node_id
    WHERE ke.instance_id=? AND ke.user_id=? AND ke.valid_until IS NULL AND sg.depth < 2
)
SELECT DISTINCT kn1.name AS source_name, ke.relation, kn2.name AS target_name,
       ke.relation_type, ke.weight, ke.edge_id
FROM subgraph sg
JOIN knowledge_edges ke ON ke.source_node_id = sg.node_id
JOIN knowledge_nodes kn1 ON kn1.node_id = ke.source_node_id
JOIN knowledge_nodes kn2 ON kn2.node_id = ke.target_node_id
WHERE ke.instance_id=? AND ke.user_id=? AND ke.valid_until IS NULL
  AND kn1.valid_until IS NULL AND kn2.valid_until IS NULL
ORDER BY ke.weight DESC LIMIT 30
"""
    cursor = await db.execute(
        query,
        (instance_id, user_id, node_id, instance_id, user_id, instance_id, user_id),
    )
    rows = await cursor.fetchall()

    edges = []
    for r in rows:
        d = dict(r)
        edges.append({
            "source": d["source_name"],
            "relation": d["relation"],
            "target": d["target_name"],
            "relation_type": d["relation_type"],
            "weight": round(d.get("weight") or 1.0, 3),
            "edge_id": d["edge_id"],
        })

    return {
        "action": "graph_query",
        "node_name": node_name,
        "edge_count": len(edges),
        "edges": edges,
        "status": "ok",
    }


async def _graph_add(
    db, user_id: int, instance_id: str,
    subject: str, relation: str, obj: str, relation_type: str,
) -> dict:
    """手动添加三元组（节点 upsert + 边插入）"""
    valid_types = {"general", "has_child", "if_then", "belongs_to", "similar_to"}
    if relation_type not in valid_types:
        relation_type = "general"

    now = time.time()

    async def _upsert_node(name: str) -> str:
        cursor = await db.execute(
            "SELECT node_id FROM knowledge_nodes "
            "WHERE instance_id=? AND user_id=? AND name=? AND valid_until IS NULL",
            (instance_id, user_id, name),
        )
        row = await cursor.fetchone()
        if row:
            node_id = dict(row)["node_id"]
            await db.execute(
                "UPDATE knowledge_nodes SET updated_at=? WHERE node_id=?",
                (now, node_id),
            )
            return node_id
        node_id = f"kn_{uuid.uuid4().hex[:8]}"
        await db.execute(
            "INSERT INTO knowledge_nodes "
            "(node_id, user_id, instance_id, name, node_type, description, "
            "source_unit_id, access_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,0,?,?)",
            (node_id, user_id, instance_id, name, "concept", None, None, now, now),
        )
        return node_id

    source_id = await _upsert_node(subject)
    target_id = await _upsert_node(obj)

    # 查找已有边（同方向同关系）
    cursor = await db.execute(
        "SELECT edge_id, weight FROM knowledge_edges "
        "WHERE instance_id=? AND user_id=? AND source_node_id=? AND target_node_id=? "
        "AND relation=? AND valid_until IS NULL",
        (instance_id, user_id, source_id, target_id, relation),
    )
    row = await cursor.fetchone()

    if row:
        d = dict(row)
        edge_id = d["edge_id"]
        new_weight = min(1.0, (d.get("weight") or 1.0) + 0.1)
        await db.execute(
            "UPDATE knowledge_edges SET weight=? WHERE edge_id=?",
            (new_weight, edge_id),
        )
    else:
        edge_id = f"ke_{uuid.uuid4().hex[:8]}"
        await db.execute(
            "INSERT INTO knowledge_edges "
            "(edge_id, user_id, instance_id, source_node_id, target_node_id, "
            "relation, relation_type, weight, condition, source_unit_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (edge_id, user_id, instance_id, source_id, target_id,
             relation, relation_type, 1.0, None, None, now),
        )

    await db.commit()
    return {
        "action": "graph_add",
        "subject": subject,
        "relation": relation,
        "object": obj,
        "relation_type": relation_type,
        "edge_id": edge_id,
        "status": "ok",
    }


async def _graph_delete(db, user_id: int, instance_id: str, item_id: str) -> dict:
    """软删除图谱节点（级联软删除其边）或图谱边"""
    now = time.time()

    # 先尝试节点
    cursor = await db.execute(
        "SELECT node_id FROM knowledge_nodes "
        "WHERE node_id=? AND user_id=? AND instance_id=? AND valid_until IS NULL",
        (item_id, user_id, instance_id),
    )
    row = await cursor.fetchone()
    if row:
        await db.execute(
            "UPDATE knowledge_nodes SET valid_until=? WHERE node_id=? AND user_id=? AND instance_id=?",
            (now, item_id, user_id, instance_id),
        )
        await db.execute(
            "UPDATE knowledge_edges SET valid_until=? "
            "WHERE (source_node_id=? OR target_node_id=?) AND user_id=? AND instance_id=?",
            (now, item_id, item_id, user_id, instance_id),
        )
        await db.commit()
        return {"action": "graph_delete", "type": "node", "id": item_id, "status": "ok (soft-deleted)"}

    # 再尝试边
    cursor = await db.execute(
        "SELECT edge_id FROM knowledge_edges "
        "WHERE edge_id=? AND user_id=? AND instance_id=? AND valid_until IS NULL",
        (item_id, user_id, instance_id),
    )
    row = await cursor.fetchone()
    if row:
        await db.execute(
            "UPDATE knowledge_edges SET valid_until=? WHERE edge_id=? AND user_id=? AND instance_id=?",
            (now, item_id, user_id, instance_id),
        )
        await db.commit()
        return {"action": "graph_delete", "type": "edge", "id": item_id, "status": "ok (soft-deleted)"}

    return {"status": "error", "message": f"未找到节点或边: {item_id}"}


# ─── 格式化工具 ───────────────────────────────────────────────────────────────

def _fmt_ts(ts) -> str:
    if not ts:
        return ""
    try:
        import datetime
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


# ─── 主入口 ───────────────────────────────────────────────────────────────────

async def run(params: dict) -> dict:
    action = params.get("action", "list")
    layer = params.get("layer", "mtm").strip().lower()
    keyword = params.get("filter", "").strip()
    dimension = params.get("dimension", "").strip()
    item_id = params.get("item_id", "").strip()
    text = params.get("text", "").strip()
    category = params.get("category", "domain_fact").strip()
    tags = params.get("tags") or []
    node_name = params.get("node_name", "").strip()
    subject = params.get("subject", "").strip()
    relation = params.get("relation", "").strip()
    obj = params.get("object", "").strip()
    relation_type = params.get("relation_type", "general").strip()
    user_id = int(params.get("user_id") or _get_user_id())
    instance_id = str(params.get("instance_id") or _get_instance_id())

    db_path = _get_db_path(user_id, instance_id)
    db = await _open_db(db_path)

    if db is None:
        return {
            "status": "error",
            "message": f"数据库不存在: {db_path}。Agent 还没有产生任何记忆数据。",
            "user_id": user_id,
            "instance_id": instance_id,
        }

    try:
        if action == "list":
            if layer == "mtm":
                result = await _list_mtm(db, user_id, instance_id, keyword)
            elif layer == "experiences":
                result = await _list_experiences(db, user_id, instance_id, keyword, dimension)
            elif layer == "knowledge":
                result = await _list_knowledge(db, user_id, instance_id, keyword)
            else:
                result = {"status": "error", "message": f"未知 layer: {layer}"}

        elif action == "add":
            if not text:
                return {"status": "error", "message": "add 操作需要提供 text 参数"}
            if layer == "experiences":
                result = await _add_experience(db, user_id, instance_id, dimension or "user_knowledge", text)
            elif layer == "knowledge":
                result = await _add_knowledge(db, user_id, instance_id, text, category, tags)
            else:
                result = {"status": "error", "message": f"add 操作不支持 layer={layer}，请使用 experiences 或 knowledge"}

        elif action == "edit":
            if not text:
                return {"status": "error", "message": "edit 操作需要提供新内容 text 参数"}
            if layer == "mtm":
                if not item_id:
                    return {"status": "error", "message": "edit MTM 需要提供 item_id（page_id）"}
                result = await _edit_mtm(db, user_id, instance_id, item_id, text)
            elif layer == "experiences":
                if not item_id:
                    return {"status": "error", "message": "edit experiences 需要提供 item_id（原始文本）"}
                result = await _edit_experience(db, user_id, instance_id, item_id, text, dimension)
            elif layer == "knowledge":
                if not item_id:
                    return {"status": "error", "message": "edit knowledge 需要提供 item_id（unit_id）"}
                result = await _edit_knowledge(db, user_id, instance_id, item_id, text)
            else:
                result = {"status": "error", "message": f"未知 layer: {layer}"}

        elif action == "delete":
            if layer == "mtm":
                if not item_id:
                    return {"status": "error", "message": "delete MTM 需要提供 item_id（page_id）"}
                result = await _delete_mtm(db, user_id, instance_id, item_id)
            elif layer == "experiences":
                if not item_id:
                    return {"status": "error", "message": "delete experiences 需要提供 item_id（原始文本）"}
                result = await _delete_experience(db, user_id, instance_id, item_id, dimension)
            elif layer == "knowledge":
                if not item_id:
                    return {"status": "error", "message": "delete knowledge 需要提供 item_id（unit_id）"}
                result = await _delete_knowledge(db, user_id, instance_id, item_id)
            else:
                result = {"status": "error", "message": f"未知 layer: {layer}"}

        elif action == "graph_query":
            if not node_name:
                return {"status": "error", "message": "graph_query 需要提供 node_name 参数"}
            result = await _graph_query(db, user_id, instance_id, node_name)

        elif action == "graph_add":
            if not subject or not relation or not obj:
                return {"status": "error", "message": "graph_add 需要提供 subject、relation、object 参数"}
            result = await _graph_add(db, user_id, instance_id, subject, relation, obj, relation_type)

        elif action == "graph_delete":
            if not item_id:
                return {"status": "error", "message": "graph_delete 需要提供 item_id（node_id 或 edge_id）"}
            result = await _graph_delete(db, user_id, instance_id, item_id)

        else:
            result = {"status": "error", "message": f"未知 action: {action}"}

    finally:
        await db.close()

    result["user_id"] = user_id
    result["instance_id"] = instance_id
    return result


if __name__ == "__main__":
    import sys

    raw = sys.argv[1] if len(sys.argv) > 1 else "{}"
    try:
        params = json.loads(raw)
    except json.JSONDecodeError:
        params = {}

    result = asyncio.run(run(params))
    print(json.dumps(result, ensure_ascii=False, indent=2))
