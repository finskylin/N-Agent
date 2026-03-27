"""
GAIA 官方评分器

复制自 HuggingFace GAIA Leaderboard 官方评分逻辑:
- 数字: 去除 $, %, , 后 float 精确比较
- 字符串: 去除空白 + 标点 + 小写后比较
- 列表: 按 , 或 ; 分割，逐元素比较（顺序敏感）
- None → "None"
"""
import re
import string
from typing import Optional


def _normalize_number_str(s: str) -> Optional[float]:
    """尝试将字符串解析为数字，去除常见格式符号"""
    # 去除货币符号、百分号、逗号
    cleaned = s.strip()
    cleaned = cleaned.replace("$", "").replace("%", "").replace(",", "")
    cleaned = cleaned.replace("€", "").replace("£", "").replace("¥", "")
    cleaned = cleaned.strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _normalize_str(s: str) -> str:
    """规范化字符串：小写 + 去除标点 + 去除多余空白"""
    s = s.lower().strip()
    # 去除标点
    s = s.translate(str.maketrans("", "", string.punctuation))
    # 去除冠词
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    # 压缩空白
    s = " ".join(s.split())
    return s


def _split_list(s: str) -> list[str]:
    """将字符串按 , 或 ; 分割为列表"""
    # 优先用分号分割
    if ";" in s:
        items = s.split(";")
    else:
        items = s.split(",")
    return [item.strip() for item in items if item.strip()]


def question_scorer(model_answer: Optional[str], ground_truth: str) -> bool:
    """
    GAIA 官方评分逻辑

    Args:
        model_answer: 模型提取的最终答案（可能为 None）
        ground_truth: 标准答案

    Returns:
        True 表示回答正确
    """
    if model_answer is None:
        return ground_truth.lower().strip() == "none"

    model_answer = str(model_answer).strip()
    ground_truth = str(ground_truth).strip()

    # 空答案
    if not model_answer:
        return not ground_truth

    # 1. 尝试数字比较
    model_num = _normalize_number_str(model_answer)
    truth_num = _normalize_number_str(ground_truth)
    if model_num is not None and truth_num is not None:
        # 浮点精确比较（容差 1e-6）
        return abs(model_num - truth_num) < 1e-6

    # 2. 检查是否为列表（ground truth 包含逗号或分号）
    if "," in ground_truth or ";" in ground_truth:
        model_items = _split_list(model_answer)
        truth_items = _split_list(ground_truth)
        if len(model_items) == len(truth_items):
            return all(
                _normalize_str(m) == _normalize_str(t)
                for m, t in zip(model_items, truth_items)
            )
        return False

    # 3. 字符串比较
    return _normalize_str(model_answer) == _normalize_str(ground_truth)
