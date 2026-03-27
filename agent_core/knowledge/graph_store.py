"""
GraphStore — 知识图谱存储层

操作 knowledge_nodes + knowledge_edges 两张表。
复用 SessionContextDB 的 SQLite 连接，零新依赖。

节点去重策略：同 (instance_id, user_id, name) 的节点只存一个，
重复写入时更新 updated_at 和 description（若有）。
"""
from __future__ import annotations

import time
from typing import List, Optional, Dict, Tuple
from uuid import uuid4

from loguru import logger


class GraphStore:
    """知识图谱 CRUD — 复用 SessionContextDB 实例"""

    def __init__(self, sqlite_db):
        self._db = sqlite_db

    # ──────────────────────────────────────────────────────────
    # 节点
    # ──────────────────────────────────────────────────────────

    async def upsert_node(
        self,
        user_id: int,
        instance_id: str,
        name: str,
        node_type: str = "concept",
        description: str = "",
        source_unit_id: str = "",
    ) -> str:
        """
        upsert 节点（同名节点不重复创建）。
        返回 node_id。
        """
        await self._db._ensure_init()
        now = time.time()

        async with self._db._connect() as db:
            await self._db._setup_conn(db)

            # 查找同名节点
            async with db.execute(
                "SELECT node_id FROM knowledge_nodes "
                "WHERE instance_id=? AND user_id=? AND name=? AND valid_until IS NULL",
                (instance_id, user_id, name),
            ) as cur:
                row = await cur.fetchone()

            if row:
                node_id = row["node_id"]
                await db.execute(
                    "UPDATE knowledge_nodes SET updated_at=?, description=COALESCE(NULLIF(?,''),(description)) "
                    "WHERE instance_id=? AND user_id=? AND node_id=?",
                    (now, description, instance_id, user_id, node_id),
                )
            else:
                node_id = f"kn_{uuid4().hex[:8]}"
                await db.execute(
                    """INSERT INTO knowledge_nodes
                       (node_id, user_id, instance_id, name, node_type, description,
                        source_unit_id, access_count, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,0,?,?)""",
                    (node_id, user_id, instance_id, name, node_type,
                     description or None, source_unit_id or None, now, now),
                )

            await db.commit()

        return node_id

    async def get_node_id(
        self, user_id: int, instance_id: str, name: str
    ) -> Optional[str]:
        """按名称查找 node_id（返回 None 若不存在）"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT node_id FROM knowledge_nodes "
                "WHERE instance_id=? AND user_id=? AND name=? AND valid_until IS NULL",
                (instance_id, user_id, name),
            ) as cur:
                row = await cur.fetchone()
        return row["node_id"] if row else None

    async def get_nodes_by_unit_id(
        self, user_id: int, instance_id: str, unit_id: str
    ) -> List[Dict]:
        """获取来源于某个 knowledge_unit 的所有节点"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT node_id, name, node_type FROM knowledge_nodes "
                "WHERE instance_id=? AND user_id=? AND source_unit_id=? AND valid_until IS NULL",
                (instance_id, user_id, unit_id),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_nodes_by_unit_ids(
        self, user_id: int, instance_id: str, unit_ids: List[str]
    ) -> List[Dict]:
        """批量获取来源节点"""
        if not unit_ids:
            return []
        await self._db._ensure_init()
        placeholders = ",".join("?" * len(unit_ids))
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                f"SELECT node_id, name, node_type, source_unit_id FROM knowledge_nodes "
                f"WHERE instance_id=? AND user_id=? AND source_unit_id IN ({placeholders}) "
                f"AND valid_until IS NULL",
                (instance_id, user_id, *unit_ids),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def bump_access_count(
        self,
        user_id: int,
        instance_id: str,
        node_ids: List[str],
    ) -> None:
        """批量递增节点访问计数"""
        if not node_ids:
            return
        await self._db._ensure_init()
        now = time.time()
        placeholders = ",".join("?" * len(node_ids))
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            await db.execute(
                f"UPDATE knowledge_nodes SET access_count = access_count + 1, updated_at=? "
                f"WHERE instance_id=? AND user_id=? AND node_id IN ({placeholders}) "
                f"AND valid_until IS NULL",
                (now, instance_id, user_id, *node_ids),
            )
            await db.commit()

    async def delete_node(
        self, user_id: int, instance_id: str, node_id: str
    ) -> bool:
        """软删除节点（同时软删除关联边）"""
        now = time.time()
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT node_id FROM knowledge_nodes "
                "WHERE instance_id=? AND user_id=? AND node_id=? AND valid_until IS NULL",
                (instance_id, user_id, node_id),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return False
            await db.execute(
                "UPDATE knowledge_nodes SET valid_until=? "
                "WHERE instance_id=? AND user_id=? AND node_id=?",
                (now, instance_id, user_id, node_id),
            )
            # 级联软删除关联边
            await db.execute(
                "UPDATE knowledge_edges SET valid_until=? "
                "WHERE instance_id=? AND user_id=? "
                "AND (source_node_id=? OR target_node_id=?)",
                (now, instance_id, user_id, node_id, node_id),
            )
            await db.commit()
        return True

    # ──────────────────────────────────────────────────────────
    # 边
    # ──────────────────────────────────────────────────────────

    async def insert_edge(
        self,
        user_id: int,
        instance_id: str,
        source_node_id: str,
        target_node_id: str,
        relation: str,
        relation_type: str = "general",
        weight: float = 1.0,
        condition: str = "",
        source_unit_id: str = "",
        expires_at: float = None,
    ) -> str:
        """
        插入关系边（允许重复，重复时权重累加上限 1.0，同时更新 observed_at）。
        返回 edge_id。
        """
        await self._db._ensure_init()
        now = time.time()

        async with self._db._connect() as db:
            await self._db._setup_conn(db)

            # 查找同一方向同一关系的已有边
            async with db.execute(
                "SELECT edge_id, weight FROM knowledge_edges "
                "WHERE instance_id=? AND user_id=? "
                "AND source_node_id=? AND target_node_id=? AND relation=? "
                "AND valid_until IS NULL",
                (instance_id, user_id, source_node_id, target_node_id, relation),
            ) as cur:
                row = await cur.fetchone()

            if row:
                edge_id = row["edge_id"]
                new_weight = min(1.0, (row["weight"] or 1.0) + 0.1)
                await db.execute(
                    "UPDATE knowledge_edges SET weight=?, observed_at=? "
                    "WHERE instance_id=? AND user_id=? AND edge_id=?",
                    (new_weight, now, instance_id, user_id, edge_id),
                )
            else:
                edge_id = f"ke_{uuid4().hex[:8]}"
                await db.execute(
                    """INSERT INTO knowledge_edges
                       (edge_id, user_id, instance_id, source_node_id, target_node_id,
                        relation, relation_type, weight, condition, source_unit_id,
                        created_at, observed_at, expires_at, edge_status, version)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (edge_id, user_id, instance_id, source_node_id, target_node_id,
                     relation, relation_type, weight,
                     condition or None, source_unit_id or None,
                     now, now, expires_at, "active", 1),
                )

            await db.commit()

        return edge_id

    async def update_edge_weight(
        self,
        user_id: int,
        instance_id: str,
        edge_id: str,
        delta: float,
    ) -> bool:
        """
        调整边权重（delta 可正可负），限制在 [0.1, 1.0] 范围内。
        返回是否找到并更新了边。
        """
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT weight FROM knowledge_edges "
                "WHERE instance_id=? AND user_id=? AND edge_id=? AND valid_until IS NULL",
                (instance_id, user_id, edge_id),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return False
            new_weight = max(0.1, min(1.0, (row["weight"] or 1.0) + delta))
            await db.execute(
                "UPDATE knowledge_edges SET weight=?, observed_at=? "
                "WHERE instance_id=? AND user_id=? AND edge_id=?",
                (new_weight, time.time(), instance_id, user_id, edge_id),
            )
            await db.commit()
        return True

    async def delete_edge(
        self, user_id: int, instance_id: str, edge_id: str
    ) -> bool:
        """软删除边"""
        now = time.time()
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT edge_id FROM knowledge_edges "
                "WHERE instance_id=? AND user_id=? AND edge_id=? AND valid_until IS NULL",
                (instance_id, user_id, edge_id),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return False
            await db.execute(
                "UPDATE knowledge_edges SET valid_until=? "
                "WHERE instance_id=? AND user_id=? AND edge_id=?",
                (now, instance_id, user_id, edge_id),
            )
            await db.commit()
        return True

    # ──────────────────────────────────────────────────────────
    # 图谱查询
    # ──────────────────────────────────────────────────────────

    async def bfs_subgraph(
        self,
        user_id: int,
        instance_id: str,
        seed_node_ids: List[str],
        max_hops: int = 2,
        max_edges: int = 30,
    ) -> List[Dict]:
        """
        BFS 子图查询（SQLite 递归 CTE）。
        从 seed_node_ids 出发，最多遍历 max_hops 跳。
        返回边列表：[{source_name, relation, target_name, relation_type, weight}, ...]
        """
        if not seed_node_ids:
            return []

        await self._db._ensure_init()
        seeds_ph = ",".join("?" * len(seed_node_ids))

        query = f"""
WITH RECURSIVE subgraph(node_id, depth) AS (
    SELECT node_id, 0
    FROM knowledge_nodes
    WHERE instance_id=? AND user_id=?
      AND node_id IN ({seeds_ph})
      AND valid_until IS NULL

    UNION

    SELECT ke.target_node_id, sg.depth + 1
    FROM knowledge_edges ke
    JOIN subgraph sg ON ke.source_node_id = sg.node_id
    WHERE ke.instance_id=? AND ke.user_id=?
      AND ke.valid_until IS NULL
      AND sg.depth < ?
)
SELECT DISTINCT
    kn1.name  AS source_name,
    ke.relation,
    kn2.name  AS target_name,
    ke.relation_type,
    ke.weight,
    ke.edge_id,
    ke.created_at,
    ke.observed_at,
    COALESCE(ke.edge_status, 'active') AS edge_status,
    COALESCE(ke.version, 1) AS version,
    CASE
        WHEN ke.observed_at IS NOT NULL AND (strftime('%s','now') - ke.observed_at) > 2592000 THEN 'outdated'
        WHEN ke.expires_at IS NOT NULL AND ke.expires_at < strftime('%s','now') THEN 'outdated'
        ELSE COALESCE(ke.edge_status, 'active')
    END AS effective_status
FROM subgraph sg
JOIN knowledge_edges ke ON ke.source_node_id = sg.node_id
JOIN knowledge_nodes kn1 ON kn1.node_id = ke.source_node_id
JOIN knowledge_nodes kn2 ON kn2.node_id = ke.target_node_id
WHERE ke.instance_id=? AND ke.user_id=?
  AND ke.valid_until IS NULL
  AND kn1.valid_until IS NULL
  AND kn2.valid_until IS NULL
ORDER BY COALESCE(ke.observed_at, ke.created_at) DESC, ke.weight DESC
LIMIT ?
"""
        params = (
            instance_id, user_id,
            *seed_node_ids,
            instance_id, user_id,
            max_hops,
            instance_id, user_id,
            max_edges,
        )

        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()

        return [dict(r) for r in rows]

    async def get_subgraph_by_name(
        self,
        user_id: int,
        instance_id: str,
        node_name: str,
        max_hops: int = 2,
        max_edges: int = 30,
    ) -> List[Dict]:
        """按节点名称查子图（便捷方法）"""
        node_id = await self.get_node_id(user_id, instance_id, node_name)
        if not node_id:
            return []
        return await self.bfs_subgraph(
            user_id, instance_id, [node_id], max_hops=max_hops, max_edges=max_edges,
        )

    async def list_nodes(
        self,
        user_id: int,
        instance_id: str,
        filter_kw: str = "",
        limit: int = 100,
    ) -> List[Dict]:
        """列出节点（可按关键词过滤名称/描述）"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT node_id, name, node_type, description, access_count, created_at "
                "FROM knowledge_nodes "
                "WHERE instance_id=? AND user_id=? AND valid_until IS NULL "
                "ORDER BY access_count DESC, created_at DESC LIMIT ?",
                (instance_id, user_id, limit),
            ) as cur:
                rows = await cur.fetchall()

        result = []
        for r in rows:
            d = dict(r)
            if filter_kw and filter_kw.lower() not in (d["name"] or "").lower():
                continue
            result.append(d)
        return result
