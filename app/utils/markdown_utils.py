"""
Markdown 工具函数 — 钉钉渠道后处理共用

从 bot_handler.py 提取，供 /cron-execute 端点和 bot_handler 共用。
"""
from loguru import logger


def truncate_markdown_safe(text: str, max_chars: int, image_only: bool = False) -> str:
    """在 max_chars 附近截断，但不切断 Markdown 块结构。

    识别代码块(```)、表格(|...|)、有序/无序列表连续体，
    如果 max_chars 落在块内部，回退到块起始位置之前截断。

    Args:
        text: 原始文本
        max_chars: 最大字符数
        image_only: 全图片模式，只保留图片标记，截取所有文字

    Returns:
        截断后的文本
    """
    # 全图片模式：只保留图片标记，截取所有文字
    if image_only:
        # 保留钉钉支持的图片格式：![alt](url) 和 <img src="...">
        import re
        # 提取所有图片
        images = re.findall(r'!\[.*?\]\([^)]+\)|<img[^>]+src=["\'][^"\']*["\'][^"\']*[^>]+>', text, re.IGNORECASE)
        if images:
            # 按照在文本中出现的顺序排列图片
            return "\n".join(images[:10])  # 最多保留 10 张图片
        else:
            return "[报告图片加载中...]"

    if len(text) <= max_chars:
        return text

    # 扫描所有块的 [start, end) 区间
    blocks = []  # [(start, end), ...]
    lines = text.split("\n")
    pos = 0  # 当前字符偏移
    in_code_block = False
    code_block_start = 0
    table_start = -1
    i = 0
    while i < len(lines):
        line = lines[i]
        line_end = pos + len(line) + 1  # +1 for \n

        stripped = line.strip()

        # 代码块: ``` 开/关
        if stripped.startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_block_start = pos
            else:
                in_code_block = False
                blocks.append((code_block_start, line_end))
        # 表格行: | xxx | xxx |
        elif not in_code_block and "|" in stripped and stripped.startswith("|"):
            if table_start < 0:
                table_start = pos
        else:
            # 非表格行，结束之前的表格块
            if table_start >= 0:
                blocks.append((table_start, pos))
                table_start = -1

        pos = line_end
        i += 1

    # 收尾：未闭合的代码块或表格
    if in_code_block:
        blocks.append((code_block_start, len(text)))
    if table_start >= 0:
        blocks.append((table_start, len(text)))

    # 检查 max_chars 是否落在某个块内部
    cut_pos = max_chars
    for bstart, bend in blocks:
        if bstart < max_chars <= bend:
            # 落在块内，回退到块起始位置
            cut_pos = bstart
            break

    # 在 cut_pos 附近找换行符作为自然断点
    newline_pos = text.rfind("\n", 0, cut_pos)
    if newline_pos > cut_pos // 2:
        cut_pos = newline_pos

    return text[:cut_pos]


def contains_api_error(text: str) -> bool:
    """检测文本是否包含明确的 API 错误信息（从配置文件加载 patterns）

    只在短文本（<500字符）时检测，避免正常长回答中包含错误关键词导致误判。
    """
    if not text or len(text.strip()) > 500:
        return False
    error_patterns = [
        "apierror(", "badrequesterror(", "litellm.badrequesterror",
        "openaiexception", "datainspectionfailed",
        "content blocked", "内容审核未通过", "内容违规",
        '"error":{', '"code":"400"', '"code":"403"', '"code":"500"',
    ]
    text_lower = text.lower()
    return any(p.lower() in text_lower for p in error_patterns)
