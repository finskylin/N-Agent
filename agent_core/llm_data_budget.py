"""
LLM Data Budget -- 智能裁剪工具返回数据，防止超出 CLI token 上限

当 skill 返回数据超过 CLI token 上限时，CLI 会将结果替换为
"Result exceeds maximum allowed tokens"，LLM 看不到任何数据。

本模块在数据返回 CLI 之前做智能裁剪:
- 高置信度内容保留完整原文
- 保证 LLM 至少看到 N 篇完整文章
- 低置信度内容降级为摘要
- 非文本数据（base64/GeoJSON/轨迹）对 LLM 省略

所有阈值通过 V4Config + 环境变量配置。
"""
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger


@dataclass
class BudgetConfig:
    """LLM 数据预算配置 -- 所有阈值从 V4Config 读取"""

    max_llm_chars: int = 150_000          # LLM 可接收的最大字符数，默认 150K (~37K tokens)
    min_full_articles: int = 5            # 保证完整文章的最低数量
    max_full_articles: int = 15           # 完整文章上限
    high_score_threshold: float = 60.0    # 高分阈值 (0-100 标度)
    summary_max_chars: int = 800          # 低优先级条目摘要的最大字符数
    timeseries_max_records: int = 30      # 时序数据保留的最大记录数
    strip_fields: List[str] = field(default_factory=list)  # 全局必须剥离的字段名
    enabled: bool = True                  # 功能开关

    @classmethod
    def from_v4_config(cls, config) -> "BudgetConfig":
        """从 V4Config 构建 BudgetConfig"""
        strip_str = getattr(config, "llm_budget_strip_fields", "")
        strip_list = [f.strip() for f in strip_str.split(",") if f.strip()] if strip_str else []

        return cls(
            max_llm_chars=getattr(config, "llm_budget_max_chars", 150_000),
            min_full_articles=getattr(config, "llm_budget_min_full_articles", 5),
            max_full_articles=getattr(config, "llm_budget_max_full_articles", 15),
            high_score_threshold=getattr(config, "llm_budget_high_score_threshold", 60.0),
            summary_max_chars=getattr(config, "llm_budget_summary_max_chars", 800),
            timeseries_max_records=getattr(config, "llm_budget_timeseries_max_records", 30),
            strip_fields=strip_list,
            enabled=getattr(config, "llm_budget_enabled", True),
        )


def budget_for_llm(
    skill_name: str,
    data: Any,
    config: BudgetConfig,
    skill_strip_fields: Optional[List[str]] = None,
) -> Any:
    """
    对 skill 返回数据做智能裁剪，确保 LLM 能看到有效数据。

    只裁剪 LLM 看到的数据，不影响:
    - UI 渲染（使用 sidecar 全量文件）
    - MySQL 持久化（hook_manager 从 sidecar 读取）
    - 历史 session 回放（从 MySQL 恢复）

    Args:
        skill_name: 技能名称
        data: 原始数据（dict 或其他）
        config: 预算配置
        skill_strip_fields: per-skill 额外剥离字段（从 SKILL.md llm_strip_fields）

    Returns:
        裁剪后的数据（dict 或原始类型）
    """
    if not config.enabled:
        return data

    if not isinstance(data, dict):
        # 非 dict 类型，只做字符串长度检查
        text = json.dumps(data, ensure_ascii=False, default=str) if not isinstance(data, str) else data
        if len(text) <= config.max_llm_chars:
            return data
        return text[:config.max_llm_chars] + "...[truncated]"

    try:
        # 深拷贝，避免修改原始数据（原始数据用于 UI 渲染和持久化）
        result = deepcopy(data)

        # Phase 1: 字段剥离
        all_strip_fields = list(config.strip_fields)
        if skill_strip_fields:
            all_strip_fields.extend(skill_strip_fields)
        if all_strip_fields:
            result = strip_fields_recursive(result, all_strip_fields)

        # Phase 1b: 自动检测并移除 base64 和 GeoJSON
        result = _auto_strip_heavy_data(result)

        # Phase 2: 数据类型检测
        category = detect_category(result)
        logger.debug(f"[LLM Budget] {skill_name}: detected category={category}")

        # Phase 3: 类型专属裁剪
        if category == "scored_articles":
            result = _trim_scored_articles(result, config)
        elif category == "events_list":
            result = _trim_events_list(result, config)
        elif category == "timeseries":
            result = _trim_timeseries(result, config)
        else:
            result = _trim_generic(result, config)

        # Phase 4: 总量控制
        result = _enforce_total_limit(result, config)

        logger.info(
            f"[LLM Budget] {skill_name}: category={category}, "
            f"final_size={len(json.dumps(result, ensure_ascii=False, default=str))}"
        )
        return result

    except Exception as e:
        logger.warning(f"[LLM Budget] {skill_name}: trimming failed: {e}, returning raw data")
        return data


# ============================================================
# Phase 1: 字段剥离
# ============================================================

def strip_fields_recursive(data: Any, fields: List[str]) -> Any:
    """递归遍历数据，移除指定字段名"""
    if isinstance(data, dict):
        return {
            k: strip_fields_recursive(v, fields)
            for k, v in data.items()
            if k not in fields
        }
    elif isinstance(data, list):
        return [strip_fields_recursive(item, fields) for item in data]
    return data


def _auto_strip_heavy_data(data: Any) -> Any:
    """自动检测并移除 base64 字符串、大型 GeoJSON 等非文本重数据"""
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            # GeoJSON FeatureCollection → 压缩为摘要
            if isinstance(v, dict) and v.get("type") == "FeatureCollection":
                features = v.get("features", [])
                result[k] = {
                    "_type": "geojson_stripped",
                    "feature_count": len(features),
                    "bbox": v.get("bbox"),
                }
                continue

            # 超长无空格字符串 → 大概率是 base64
            if isinstance(v, str) and len(v) > 1000 and " " not in v[:500]:
                # 检查是否像 base64 (字母+数字+/+=)
                if re.match(r'^[A-Za-z0-9+/=\n]+$', v[:200]):
                    result[k] = f"[base64_stripped, length={len(v)}]"
                    continue

            result[k] = _auto_strip_heavy_data(v)
        return result
    elif isinstance(data, list):
        return [_auto_strip_heavy_data(item) for item in data]
    return data


# ============================================================
# Phase 2: 数据类型检测
# ============================================================

def detect_category(data: Any) -> str:
    """
    基于数据结构特征检测类型（不依赖 skill 名称）

    Returns:
        "scored_articles" | "events_list" | "timeseries" | "generic"
    """
    if not isinstance(data, dict):
        return "generic"

    # scored_articles: 含 results/collected_data 数组，且条目有评分字段
    for list_key in ("results", "collected_data", "items", "search_results"):
        arr = data.get(list_key)
        if isinstance(arr, list) and len(arr) >= 2:
            # 检查前几个条目是否有评分字段
            score_fields = {"final_score", "relevance_score", "relevance", "score", "authority_score"}
            sample = arr[:3]
            has_scores = any(
                isinstance(item, dict) and bool(set(item.keys()) & score_fields)
                for item in sample
            )
            if has_scores:
                return "scored_articles"

    # 嵌套在 intelligent_data 中
    intel_data = data.get("intelligent_data", {})
    if isinstance(intel_data, dict):
        collected = intel_data.get("collected_data", [])
        if isinstance(collected, list) and len(collected) >= 2:
            sample = collected[:3]
            score_fields = {"relevance", "relevance_score", "authority_score"}
            has_scores = any(
                isinstance(item, dict) and bool(set(item.keys()) & score_fields)
                for item in sample
            )
            if has_scores:
                return "scored_articles"

    # events_list: 含 events/acled_events/items 数组，条目数 > 10
    for list_key in ("events", "acled_events", "gdelt_events", "articles"):
        arr = data.get(list_key)
        if isinstance(arr, list) and len(arr) > 10:
            return "events_list"

    # timeseries: 含 data 数组，条目有日期字段
    for list_key in ("data", "records", "kline_data", "history"):
        arr = data.get(list_key)
        if isinstance(arr, list) and len(arr) >= 5:
            sample = arr[:3]
            date_fields = {"trade_date", "date", "timestamp", "datetime", "time"}
            has_dates = any(
                isinstance(item, dict) and bool(set(item.keys()) & date_fields)
                for item in sample
            )
            if has_dates:
                return "timeseries"

    return "generic"


# ============================================================
# Phase 3: 类型专属裁剪
# ============================================================

def _trim_scored_articles(data: dict, config: BudgetConfig) -> dict:
    """
    裁剪评分文章列表:
    - 按评分降序排列
    - 高分条目保留完整
    - 低分条目降级为摘要
    """
    result = dict(data)

    # 找到包含评分文章的数组
    list_key = None
    articles = None
    for key in ("results", "collected_data", "items", "search_results"):
        arr = data.get(key)
        if isinstance(arr, list) and len(arr) >= 1:
            list_key = key
            articles = arr
            break

    # 检查嵌套 intelligent_data
    if articles is None:
        intel = data.get("intelligent_data", {})
        if isinstance(intel, dict):
            arr = intel.get("collected_data", [])
            if isinstance(arr, list) and len(arr) >= 1:
                list_key = "intelligent_data.collected_data"
                articles = arr

    if not articles:
        return result

    total = len(articles)

    # 按评分排序
    scored = []
    for item in articles:
        score = normalize_score(item)
        scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)

    # 决定完整保留的数量
    high_score_count = sum(1 for s, _ in scored if s >= config.high_score_threshold)
    full_count = min(max(high_score_count, config.min_full_articles), config.max_full_articles)
    full_count = min(full_count, total)

    # 裁剪
    trimmed = []
    for i, (score, item) in enumerate(scored):
        if i < full_count:
            trimmed.append(item)
        else:
            trimmed.append(summarize_item(item, config.summary_max_chars))

    # 写回
    if list_key == "intelligent_data.collected_data":
        result["intelligent_data"] = dict(data.get("intelligent_data", {}))
        result["intelligent_data"]["collected_data"] = trimmed
    else:
        result[list_key] = trimmed

    result["_budget_info"] = {
        "total": total,
        "full_count": full_count,
        "summarized_count": total - full_count,
        "category": "scored_articles",
    }

    return result


def _trim_events_list(data: dict, config: BudgetConfig) -> dict:
    """
    裁剪事件列表:
    - 保留统计/聚合字段
    - 事件数组截断到前 50 条
    - 每条文本字段截断到 800 字符
    """
    result = dict(data)
    max_events = 50
    max_event_text = 800

    for list_key in ("events", "acled_events", "gdelt_events", "articles"):
        arr = data.get(list_key)
        if not isinstance(arr, list) or len(arr) <= max_events:
            continue

        total = len(arr)
        trimmed = []
        for item in arr[:max_events]:
            if isinstance(item, dict):
                trimmed_item = {}
                for k, v in item.items():
                    if isinstance(v, str) and len(v) > max_event_text:
                        trimmed_item[k] = v[:max_event_text] + "..."
                    else:
                        trimmed_item[k] = v
                trimmed.append(trimmed_item)
            else:
                trimmed.append(item)

        result[list_key] = trimmed
        result[f"_events_budget_info"] = {"total": total, "kept": len(trimmed)}

    return result


def _trim_timeseries(data: dict, config: BudgetConfig) -> dict:
    """
    裁剪时序数据:
    - 计算聚合统计
    - 保留最近 N 条记录
    """
    result = dict(data)

    for list_key in ("data", "records", "kline_data", "history"):
        arr = data.get(list_key)
        if not isinstance(arr, list) or len(arr) <= config.timeseries_max_records:
            continue

        total = len(arr)
        # 计算聚合
        agg = compute_aggregates(arr)
        # 保留最近 N 条
        result[list_key] = arr[-config.timeseries_max_records:]
        result["_timeseries_info"] = {"total_records": total, "kept": config.timeseries_max_records}
        if agg:
            result["_aggregate"] = agg

    return result


def _trim_generic(data: dict, config: BudgetConfig) -> dict:
    """
    通用裁剪:
    - 长字符串截断
    - 大数组保留首尾
    """
    return _trim_generic_recursive(data, config, depth=0)


def _trim_generic_recursive(data: Any, config: BudgetConfig, depth: int) -> Any:
    """递归通用裁剪"""
    if depth > 8:
        return data

    max_str_len = 8000
    max_array_size = 30
    array_head = 20
    array_tail = 5

    if isinstance(data, str):
        if len(data) > max_str_len:
            return data[:max_str_len] + f"...[truncated, total {len(data)} chars]"
        return data

    if isinstance(data, list):
        if len(data) > max_array_size:
            head = [_trim_generic_recursive(item, config, depth + 1) for item in data[:array_head]]
            tail = [_trim_generic_recursive(item, config, depth + 1) for item in data[-array_tail:]]
            return head + [f"...[{len(data) - array_head - array_tail} items omitted]"] + tail
        return [_trim_generic_recursive(item, config, depth + 1) for item in data]

    if isinstance(data, dict):
        return {
            k: _trim_generic_recursive(v, config, depth + 1)
            for k, v in data.items()
        }

    return data


# ============================================================
# Phase 4: 总量控制
# ============================================================

def _enforce_total_limit(data: Any, config: BudgetConfig) -> Any:
    """渐进式缩减，确保总字符数在预算内"""
    json_text = json.dumps(data, ensure_ascii=False, default=str)
    if len(json_text) <= config.max_llm_chars:
        return data

    logger.info(
        f"[LLM Budget] Total limit exceeded: {len(json_text)} > {config.max_llm_chars}, "
        f"starting progressive reduction"
    )

    if not isinstance(data, dict):
        return json.loads(json_text[:config.max_llm_chars])

    result = data

    # Step 1: 减少完整文章数
    for list_key in ("results", "collected_data", "items", "search_results"):
        arr = result.get(list_key)
        if not isinstance(arr, list) or len(arr) <= config.min_full_articles:
            continue

        current_full = len(arr)
        while current_full > config.min_full_articles:
            current_full -= 1
            # 将末尾条目转为摘要
            if isinstance(arr[current_full], dict):
                arr[current_full] = summarize_item(arr[current_full], config.summary_max_chars)

            check = json.dumps(result, ensure_ascii=False, default=str)
            if len(check) <= config.max_llm_chars:
                return result

    # 嵌套 intelligent_data
    intel = result.get("intelligent_data", {})
    if isinstance(intel, dict):
        arr = intel.get("collected_data", [])
        if isinstance(arr, list) and len(arr) > config.min_full_articles:
            current_full = len(arr)
            while current_full > config.min_full_articles:
                current_full -= 1
                if isinstance(arr[current_full], dict):
                    arr[current_full] = summarize_item(arr[current_full], config.summary_max_chars)
                check = json.dumps(result, ensure_ascii=False, default=str)
                if len(check) <= config.max_llm_chars:
                    return result

    # Step 2: 缩短摘要长度（保持原始摘要长度，不再减半）
    result = _re_summarize_all(result, config.summary_max_chars)
    check = json.dumps(result, ensure_ascii=False, default=str)
    if len(check) <= config.max_llm_chars:
        return result

    # Step 3: 截断最长字段
    result = _truncate_longest_fields(result, config.max_llm_chars)

    return result


def _re_summarize_all(data: Any, max_chars: int) -> Any:
    """重新摘要化所有包含 _summarized 标记的条目"""
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if isinstance(v, dict) and v.get("_summarized"):
                result[k] = summarize_item(v, max_chars)
            else:
                result[k] = _re_summarize_all(v, max_chars)
        return result
    elif isinstance(data, list):
        return [
            summarize_item(item, max_chars)
            if isinstance(item, dict) and item.get("_summarized")
            else _re_summarize_all(item, max_chars)
            for item in data
        ]
    return data


def _truncate_longest_fields(data: dict, max_chars: int) -> dict:
    """找到最长的字符串字段并截断"""
    json_text = json.dumps(data, ensure_ascii=False, default=str)
    if len(json_text) <= max_chars:
        return data

    # 收集所有字符串字段及长度
    str_fields = []
    _collect_string_fields(data, [], str_fields)
    str_fields.sort(key=lambda x: x[1], reverse=True)

    result = deepcopy(data)
    for path, length in str_fields:
        if length < 500:
            break
        # 截断到一半
        _set_nested(result, path, _get_nested(result, path)[:length // 2] + "...[truncated]")
        check = json.dumps(result, ensure_ascii=False, default=str)
        if len(check) <= max_chars:
            return result

    # 最终兜底: 硬截断 JSON
    json_text = json.dumps(result, ensure_ascii=False, default=str)
    if len(json_text) > max_chars:
        truncated = json_text[:max_chars - 100]
        try:
            return json.loads(truncated + '"}')
        except (json.JSONDecodeError, ValueError):
            # 预留足够空间给 wrapper JSON + 转义膨胀
            safe_len = max(100, max_chars // 3)
            fallback = {"_budget_truncated": True, "partial_data": truncated[:safe_len]}
            # 验证最终大小
            fallback_text = json.dumps(fallback, ensure_ascii=False, default=str)
            if len(fallback_text) > max_chars:
                # 再次缩减
                fallback["partial_data"] = fallback["partial_data"][:safe_len // 2]
            return fallback

    return result


def _collect_string_fields(data: Any, path: list, result: list):
    """递归收集所有字符串字段的路径和长度"""
    if isinstance(data, str) and len(data) > 200:
        result.append((list(path), len(data)))
    elif isinstance(data, dict):
        for k, v in data.items():
            _collect_string_fields(v, path + [k], result)
    elif isinstance(data, list):
        for i, v in enumerate(data[:50]):  # 限制遍历数量
            _collect_string_fields(v, path + [i], result)


def _get_nested(data: Any, path: list) -> Any:
    """按路径获取嵌套值"""
    current = data
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and isinstance(key, int) and key < len(current):
            current = current[key]
        else:
            return None
    return current


def _set_nested(data: Any, path: list, value: Any):
    """按路径设置嵌套值"""
    current = data
    for key in path[:-1]:
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and isinstance(key, int) and key < len(current):
            current = current[key]
        else:
            return
    last_key = path[-1]
    if isinstance(current, dict):
        current[last_key] = value
    elif isinstance(current, list) and isinstance(last_key, int) and last_key < len(current):
        current[last_key] = value


# ============================================================
# 辅助函数
# ============================================================

def normalize_score(item: dict) -> float:
    """统一不同评分标度到 0-100"""
    if not isinstance(item, dict):
        return 0.0

    # 0-100 标度
    for key in ("final_score", "relevance_score", "score"):
        val = item.get(key)
        if val is not None:
            try:
                score = float(val)
                return score if score > 1.0 else score * 100
            except (ValueError, TypeError):
                continue

    # 0-1 标度
    for key in ("relevance", "authority_score"):
        val = item.get(key)
        if val is not None:
            try:
                score = float(val)
                return score * 100 if score <= 1.0 else score
            except (ValueError, TypeError):
                continue

    return 0.0


def summarize_item(item: dict, max_chars: int = 300) -> dict:
    """
    将完整条目降级为摘要:
    - 保留: title, link/source/url, date, 评分字段 及所有短字段
    - 长文本内容截断到 max_chars
    - 大型 dict/list 值移除（仅移除大内容字段）
    """
    if not isinstance(item, dict):
        return item

    # 长文本内容字段名（需要截断）
    content_fields = {
        "full_content", "data", "content", "snippet", "text",
        "description", "summary", "abstract", "body",
    }

    result = {}
    for k, v in item.items():
        if k in content_fields:
            # 长文本字段: 截断
            if isinstance(v, str):
                if len(v) > max_chars:
                    result[k] = v[:max_chars] + "..."
                else:
                    result[k] = v
            # 非字符串的 content 字段（大 dict/list）: 跳过
        elif isinstance(v, str):
            # 保留所有短字符串字段
            if len(v) <= 500:
                result[k] = v
            else:
                result[k] = v[:500] + "..."
        elif isinstance(v, (int, float, bool, type(None))):
            # 保留所有简单值
            result[k] = v
        elif isinstance(v, list) and len(v) <= 5:
            # 保留小数组
            result[k] = v
        elif isinstance(v, dict) and len(str(v)) <= 500:
            # 保留小 dict
            result[k] = v
        # 其他大型 list/dict: 跳过

    result["_summarized"] = True
    return result


def compute_aggregates(records: list) -> dict:
    """对数值列计算 min/max/avg/latest"""
    if not records or not isinstance(records[0], dict):
        return {}

    # 收集数值列
    numeric_cols = {}
    for key in records[0]:
        vals = []
        for r in records:
            if isinstance(r, dict):
                v = r.get(key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    vals.append(v)
        if vals and len(vals) > len(records) * 0.5:
            numeric_cols[key] = vals

    agg = {}
    for col, vals in numeric_cols.items():
        agg[col] = {
            "min": round(min(vals), 4),
            "max": round(max(vals), 4),
            "avg": round(sum(vals) / len(vals), 4),
            "latest": vals[-1],
            "count": len(vals),
        }

        # 趋势方向（简单：对比首尾 20%）
        if len(vals) >= 5:
            head_avg = sum(vals[:len(vals) // 5 + 1]) / (len(vals) // 5 + 1)
            tail_avg = sum(vals[-len(vals) // 5 - 1:]) / (len(vals) // 5 + 1)
            if tail_avg > head_avg * 1.02:
                agg[col]["trend"] = "up"
            elif tail_avg < head_avg * 0.98:
                agg[col]["trend"] = "down"
            else:
                agg[col]["trend"] = "flat"

    return agg
