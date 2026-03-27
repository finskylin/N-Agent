"""
Skill 输出适配器

将各种技能的返回格式转换为统一的 SkillOutput 结构
支持向后兼容旧格式

设计文档: doc_design/agent2/data_store.md
"""
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from loguru import logger

from agent_core.skill_output import SkillOutput, CollectedItem


class SkillOutputAdapter:
    """
    技能输出适配器

    将不同格式的技能输出转换为统一的 SkillOutput 结构
    """

    @classmethod
    def from_web_search_result(
        cls,
        result: Dict[str, Any],
        skill_name: str = "web_search",
        user_id: int = 1,
        session_id: str = "",
        query: str = "",
        duration_ms: int = 0,
    ) -> SkillOutput:
        """
        从 web_search 结果转换

        web_search 返回格式:
        {
            "for_llm": {
                "query": str,
                "results": List[Dict],
                "content_summary": str,
                ...
            },
            "for_ui": {
                "components": List[Dict]
            }
        }
        """
        output = SkillOutput(
            skill_name=skill_name,
            user_id=user_id,
            session_id=session_id,
            query=query,
            duration_ms=duration_ms,
        )

        # 处理 for_llm 部分 - 提取原始数据
        for_llm = result.get("for_llm", {})
        results = for_llm.get("results", [])

        for item in results:
            collected = cls._convert_search_result_to_item(item)
            output.raw_data.append(collected)

        # 处理 for_ui 部分 - 提取 UI 组件
        for_ui = result.get("for_ui", {})
        output.ui_components = for_ui.get("components", [])

        # 如果是错误结果
        if isinstance(result, list) and result and "error" in result[0]:
            output.success = False
            output.error = result[0].get("error")
        elif "error" in result:
            output.success = False
            output.error = result.get("error")

        return output

    @classmethod
    def from_generic_result(
        cls,
        result: Any,
        skill_name: str,
        user_id: int = 1,
        session_id: str = "",
        query: str = "",
        duration_ms: int = 0,
    ) -> SkillOutput:
        """
        从通用结果转换

        尝试智能识别结果格式并转换
        """
        output = SkillOutput(
            skill_name=skill_name,
            user_id=user_id,
            session_id=session_id,
            query=query,
            duration_ms=duration_ms,
        )

        # 1. 已经是 SkillOutput
        if isinstance(result, SkillOutput):
            return result

        # 2. 是字典，包含 for_llm/for_ui 结构（web_search 格式）
        if isinstance(result, dict):
            if "for_llm" in result or "for_ui" in result:
                return cls.from_web_search_result(
                    result, skill_name, user_id, session_id, query, duration_ms
                )

            # 3. 是字典，包含 raw_data 字段（已转换格式）
            if "raw_data" in result:
                return SkillOutput.from_dict(result)

            # 4. 普通字典结果
            output.raw_data.append(CollectedItem(
                source_name=skill_name,
                content=str(result),
                extra=result if isinstance(result, dict) else {"data": result},
            ))

            # 提取 UI 组件
            if "ui_components" in result:
                output.ui_components = result.get("ui_components", [])
            elif "components" in result:
                output.ui_components = result.get("components", [])

            # 检查错误
            if result.get("error"):
                output.success = False
                output.error = result.get("error")

        # 5. 是列表
        elif isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    # 检查是否是错误
                    if "error" in item:
                        output.success = False
                        output.error = item.get("error")
                    else:
                        collected = cls._convert_dict_to_item(item, skill_name)
                        output.raw_data.append(collected)
                else:
                    output.raw_data.append(CollectedItem(
                        source_name=skill_name,
                        content=str(item),
                    ))

        # 6. 其他类型
        else:
            output.raw_data.append(CollectedItem(
                source_name=skill_name,
                content=str(result),
            ))

        return output

    @classmethod
    def _convert_search_result_to_item(cls, item: Dict[str, Any]) -> CollectedItem:
        """转换搜索结果项为 CollectedItem"""
        return CollectedItem(
            source_url=item.get("link") or item.get("url", ""),
            source_name=item.get("source") or item.get("engine", "unknown"),
            source_type=cls._infer_source_type(item.get("link", "")),
            title=item.get("title", ""),
            content=item.get("content") or item.get("details", ""),
            snippet=item.get("snippet", ""),
            data_time=cls._parse_date(item.get("date")),
            fetch_time=datetime.now(),
            content_type="text",
            authority_score=item.get("score", 0.5) / 100 if item.get("score", 0) > 1 else item.get("score", 0.5),
            extra={
                "engine": item.get("engine"),
                "is_tracking_site": item.get("is_tracking_site", False),
            },
        )

    @classmethod
    def _convert_dict_to_item(cls, item: Dict[str, Any], skill_name: str) -> CollectedItem:
        """转换通用字典为 CollectedItem"""
        return CollectedItem(
            source_url=item.get("url") or item.get("link", ""),
            source_name=item.get("source") or skill_name,
            source_type=cls._infer_source_type(item.get("url", "")),
            title=item.get("title") or item.get("name", ""),
            content=item.get("content") or item.get("text", ""),
            snippet=item.get("snippet") or item.get("summary", ""),
            data_time=cls._parse_date(item.get("date") or item.get("time")),
            fetch_time=datetime.now(),
            extra=item,
        )

    @classmethod
    def _infer_source_type(cls, url: str) -> str:
        """根据 URL 推断来源类型"""
        if not url:
            return "unknown"

        url_lower = url.lower()

        # 官方/政府来源
        if any(domain in url_lower for domain in [".gov", ".mil", "gov.cn", "mod.gov"]):
            return "official"

        # 学术来源
        if any(domain in url_lower for domain in [".edu", "arxiv", "scholar", "researchgate", "cnki"]):
            return "academic"

        # 主流媒体
        if any(domain in url_lower for domain in [
            "reuters", "bbc", "cnn", "nytimes", "washingtonpost",
            "xinhua", "people.com.cn", "chinadaily", "cctv"
        ]):
            return "media"

        # 社区/论坛
        if any(domain in url_lower for domain in ["reddit", "twitter", "weibo", "zhihu", "tieba"]):
            return "community"

        # 百科
        if any(domain in url_lower for domain in ["wikipedia", "baike.baidu", "wiki"]):
            return "encyclopedia"

        return "unknown"

    @classmethod
    def _parse_date(cls, date_str: Optional[str]) -> Optional[datetime]:
        """解析日期字符串"""
        if not date_str:
            return None

        # 尝试多种格式
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%m/%d/%Y",
            "%d/%m/%Y",
        ]

        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except (ValueError, TypeError):
                continue

        return None


def adapt_skill_result(
    result: Any,
    skill_name: str,
    user_id: int = 1,
    session_id: str = "",
    query: str = "",
    start_time: float = None,
) -> SkillOutput:
    """
    便捷函数：将技能结果转换为 SkillOutput

    Args:
        result: 技能执行结果（任意格式）
        skill_name: 技能名称
        user_id: 用户 ID
        session_id: 会话 ID
        query: 原始查询
        start_time: 执行开始时间（用于计算耗时）

    Returns:
        SkillOutput 实例
    """
    duration_ms = 0
    if start_time:
        duration_ms = int((time.time() - start_time) * 1000)

    return SkillOutputAdapter.from_generic_result(
        result=result,
        skill_name=skill_name,
        user_id=user_id,
        session_id=session_id,
        query=query,
        duration_ms=duration_ms,
    )
