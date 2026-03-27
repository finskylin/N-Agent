"""
GraphRetriever — BFS 子图查询 + 格式化为 prompt 注入文本

用法:
    graph_retriever = GraphRetriever(graph_store, max_hops=2, max_edges=15)
    text = await graph_retriever.subgraph_for_prompt(unit_ids, user_id, instance_id, query_tags=tags)
    # text 追加到 KnowledgeRetriever.retrieve_for_prompt() 结果末尾
"""
from __future__ import annotations

import time
from typing import List, Optional

from loguru import logger


class GraphRetriever:
    """知识图谱检索器 — 从召回的 unit_ids 出发做 BFS 子图，格式化为 prompt 文本"""

    def __init__(
        self,
        graph_store,
        max_hops: int = 2,
        max_edges: int = 15,
    ):
        self._gs = graph_store
        self._max_hops = max_hops
        self._max_edges = max_edges

    async def subgraph_for_prompt(
        self,
        unit_ids: List[str],
        user_id: int,
        instance_id: str,
        max_hops: Optional[int] = None,
        max_edges: Optional[int] = None,
        query_tags: Optional[List[str]] = None,
    ) -> str:
        """
        从召回的 knowledge_unit IDs 出发，BFS 遍历关联子图，
        格式化为可注入 prompt 的文本段落。

        当 unit_ids 找不到种子节点时，用 query_tags 关键词直接按节点名称匹配兜底。
        命中节点后更新其 access_count。

        Returns:
            字符串，如果图谱为空则返回 ""
        """
        if not unit_ids and not query_tags:
            return ""

        hops = max_hops if max_hops is not None else self._max_hops
        edges_limit = max_edges if max_edges is not None else self._max_edges

        try:
            seed_ids = []

            # 1. 优先用 unit_ids 找种子节点
            if unit_ids:
                nodes = await self._gs.get_nodes_by_unit_ids(user_id, instance_id, unit_ids)
                seed_ids = [n["node_id"] for n in nodes]

            # 2. 兜底：用 query_tags 按节点名称关键词匹配
            if not seed_ids and query_tags:
                for tag in query_tags[:5]:  # 最多取前5个tag
                    tag = tag.strip()
                    if len(tag) < 2:
                        continue
                    node_id = await self._gs.get_node_id(user_id, instance_id, tag)
                    if node_id:
                        seed_ids.append(node_id)
                    else:
                        # 模糊匹配：扫描节点名称含此关键词的节点
                        fuzzy_nodes = await self._gs.list_nodes(
                            user_id, instance_id, filter_kw=tag, limit=3
                        )
                        seed_ids.extend(n["node_id"] for n in fuzzy_nodes)
                    if seed_ids:
                        break  # 找到种子节点即停止

            if not seed_ids:
                return ""

            # 3. BFS 子图查询
            edges = await self._gs.bfs_subgraph(
                user_id=user_id,
                instance_id=instance_id,
                seed_node_ids=seed_ids,
                max_hops=hops,
                max_edges=edges_limit,
            )
            if not edges:
                return ""

            logger.debug(
                f"[GraphRetriever] subgraph_nodes={len(seed_ids)} "
                f"edges={len(edges)} user={user_id}"
            )

            # 4. 更新种子节点的 access_count
            await self._gs.bump_access_count(user_id, instance_id, seed_ids)

            # 5. 格式化为文本
            return self._format_edges(edges)

        except Exception as e:
            logger.warning(f"[GraphRetriever] subgraph_for_prompt failed: {e}")
            return ""

    @staticmethod
    def _format_edges(edges: List[dict]) -> str:
        """
        将边列表格式化为 prompt 文本。

        格式示例:
        [知识关联图谱]
        茅台 --[PE历史分位]--> 70%分位以上
        PE历史分位>70% --[操作建议]--> 观望
        茅台 --[所属行业]--> 白酒板块
        """
        lines = ["[知识关联图谱]"]
        seen = set()
        for e in edges:
            src = e.get("source_name", "")
            rel = e.get("relation", "")
            tgt = e.get("target_name", "")
            if not src or not rel or not tgt:
                continue
            key = (src, rel, tgt)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{src} --[{rel}]--> {tgt}")
        if len(lines) <= 1:
            return ""
        return "\n".join(lines)
