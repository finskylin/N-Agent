"""
miroflow — Miro 白板流程图创建技能

在 Miro 白板上创建流程图、思维导图、架构图等可视化内容。
数据源: Miro REST API
"""
import os
import json
from typing import Any, Dict, List, Optional

import aiohttp
from loguru import logger

MIRO_API_BASE = "https://api.miro.com/v2"


async def _get_or_create_board(session: aiohttp.ClientSession, token: str, board_id: Optional[str]) -> Optional[str]:
    """获取或创建 Miro 白板"""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    if board_id:
        # 验证白板是否存在
        async with session.get(f"{MIRO_API_BASE}/boards/{board_id}", headers=headers) as resp:
            if resp.status == 200:
                return board_id
            logger.warning(f"[miroflow] Board {board_id} not found, creating new board")

    # 创建新白板
    payload = {"name": "Agent Generated Board", "description": "Created by AI Agent"}
    async with session.post(f"{MIRO_API_BASE}/boards", headers=headers, json=payload,
                            timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status == 201:
            data = await resp.json()
            return data.get("id")
        text = await resp.text()
        logger.error(f"[miroflow] Failed to create board: {resp.status} {text[:200]}")
        return None


async def _create_sticky_note(session: aiohttp.ClientSession, token: str, board_id: str,
                               content: str, x: float, y: float, color: str = "yellow") -> Optional[Dict]:
    """在白板上创建便利贴"""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "data": {"content": content, "shape": "square"},
        "style": {"fillColor": color},
        "position": {"x": x, "y": y},
        "geometry": {"width": 200, "height": 200},
    }
    async with session.post(
        f"{MIRO_API_BASE}/boards/{board_id}/sticky_notes",
        headers=headers, json=payload,
        timeout=aiohttp.ClientTimeout(total=10)
    ) as resp:
        if resp.status == 201:
            return await resp.json()
        return None


async def _create_connector(session: aiohttp.ClientSession, token: str, board_id: str,
                             from_id: str, to_id: str) -> Optional[Dict]:
    """在白板上创建连接线"""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "startItem": {"id": from_id},
        "endItem": {"id": to_id},
        "style": {"strokeColor": "#333333", "endStrokeCap": "arrow"},
    }
    async with session.post(
        f"{MIRO_API_BASE}/boards/{board_id}/connectors",
        headers=headers, json=payload,
        timeout=aiohttp.ClientTimeout(total=10)
    ) as resp:
        if resp.status == 201:
            return await resp.json()
        return None


async def _execute(params: Dict[str, Any]) -> Dict[str, Any]:
    """异步执行 Miro 流程图创建"""
    query = (params.get("query") or "").strip()
    board_id = params.get("board_id", "")
    diagram_type = params.get("diagram_type", "auto")

    if not query:
        return {
            "error": "缺少必需参数 query",
            "for_llm": {"error": "请描述需要创建的图形内容"},
        }

    token = os.environ.get("MIRO_ACCESS_TOKEN", "")
    if not token:
        return {
            "error": "MIRO_ACCESS_TOKEN 未配置",
            "for_llm": {"error": "MIRO_ACCESS_TOKEN 未配置，请设置环境变量"},
        }

    try:
        async with aiohttp.ClientSession() as session:
            # 1. 获取或创建白板
            actual_board_id = await _get_or_create_board(session, token, board_id or None)
            if not actual_board_id:
                return {
                    "error": "无法创建或访问 Miro 白板",
                    "for_llm": {"error": "无法创建 Miro 白板，请检查 MIRO_ACCESS_TOKEN 权限"},
                }

            # 2. 根据 query 创建简单的节点布局
            # 将 query 拆分为步骤（按换行或句号分割）
            import re
            steps = [s.strip() for s in re.split(r'[。\n→->]', query) if s.strip()]
            if not steps:
                steps = [query]

            # 限制最多 10 个节点
            steps = steps[:10]

            created_items = []
            spacing = 250
            colors = ["yellow", "light_yellow", "light_green", "light_blue", "violet", "pink"]

            for i, step in enumerate(steps):
                x = i * spacing
                y = 0
                color = colors[i % len(colors)]
                item = await _create_sticky_note(session, token, actual_board_id, step, x, y, color)
                if item:
                    created_items.append(item)

            # 3. 创建连接线
            connectors_created = 0
            for i in range(len(created_items) - 1):
                from_id = created_items[i].get("id")
                to_id = created_items[i + 1].get("id")
                if from_id and to_id:
                    connector = await _create_connector(session, token, actual_board_id, from_id, to_id)
                    if connector:
                        connectors_created += 1

            board_url = f"https://miro.com/app/board/{actual_board_id}/"

            return {
                "board_id": actual_board_id,
                "board_url": board_url,
                "nodes_created": len(created_items),
                "connectors_created": connectors_created,
                "diagram_type": diagram_type,
                "query": query,
                "for_llm": {
                    "board_url": board_url,
                    "nodes_created": len(created_items),
                    "message": f"已在 Miro 白板创建流程图，包含 {len(created_items)} 个节点，白板链接: {board_url}",
                },
            }

    except Exception as e:
        logger.error(f"[miroflow] Error: {e}")
        return {"error": str(e), "for_llm": {"error": f"Miro 流程图创建失败: {e}"}}


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main(params: Dict[str, Any]) -> Dict[str, Any]:
    """同步入口，供框架调用"""
    import asyncio
    try:
        return asyncio.run(_execute(params))
    except Exception as e:
        return {"error": str(e), "for_llm": {"error": str(e)}}


if __name__ == "__main__":
    import sys
    import json as _json
    if len(sys.argv) > 1:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--query", default="")
        parser.add_argument("--board_id", default="")
        parser.add_argument("--diagram_type", default="auto")
        args = parser.parse_args()
        params = {k: v for k, v in vars(args).items() if v}
    else:
        params = _json.loads(sys.stdin.read())
    result = main(params)
    print(_json.dumps(result, ensure_ascii=False))
