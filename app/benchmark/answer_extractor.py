"""
GAIA 答案提取器

从 Agent 的完整输出中提取 "FINAL ANSWER:" 后面的文本。
"""
import re
from typing import Optional


def extract_final_answer(
    text: str,
    marker: str = "FINAL ANSWER:",
    case_insensitive: bool = True,
) -> Optional[str]:
    """
    从 Agent 输出中提取最终答案。

    提取规则:
    1. 查找最后一次出现的 marker
    2. 取 marker 之后的所有文本
    3. 清理首尾空白

    Args:
        text: Agent 的完整输出文本
        marker: 答案标记（默认 "FINAL ANSWER:"）
        case_insensitive: 是否忽略大小写匹配 marker

    Returns:
        提取的答案文本，未找到 marker 返回 None
    """
    if not text or not text.strip():
        return None

    flags = re.IGNORECASE if case_insensitive else 0
    # 使用 re.escape 防止 marker 中的特殊字符
    pattern = re.escape(marker)

    # 查找所有匹配位置，取最后一个
    matches = list(re.finditer(pattern, text, flags))
    if not matches:
        return None

    last_match = matches[-1]
    answer = text[last_match.end():].strip()

    # 去除可能的 markdown 格式
    answer = answer.strip("`").strip("*").strip()

    # 如果答案被引号包裹，去除引号
    if len(answer) >= 2 and answer[0] == answer[-1] and answer[0] in ('"', "'"):
        answer = answer[1:-1].strip()

    return answer if answer else None
