"""
LLM Helpers — 摘要压缩、经验提取和智能参数提取

职责:
1. summarize_conversation: 使用 LLM 总结对话历史
2. extract_experience: 从单轮对话中提取可复用的经验知识
3. extract_skill_params: 从自然语言查询中智能提取 Skill 所需参数
4. extract_topics_and_entities: 从摘要文本中提取主题和实体（MTM topic_extractor 回调）
5. extract_query_keywords: 轻量级中文关键词提取（热路径同步函数）

这些函数作为 ConversationHistory.maybe_summarize() 和
ExperienceStore.extract_and_save() 的回调函数使用。

所有提示词模板均从 app/prompts/ 目录加载，不硬编码。
"""
import json
import re
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple
from loguru import logger


def _load_prompt(name: str, **kwargs) -> str:
    """
    从 agent_core/prompts/ 加载提示词模板

    优先使用 agent_core.prompts.loader（如可用），
    fallback 直接读 md 文件并做 format。
    """
    try:
        from agent_core.prompts.loader import load_prompt
        return load_prompt(name, **kwargs)
    except Exception:
        try:
            p = Path(__file__).parent.parent.parent.parent / "agent_core" / "prompts" / f"{name}.md"
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


async def summarize_conversation(
    messages: List[Dict[str, str]],
    existing_summary: Optional[str] = None
) -> str:
    """
    使用 Claude 总结对话历史

    Args:
        messages: 需要被总结的旧消息列表
        existing_summary: 已有的总结（增量更新）

    Returns:
        更新后的总结文本
    """
    messages_text = "\n".join(
        f"{'用户' if m.get('role') == 'user' else '助手'}: {m.get('content', '')}"
        for m in messages
    )

    prompt = _load_prompt(
        "v4_summarize_conversation",
        messages_text=messages_text,
        existing_summary=existing_summary or "无",
    )

    result_text = await _call_llm(prompt)
    return result_text or existing_summary or ""


async def extract_experience(
    user_msg: str,
    assistant_msg: str
) -> Dict[str, List[dict]]:
    """
    从单轮对话中提取可复用的经验知识（带价值评分）

    Args:
        user_msg: 用户消息
        assistant_msg: 助手回复

    Returns:
        {
            "user_preferences": [{"text": "...", "score": 0.8}, ...],
            "stock_insights": [{"text": "...", "score": 0.9}, ...],
            "learned_patterns": [...],
            "corrections": [...]
        }
    """
    prompt = _load_prompt(
        "v4_extract_experience",
        user_message=user_msg,
        assistant_message=assistant_msg,
    )

    result_text = await _call_llm(prompt)

    empty_result: Dict[str, List[dict]] = {
        "user_preferences": [],
        "stock_insights": [],
        "learned_patterns": [],
        "corrections": [],
    }

    if not result_text:
        return empty_result

    try:
        # 尝试从结果中提取 JSON
        text = result_text.strip()
        # 处理可能的 markdown 代码块包裹
        if text.startswith("```"):
            lines = text.split("\n")
            json_lines = []
            in_block = False
            for line in lines:
                if line.strip().startswith("```") and not in_block:
                    in_block = True
                    continue
                elif line.strip() == "```" and in_block:
                    break
                elif in_block:
                    json_lines.append(line)
            text = "\n".join(json_lines)

        parsed = json.loads(text)

        # 校验结构，兼容纯字符串和 dict 两种格式
        result: Dict[str, List[dict]] = {}
        for key in empty_result:
            val = parsed.get(key, [])
            if isinstance(val, list):
                items = []
                for item in val:
                    if not item:
                        continue
                    if isinstance(item, dict):
                        t = item.get("text", "")
                        s = float(item.get("score", 0.5))
                        if t:
                            items.append({"text": str(t), "score": s})
                    elif isinstance(item, str) and item.strip():
                        # LLM 回退到旧格式（纯字符串），给默认分数
                        items.append({"text": item, "score": 0.5})
                result[key] = items
            else:
                result[key] = []

        return result

    except (json.JSONDecodeError, TypeError, AttributeError, ValueError) as e:
        logger.warning(
            f"[extract_experience] Failed to parse LLM response: {e}, "
            f"text: {result_text[:200]}"
        )
        return empty_result


async def update_user_profile(
    user_msg: str,
    assistant_msg: str,
    current_dims: Dict[str, str],
) -> Dict[str, str]:
    """
    通过 LLM 更新用户画像维度

    Args:
        user_msg: 用户消息
        assistant_msg: 助手回复
        current_dims: 当前画像维度

    Returns:
        需要更新的维度 {key: value}
    """
    # 加载提示词模板
    prompt = _load_prompt(
        "v4_profile_update",
        user_message=user_msg,
        assistant_message=assistant_msg,
        current_profile=json.dumps(current_dims, ensure_ascii=False, indent=2)
        if current_dims else "（空）",
    )

    result_text = await _call_llm(prompt)
    if not result_text:
        return {}

    try:
        text = result_text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            json_lines = []
            in_block = False
            for line in lines:
                if line.strip().startswith("```") and not in_block:
                    in_block = True
                    continue
                elif line.strip() == "```" and in_block:
                    break
                elif in_block:
                    json_lines.append(line)
            text = "\n".join(json_lines)

        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return {k: v for k, v in parsed.items() if v}
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning(
            f"[update_user_profile] Parse failed: {e}, text: {result_text[:200]}"
        )
    return {}


async def extract_knowledge(
    user_msg: str,
    assistant_msg: str,
) -> Dict[str, list]:
    """
    从对话中提取可复用知识（user_knowledge + system_knowledge）

    Returns:
        {"user_knowledge": [...], "system_knowledge": [...]}
    """
    prompt = _load_prompt(
        "v4_knowledge_extract",
        user_message=user_msg,
        assistant_message=assistant_msg,
    )

    result_text = await _call_llm(prompt)

    empty_result: Dict[str, list] = {
        "user_knowledge": [],
        "system_knowledge": [],
    }

    if not result_text:
        return empty_result

    try:
        text = result_text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            json_lines = []
            in_block = False
            for line in lines:
                if line.strip().startswith("```") and not in_block:
                    in_block = True
                    continue
                elif line.strip() == "```" and in_block:
                    break
                elif in_block:
                    json_lines.append(line)
            text = "\n".join(json_lines)

        parsed = json.loads(text)
        result: Dict[str, list] = {}
        for key in empty_result:
            val = parsed.get(key, [])
            if isinstance(val, list):
                items = []
                for item in val:
                    if not item:
                        continue
                    if isinstance(item, dict):
                        t = item.get("text", "")
                        s = float(item.get("score", 0.5))
                        if t:
                            items.append({"text": str(t), "score": s})
                    elif isinstance(item, str) and item.strip():
                        items.append({"text": item, "score": 0.5})
                result[key] = items
            else:
                result[key] = []
        return result
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning(
            f"[extract_knowledge] Parse failed: {e}, text: {result_text[:200]}"
        )
        return empty_result


async def extract_topics_and_entities(summary: str) -> Tuple[List[str], List[str]]:
    """
    从摘要文本提取主题关键词和命名实体

    签名匹配 context_manager.py:614 的 topic_extractor 回调:
        (summary_text) -> (topics: List[str], entities: List[str])

    失败时返回 ([], [])，不引入回归。
    """
    if not summary or not summary.strip():
        return [], []

    prompt = _load_prompt("v4_extract_topics", summary_text=summary)
    if not prompt:
        logger.warning("[extract_topics_and_entities] Prompt template not found")
        return [], []

    result_text = await _call_llm(prompt)
    if not result_text:
        return [], []

    try:
        text = result_text.strip()
        # 剥离 markdown fence
        if text.startswith("```"):
            lines = text.split("\n")
            json_lines = []
            in_block = False
            for line in lines:
                if line.strip().startswith("```") and not in_block:
                    in_block = True
                    continue
                elif line.strip() == "```" and in_block:
                    break
                elif in_block:
                    json_lines.append(line)
            text = "\n".join(json_lines)

        parsed = json.loads(text)
        topics = parsed.get("topics", [])
        entities = parsed.get("entities", [])

        # 类型校验
        if not isinstance(topics, list):
            topics = []
        if not isinstance(entities, list):
            entities = []

        topics = [str(t) for t in topics if t]
        entities = [str(e) for e in entities if e]

        logger.debug(
            f"[extract_topics_and_entities] topics={topics}, entities={entities}"
        )
        return topics, entities

    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning(
            f"[extract_topics_and_entities] Parse failed: {e}, "
            f"text: {result_text[:200]}"
        )
        return [], []


# ---------- 停用词缓存（从 config/memory_keywords.json 加载） ----------

_stopwords_cache: Optional[set] = None


def _load_stopwords() -> set:
    """加载停用词集合，缓存后续调用"""
    global _stopwords_cache
    if _stopwords_cache is not None:
        return _stopwords_cache

    try:
        p = Path(__file__).parent.parent.parent.parent / "config" / "memory_keywords.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            _stopwords_cache = set(data.get("stopwords", []))
        else:
            _stopwords_cache = set()
    except Exception as e:
        logger.warning(f"[_load_stopwords] Failed: {e}")
        _stopwords_cache = set()

    return _stopwords_cache


# CJK Unified Ideographs 正则
_RE_STOCK_CODE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_RE_ENGLISH_TOKEN = re.compile(r"[A-Za-z]{2,}")
_RE_CJK_SEQ = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+"
)


def extract_query_keywords(query_text: str, max_keywords: int = 10) -> List[str]:
    """
    轻量级同步关键词提取（热路径，每次请求调用）

    策略:
    1. 正则提取 6 位股票代码
    2. 提取英文 token（>= 2 字符）
    3. 提取 CJK 字符序列：
       - 2-8 字符直接作为关键词
       - >8 字符提取 n-gram (4, 3, 2)
    4. 过滤停用词（从 config/memory_keywords.json 加载）
    """
    if not query_text or not query_text.strip():
        return []

    keywords: list = []
    stopwords = _load_stopwords()

    # 1. 股票代码
    for code in _RE_STOCK_CODE.findall(query_text):
        keywords.append(code)

    # 2. 英文 token
    for tok in _RE_ENGLISH_TOKEN.findall(query_text):
        if tok.lower() not in stopwords and len(tok) >= 2:
            keywords.append(tok)

    # 3. CJK 序列
    for seq in _RE_CJK_SEQ.findall(query_text):
        if len(seq) <= 8:
            if seq not in stopwords:
                keywords.append(seq)
        else:
            # 长序列提取 n-gram
            for n in (4, 3, 2):
                for i in range(len(seq) - n + 1):
                    gram = seq[i:i + n]
                    if gram not in stopwords:
                        keywords.append(gram)

    # 去重保序 + 过滤停用词 + 限制数量
    seen: set = set()
    result: list = []
    for kw in keywords:
        if kw not in seen and kw not in stopwords:
            seen.add(kw)
            result.append(kw)
            if len(result) >= max_keywords:
                break

    return result


async def _call_llm(prompt: str) -> str:
    """调用 LLM 执行辅助任务（摘要/提取）"""
    from agent_core.agentloop.llm_provider import call_llm
    try:
        return await call_llm(prompt, max_tokens=1024, timeout=60.0)
    except Exception as e:
        logger.error(f"[llm_helpers] LLM call failed: {e}")
        return ""


async def _call_anthropic_api_direct(prompt: str) -> str:
    """调用 LLM（保留签名兼容）"""
    from agent_core.agentloop.llm_provider import call_llm
    try:
        return await call_llm(prompt, max_tokens=1024, timeout=60.0)
    except Exception as e:
        logger.error(f"[llm_helpers] LLM direct call failed: {e}")
        return ""


async def summarize_for_dingtalk(
    full_analysis: str,
    user_query: str,
    report_lang: str = "zh",
) -> str:
    """
    使用 LLM 将完整分析精简为钉钉摘要版

    失败时 fallback 截断前 500 字。
    """
    if not full_analysis or len(full_analysis) < 300:
        return full_analysis  # 已经够短，不需要摘要

    lang = "中文" if report_lang == "zh" else "English"
    prompt = _load_prompt(
        "v4_dingtalk_summary",
        user_query=user_query[:200],
        full_analysis=full_analysis[:3000],  # 限制输入长度
        lang=lang,
    )
    if not prompt:
        return full_analysis[:500] + "..."

    result = await _call_llm(prompt)
    if result and len(result) < len(full_analysis):
        return result

    # fallback: 截断
    return full_analysis[:500] + "..."


async def extract_skill_params(
    query: str,
    skill_name: str,
    input_schema: Dict[str, Any],
) -> Dict[str, Any]:
    """
    使用 LLM 从自然语言查询中智能提取 Skill 所需参数

    Args:
        query: 用户的自然语言查询（如"分析俄乌冲突最新态势"）
        skill_name: 技能名称（如"geopolitical_security"）
        input_schema: 技能的输入参数模式定义

    Returns:
        提取出的参数字典（如 {"country": "乌克兰", "topic": "冲突"}）
    """
    schema_desc_parts = []
    for param_name, param_info in input_schema.items():
        required = param_info.get("required", False)
        desc = param_info.get("description", "")
        param_type = param_info.get("type", "string")
        required_mark = "[必填]" if required else "[可选]"
        schema_desc_parts.append(f"  - {param_name} ({param_type}): {desc} {required_mark}")

    schema_desc = "\n".join(schema_desc_parts)

    prompt = _load_prompt(
        "v4_extract_skill_params",
        skill_name=skill_name,
        query=query,
        schema_desc=schema_desc,
    )

    try:
        result = await _call_anthropic_api_direct(prompt)
        result = result.strip()

        if result.startswith("```"):
            lines = result.split("\n")
            json_lines = []
            in_json = False
            for line in lines:
                if line.startswith("```json"):
                    in_json = True
                    continue
                elif line.startswith("```"):
                    in_json = False
                    continue
                if in_json:
                    json_lines.append(line)
            result = "\n".join(json_lines)

        extracted = json.loads(result)
        logger.info(f"[extract_skill_params] Skill={skill_name}, query='{query[:50]}...' -> {extracted}")
        return extracted

    except json.JSONDecodeError as e:
        logger.warning(f"[extract_skill_params] JSON parse error: {e}, raw: {result[:200]}")
        return {"query": query}
    except Exception as e:
        logger.warning(f"[extract_skill_params] Failed: {e}")
        return {"query": query}
