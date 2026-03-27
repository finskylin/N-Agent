"""
PredictionExtractor — Stop Hook 中提取预测性判断

从 Agent 回答里调用轻量 LLM 提取带时间性的预测，写入 prediction_store。
完全异步，5s 超时，失败静默忽略，不阻塞主响应流。
"""
from __future__ import annotations

import json
import time
from typing import List, Dict

from loguru import logger


class PredictionExtractor:
    """从 Agent 回答中提取预测性判断并保存"""

    _EXTRACT_PROMPT = """\
分析以下 Agent 回答，提取所有带时间性的预测判断。

用户问题：{user_query}

Agent 回答（节选）：
{assistant_text}

请仅提取明确的、带时间范围的预测性判断（如"下周上涨"、"Q1业绩超预期"、"3个月内回调"等）。
对于不含时间预测的回答，返回空数组。

输出格式（JSON 数组，不要有任何其他文字）：
[
  {{
    "subject": "预测主体（如股票名称、公司、指数等）",
    "prediction_text": "完整预测描述（50字以内）",
    "direction": "up/down/neutral/other",
    "timeframe": "时间范围描述（如'下周'/'Q1'/'3个月内'）",
    "verify_before_days": 整数（几天后可验证，如7/30/90）
  }}
]

若没有预测性判断，返回：[]
"""

    def __init__(self, llm_call, prediction_store, enabled: bool = True):
        """
        Args:
            llm_call: agent_core.agentloop.llm_provider.call_llm 函数引用
            prediction_store: PredictionStore 实例
            enabled: 是否启用提取（可通过配置关闭）
        """
        self._llm_call = llm_call
        self._store = prediction_store
        self._enabled = enabled

    async def extract_and_save(
        self,
        user_id: int,
        instance_id: str,
        session_id: str,
        user_query: str,
        assistant_text: str,
    ) -> int:
        """
        从 assistant_text 中提取预测性判断并保存。
        调用轻量 LLM（5s 超时），失败静默忽略。
        返回写入的预测数量。
        """
        if not self._enabled:
            return 0
        if not assistant_text or len(assistant_text.strip()) < 20:
            return 0

        try:
            predictions = await self._call_llm(user_query, assistant_text)
            if not predictions:
                return 0

            now = time.time()
            saved = 0
            for p in predictions[:5]:  # 最多保存 5 条/轮，避免滥写
                if not isinstance(p, dict):
                    continue
                subject = (p.get("subject") or "").strip()
                pred_text = (p.get("prediction_text") or "").strip()
                if not subject or not pred_text:
                    continue

                direction = (p.get("direction") or "other").strip()
                timeframe = (p.get("timeframe") or "").strip()
                days = int(p.get("verify_before_days") or 7)
                days = max(1, min(days, 365))  # 限制 1~365 天
                verify_before = now + days * 86400

                await self._store.save(
                    user_id=user_id,
                    instance_id=instance_id,
                    session_id=session_id,
                    subject=subject,
                    prediction_text=pred_text,
                    direction=direction,
                    timeframe=timeframe,
                    verify_before=verify_before,
                )
                saved += 1

            if saved:
                logger.info(f"[PredictionExtractor] Saved {saved} predictions for session={session_id}")
            return saved

        except Exception as e:
            logger.debug(f"[PredictionExtractor] extract_and_save failed (non-fatal): {e}")
            return 0

    async def _call_llm(self, user_query: str, assistant_text: str) -> List[Dict]:
        """调用轻量 LLM 提取预测（5s 超时，失败返回空列表）"""
        import asyncio

        prompt = self._EXTRACT_PROMPT.format(
            user_query=(user_query or "")[:200],
            assistant_text=assistant_text[:1500],
        )

        try:
            result = await asyncio.wait_for(
                self._llm_call(
                    prompt=prompt,
                    use_small_fast=True,
                    max_tokens=512,
                    timeout=8.0,
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.debug("[PredictionExtractor] LLM call timed out (5s)")
            return []
        except Exception as e:
            logger.debug(f"[PredictionExtractor] LLM call failed: {e}")
            return []

        if not result:
            return []

        # 解析 JSON
        text = result.strip()
        # 尝试从 ```json 代码块提取
        if "```" in text:
            import re
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
            if m:
                text = m.group(1).strip()

        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

        # 尝试找 [ ... ]
        try:
            start = text.find("[")
            end = text.rfind("]")
            if start != -1 and end > start:
                data = json.loads(text[start:end + 1])
                if isinstance(data, list):
                    return data
        except json.JSONDecodeError:
            pass

        return []
