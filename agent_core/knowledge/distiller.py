"""
Knowledge Distiller — 知识蒸馏器

从 Episode 执行轨迹中提取可复用的结构化知识。
含冲突检测: 新知识与旧知识矛盾时，旧知识标记 superseded_by。
"""
import json
import time
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from loguru import logger

from .models import KnowledgeUnit, Episode


def _load_prompt(name: str, **kwargs) -> str:
    """加载提示词模板"""
    try:
        from agent_core.prompts.loader import load_prompt
        return load_prompt(name, **kwargs)
    except Exception:
        try:
            p = Path(__file__).parent.parent.parent / "app" / "prompts" / f"{name}.md"
            if p.exists():
                text = p.read_text(encoding="utf-8")
                if kwargs:
                    try:
                        return text.format(**kwargs)
                    except KeyError:
                        return text
                return text
        except Exception:
            pass
    return ""


class KnowledgeDistiller:
    """知识蒸馏器 — 从 Episode 中提取结构化知识"""

    def __init__(self, store, config: dict, llm_call=None, graph_distiller=None):
        """
        Args:
            store: KnowledgeStore 实例
            config: 完整的 knowledge_engine.json 配置
            llm_call: async LLM 调用函数 (prompt: str) -> str
            graph_distiller: GraphDistiller 实例（可选，None 时跳过图谱写入）
        """
        self._store = store
        self._config = config.get("distiller", {})
        self._llm_call = llm_call
        self._graph_distiller = graph_distiller

    async def distill(
        self, episode: Episode, user_id: int, instance_id: str,
        temporal_manager=None,
    ) -> List[KnowledgeUnit]:
        """
        蒸馏: Episode → KnowledgeUnit[]
        """
        if not self._config.get("enabled", True):
            return []

        min_skills = self._config.get("min_skills_for_distill", 1)
        if len(episode.skill_executions) < min_skills:
            return []

        if not self._llm_call:
            logger.debug("[Distiller] No LLM call configured, skipping")
            return []

        # 构建提示词
        episode_summary = f"Query: {episode.query}\nSuccess: {episode.success}"
        skill_results = "\n".join(
            f"- {se.skill_name}: {'成功' if se.success else '失败'}, "
            f"耗时 {se.duration_ms:.0f}ms, 结果: {se.result_summary}"
            for se in episode.skill_executions
        )

        max_per_episode = self._config.get("max_knowledge_per_episode", 5)
        max_text_chars = self._config.get("max_knowledge_text_chars", 100)

        # 对话上下文（用于提取 user_cognition 思维链）
        conversation_context = ""
        if getattr(episode, "conversation_context", ""):
            conversation_context = episode.conversation_context[:1000]
        elif getattr(episode, "assistant_response", ""):
            conversation_context = f"助手回复摘要: {episode.assistant_response[:500]}"

        prompt = _load_prompt(
            "knowledge_distill",
            episode_summary=episode_summary,
            skill_results=skill_results,
            max_knowledge_per_episode=max_per_episode,
            max_knowledge_text_chars=max_text_chars,
            conversation_context=conversation_context or "（无对话上下文）",
        )

        if not prompt:
            return []

        try:
            timeout = self._config.get("llm_timeout_seconds", 15)
            import asyncio
            response = await asyncio.wait_for(
                self._llm_call(prompt),
                timeout=timeout,
            )

            # 解析 JSON 输出（同时提取 triples）
            units, units_with_triples = self._parse_llm_output(
                response, episode, max_per_episode, max_text_chars
            )

            # 冲突检测 + 版本更新
            if units and self._config.get("conflict_detection", True) and temporal_manager:
                await self._check_and_handle_conflicts(
                    units, user_id, instance_id, temporal_manager,
                )

            # 保存非冲突的新知识
            for unit in units:
                if not unit.supersedes:  # 未被冲突处理过的，直接保存
                    await self._store.save_knowledge(unit, user_id, instance_id)

            # 写入知识图谱（opt-in，graph_distiller=None 时跳过）
            if self._graph_distiller and units_with_triples:
                try:
                    await self._graph_distiller.extract_and_save(
                        units_with_triples, user_id, instance_id
                    )
                except Exception as e:
                    logger.warning(f"[Distiller] Graph distill failed: {e}")

            logger.info(f"[Distiller] Distilled {len(units)} knowledge units from episode {episode.episode_id}")
            return units

        except asyncio.TimeoutError:
            logger.warning(
                f"[Distiller] Distill timed out after {timeout}s (llm_timeout_seconds={timeout})"
            )
            return []
        except Exception as e:
            logger.warning(f"[Distiller] Distill failed: {type(e).__name__}: {e}", exc_info=True)
            return []

    async def _check_and_handle_conflicts(
        self, new_units: List[KnowledgeUnit],
        user_id: int, instance_id: str,
        temporal_manager,
    ):
        """冲突检测 + 版本更新"""
        for new_unit in new_units:
            existing = await self._store.retrieve(
                user_id, instance_id,
                new_unit.tags, category=new_unit.category, top_k=5,
            )
            for old in existing:
                if old.valid_until is not None:
                    continue
                if self._is_conflicting(old, new_unit):
                    await temporal_manager.update_knowledge(
                        old.unit_id, new_unit,
                        reason=f"新 Episode {new_unit.source_episode_id} 提供了更新的认知",
                        user_id=user_id, instance_id=instance_id,
                    )
                    break

    @staticmethod
    def _is_conflicting(old: KnowledgeUnit, new: KnowledgeUnit) -> bool:
        """语义冲突判断: 同 tags 且文本 Jaccard < 0.3"""
        old_tags = set(old.tags)
        new_tags = set(new.tags)
        if not old_tags or not new_tags:
            return False
        tag_jaccard = len(old_tags & new_tags) / max(len(old_tags | new_tags), 1)
        if tag_jaccard < 0.5:
            return False

        old_words = set(old.text)
        new_words = set(new.text)
        text_jaccard = len(old_words & new_words) / max(len(old_words | new_words), 1)
        return text_jaccard < 0.3

    def _parse_llm_output(
        self, response: str, episode: Episode,
        max_count: int, max_chars: int,
    ):
        """
        解析 LLM 输出的 JSON 数组。

        Returns:
            (units: List[KnowledgeUnit], units_with_triples: List[dict])
            units_with_triples 格式: [{"unit_id": str, "triples": [...]}]
        """
        try:
            # 提取 JSON
            text = response.strip()
            if "```" in text:
                start = text.find("[")
                end = text.rfind("]") + 1
                if start >= 0 and end > start:
                    text = text[start:end]

            items = json.loads(text)
            if not isinstance(items, list):
                return [], []

            units = []
            units_with_triples = []
            now = time.time()
            for item in items[:max_count]:
                if not isinstance(item, dict):
                    continue
                text_val = str(item.get("text", ""))[:max_chars]
                if not text_val.strip():
                    continue
                # user_cognition: 将 trigger 附加到文本末尾以便召回时展示
                category = item.get("category", "domain_fact")
                trigger = item.get("trigger", "")
                if category == "user_cognition" and trigger:
                    text_val = f"{text_val} [适用: {trigger}]"
                    text_val = text_val[:max_chars]

                unit_id = str(uuid4())
                unit = KnowledgeUnit(
                    unit_id=unit_id,
                    category=category,
                    text=text_val,
                    tags=item.get("tags", []),
                    utility=min(1.0, max(0.0, float(item.get("utility", 0.5)))),
                    confidence=min(1.0, max(0.0, float(item.get("confidence", 0.5)))),
                    ingestion_time=now,
                    valid_from=now,
                    source_episode_id=episode.episode_id,
                    created_at=now,
                    last_accessed=now,
                )
                units.append(unit)

                # 提取 triples（向后兼容：缺失时不报错）
                triples = item.get("triples")
                if triples and isinstance(triples, list):
                    units_with_triples.append({"unit_id": unit_id, "triples": triples})

            return units, units_with_triples
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"[Distiller] Parse LLM output failed: {e}")
            return [], []
