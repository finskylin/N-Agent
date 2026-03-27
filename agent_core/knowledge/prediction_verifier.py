"""
PredictionVerifier — 定时验证到期预测

查询所有到期 pending 预测，调用 LLM 对比真实情况，写回验证结果。
由 PredictionScheduler 定时调用，也支持手动触发。
"""
from __future__ import annotations

import json
import time
from typing import Dict, List

from loguru import logger


class PredictionVerifier:
    """验证到期预测，写回 status / actual_outcome / accuracy"""

    _VERIFY_PROMPT = """\
请验证以下预测的准确性。

预测信息：
- 主体：{subject}
- 预测内容：{prediction_text}
- 预测方向：{direction}
- 时间范围：{timeframe}
- 预测时间：{pred_date}

请基于你的知识，判断这个预测是否已经可以验证，以及验证结果如何。

输出格式（JSON，不要有任何其他文字）：
{{
  "status": "verified_correct" 或 "verified_wrong" 或 "unverifiable",
  "actual_outcome": "实际发生了什么（简短描述，50字以内）",
  "accuracy": 0.0到1.0之间的浮点数（0=完全错误，1=完全正确，0.5=部分正确）,
  "note": "验证说明（可选）"
}}

如果时间还未到或信息不足以判断，返回 status="unverifiable"。
"""

    def __init__(self, prediction_store, llm_call):
        """
        Args:
            prediction_store: PredictionStore 实例
            llm_call: call_llm 函数引用
        """
        self._store = prediction_store
        self._llm_call = llm_call

    async def run_pending_verifications(
        self,
        user_id: int,
        instance_id: str,
        max_batch: int = 20,
    ) -> int:
        """
        查询所有到期 pending 预测，逐条验证，返回验证数量。

        Args:
            max_batch: 单次最多验证条数（避免超时）

        Returns:
            实际验证的条数
        """
        pending = await self._store.get_pending(user_id, instance_id)
        if not pending:
            logger.info(f"[PredictionVerifier] No pending predictions for user={user_id}")
            return 0

        pending = pending[:max_batch]
        logger.info(f"[PredictionVerifier] Verifying {len(pending)} predictions for user={user_id}")

        verified = 0
        for pred in pending:
            try:
                result = await self._verify_one(pred)
                await self._store.update_verification(
                    user_id=user_id,
                    instance_id=instance_id,
                    pred_id=pred["pred_id"],
                    status=result["status"],
                    actual_outcome=result.get("actual_outcome", ""),
                    accuracy=result.get("accuracy"),
                    verification_note=result.get("note", ""),
                )
                verified += 1
                logger.debug(
                    f"[PredictionVerifier] pred_id={pred['pred_id']} "
                    f"status={result['status']} accuracy={result.get('accuracy')}"
                )
            except Exception as e:
                logger.warning(f"[PredictionVerifier] Failed to verify pred_id={pred.get('pred_id')}: {e}")

        logger.info(f"[PredictionVerifier] Completed: {verified}/{len(pending)} verified")
        return verified

    async def _verify_one(self, pred: Dict) -> Dict:
        """验证单条预测，返回 {status, actual_outcome, accuracy, note}"""
        import asyncio
        from datetime import datetime

        pred_date = ""
        created_at = pred.get("created_at", 0)
        if created_at:
            pred_date = datetime.fromtimestamp(created_at).strftime("%Y-%m-%d")

        prompt = self._VERIFY_PROMPT.format(
            subject=pred.get("subject", ""),
            prediction_text=pred.get("prediction_text", ""),
            direction=pred.get("direction") or "unknown",
            timeframe=pred.get("timeframe") or "未知时间范围",
            pred_date=pred_date,
        )

        try:
            result_text = await asyncio.wait_for(
                self._llm_call(
                    prompt=prompt,
                    use_small_fast=True,
                    max_tokens=256,
                    timeout=30.0,
                ),
                timeout=35.0,
            )
        except asyncio.TimeoutError:
            logger.debug(f"[PredictionVerifier] LLM timeout for pred_id={pred.get('pred_id')}")
            return {"status": "unverifiable", "actual_outcome": "", "accuracy": None, "note": "验证超时"}
        except Exception as e:
            logger.debug(f"[PredictionVerifier] LLM error: {e}")
            return {"status": "unverifiable", "actual_outcome": "", "accuracy": None, "note": str(e)[:50]}

        if not result_text:
            return {"status": "unverifiable", "actual_outcome": "", "accuracy": None, "note": "LLM返回为空"}

        # 解析 JSON
        text = result_text.strip()
        if "```" in text:
            import re
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
            if m:
                text = m.group(1).strip()

        try:
            data = json.loads(text)
            return {
                "status": data.get("status", "unverifiable"),
                "actual_outcome": data.get("actual_outcome", ""),
                "accuracy": data.get("accuracy"),
                "note": data.get("note", ""),
            }
        except json.JSONDecodeError:
            pass

        # 尝试找 { ... }
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end > start:
                data = json.loads(text[start:end + 1])
                return {
                    "status": data.get("status", "unverifiable"),
                    "actual_outcome": data.get("actual_outcome", ""),
                    "accuracy": data.get("accuracy"),
                    "note": data.get("note", ""),
                }
        except json.JSONDecodeError:
            pass

        return {"status": "unverifiable", "actual_outcome": "", "accuracy": None, "note": "JSON解析失败"}
