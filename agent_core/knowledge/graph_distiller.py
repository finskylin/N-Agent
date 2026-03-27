"""
GraphDistiller — 从知识蒸馏结果中提取三元组并写入知识图谱

配合 KnowledgeDistiller.distill() 使用：
  distill() 解析 LLM 输出中的 triples 字段，
  再调用 GraphDistiller.extract_and_save() 写入 knowledge_nodes + knowledge_edges。
"""
from __future__ import annotations

from typing import List, Dict

from loguru import logger


class GraphDistiller:
    """从蒸馏结果三元组写入知识图谱"""

    def __init__(self, graph_store):
        """
        Args:
            graph_store: GraphStore 实例
        """
        self._gs = graph_store

    async def extract_and_save(
        self,
        units_with_triples: List[Dict],
        user_id: int,
        instance_id: str,
    ) -> int:
        """
        从带 triples 字段的 unit 列表写入图谱节点和边。

        Args:
            units_with_triples: [{"unit_id": ..., "triples": [...]}]
                triples 格式: [{"subject": str, "relation": str, "object": str,
                               "relation_type": str}]
            user_id: 用户 ID
            instance_id: 部署实例 ID

        Returns:
            写入的边数
        """
        edge_count = 0

        for item in units_with_triples:
            unit_id = item.get("unit_id", "")
            triples = item.get("triples") or []
            if not triples:
                continue

            for triple in triples:
                if not isinstance(triple, dict):
                    continue
                subject = (triple.get("subject") or "").strip()
                relation = (triple.get("relation") or "").strip()
                obj = (triple.get("object") or "").strip()
                relation_type = (triple.get("relation_type") or "general").strip()

                if not subject or not relation or not obj:
                    continue

                try:
                    # upsert 两端节点（同名自动去重）
                    source_node_id = await self._gs.upsert_node(
                        user_id=user_id,
                        instance_id=instance_id,
                        name=subject,
                        source_unit_id=unit_id,
                    )
                    target_node_id = await self._gs.upsert_node(
                        user_id=user_id,
                        instance_id=instance_id,
                        name=obj,
                        source_unit_id=unit_id,
                    )

                    # 插入边（重复时权重累加）
                    await self._gs.insert_edge(
                        user_id=user_id,
                        instance_id=instance_id,
                        source_node_id=source_node_id,
                        target_node_id=target_node_id,
                        relation=relation,
                        relation_type=relation_type,
                        source_unit_id=unit_id,
                    )
                    edge_count += 1

                except Exception as e:
                    logger.warning(
                        f"[GraphDistiller] Failed to save triple "
                        f"({subject!r} -{relation!r}-> {obj!r}): {e}"
                    )

        if edge_count:
            logger.info(
                f"[GraphDistiller] Saved {edge_count} edges for "
                f"user={user_id} instance={instance_id}"
            )
        return edge_count
