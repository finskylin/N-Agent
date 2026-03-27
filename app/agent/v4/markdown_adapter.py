"""
Markdown 格式适配器

根据不同渠道（web/dingtalk/api）转换 Markdown 格式
钉钉 Markdown 限制：
- 不支持表格
- 不支持代码块
- 不支持复杂嵌套
"""

import re
from typing import Optional


def adapt_markdown_for_channel(content: str, channel: str) -> str:
    """
    根据渠道适配 Markdown 格式

    Args:
        content: 原始 Markdown 内容
        channel: 渠道标识 (web | dingtalk | api)

    Returns:
        适配后的 Markdown 内容
    """
    if channel == "dingtalk":
        return _adapt_for_dingtalk(content)
    if channel == "feishu":
        # 飞书 post 格式支持完整 Markdown，无需转换
        return content
    # web 和 api 渠道保持原样
    return content


def _adapt_for_dingtalk(content: str) -> str:
    """
    适配钉钉 Markdown 格式

    钉钉支持：
    - 标题 # ## ###
    - 加粗 **text**
    - 链接 [text](url)
    - 图片 ![](url)
    - 无序列表 - item
    - 有序列表 1. item
    - 引用 > text

    钉钉不支持（需要转换）：
    - 代码块 -> 引用
    - 分隔线 --- -> 空行
    - mermaid 图表 -> 移除或简化
    - LaTeX 数学公式 -> Unicode 文本

    注意：表格保留原样，sampleMarkdown 在 PC 端支持表格渲染。
    """
    result = content

    # 0. 清理 LaTeX 数学公式（钉钉不支持）
    result = _clean_latex(result)

    # 1. 转换 mermaid 图表为文字说明
    result = _convert_mermaid(result)

    # 2. 转换代码块为引用格式
    result = _convert_code_blocks(result)

    # 3. 表格保留原样（sampleMarkdown PC 端支持表格渲染）
    # result = _convert_tables(result)

    # 4. 简化分隔线
    result = _simplify_separators(result)

    # 5. 规范化标题层级（钉钉最多支持 3 级）
    result = _normalize_headers(result)

    # 6. 清理多余空行
    result = _clean_empty_lines(result)

    return result


def _clean_latex(content: str) -> str:
    """清理 LaTeX 数学公式，转为纯文本（钉钉不支持 LaTeX 渲染）"""

    # 常见 LaTeX 箭头/符号 → Unicode 文本
    _LATEX_SYMBOL_MAP = {
        r"\rightarrow": "\u2192",
        r"\leftarrow": "\u2190",
        r"\Rightarrow": "\u21d2",
        r"\Leftarrow": "\u21d0",
        r"\leftrightarrow": "\u2194",
        r"\uparrow": "\u2191",
        r"\downarrow": "\u2193",
        r"\times": "\u00d7",
        r"\div": "\u00f7",
        r"\pm": "\u00b1",
        r"\leq": "\u2264",
        r"\geq": "\u2265",
        r"\neq": "\u2260",
        r"\approx": "\u2248",
        r"\infty": "\u221e",
        r"\sum": "\u2211",
        r"\prod": "\u220f",
        r"\alpha": "\u03b1",
        r"\beta": "\u03b2",
        r"\gamma": "\u03b3",
        r"\delta": "\u03b4",
    }

    result = content

    def _replace_formula(formula: str) -> str:
        """将单个 LaTeX 公式内容替换为纯文本"""
        for latex, text in _LATEX_SYMBOL_MAP.items():
            formula = formula.replace(latex, text)
        # 去除剩余 LaTeX 命令
        formula = re.sub(r'\\[a-zA-Z]+', '', formula)
        # 清理花括号
        formula = formula.replace('{', '').replace('}', '').strip()
        return formula

    # 块级公式 $$...$$ → 引用格式（先处理块级，避免行内匹配干扰）
    def replace_block_math(match):
        formula = match.group(1).strip()
        return f"> {_replace_formula(formula)}"

    result = re.sub(r'\$\$([\s\S]*?)\$\$', replace_block_math, result)

    # 行内公式 $...$ （非贪婪，排除 $$）
    def replace_inline_math(match):
        formula = match.group(1)
        return _replace_formula(formula)

    result = re.sub(r'(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)', replace_inline_math, result)

    return result


def _convert_mermaid(content: str) -> str:
    """将 mermaid 图表转换为简单文字说明"""
    # 匹配 ```mermaid ... ```
    pattern = r'```mermaid\s*([\s\S]*?)```'

    def replace_mermaid(match):
        mermaid_content = match.group(1).strip()

        # flowchart / graph 类型
        if 'flowchart' in mermaid_content or 'graph' in mermaid_content:
            return _mermaid_flowchart_to_text(mermaid_content)

        # timeline 类型
        if mermaid_content.lstrip().startswith('timeline'):
            return _mermaid_timeline_to_text(mermaid_content)

        # 其他类型（pie, sequence, gantt 等）
        return "> *[图表已省略，请查看完整报告]*"

    return re.sub(pattern, replace_mermaid, content)


def _mermaid_flowchart_to_text(mermaid_content: str) -> str:
    """将 flowchart/graph mermaid 转换为结构化文字"""
    lines = mermaid_content.split('\n')
    sections = []       # 收集 subgraph 标题
    nodes = []          # 收集节点文本

    for line in lines:
        stripped = line.strip()

        # 跳过空行和 flowchart/graph/end 声明
        if not stripped or stripped.startswith('flowchart') or stripped.startswith('graph') or stripped == 'end':
            continue

        # subgraph 标题
        sg_match = re.match(r'subgraph\s+(.+)', stripped)
        if sg_match:
            title = sg_match.group(1).strip()
            sections.append(title)
            continue

        # 提取节点内容：A[文本] 或 A(文本) 或 A{文本} 等
        node_texts = re.findall(r'[\w]+[\[\(\{]([^\]\)\}]+)[\]\)\}]', stripped)
        for text in node_texts:
            # 清理 <br/> 标签，替换为空格
            clean = re.sub(r'<br\s*/?>',  ' ', text).strip()
            if clean and clean not in nodes:
                nodes.append(clean)

    result_parts = []

    if sections:
        result_parts.append("> **关键区域**: " + " | ".join(sections))

    if nodes:
        # 最多展示 8 个节点，避免过长
        display_nodes = nodes[:8]
        result_parts.append("> **流程**: " + " → ".join(display_nodes))
        if len(nodes) > 8:
            result_parts.append(f"> *(共 {len(nodes)} 个节点，更多请查看完整报告)*")

    if not result_parts:
        return "> *[流程图已省略，请查看完整报告]*"

    return "\n".join(result_parts)


def _mermaid_timeline_to_text(mermaid_content: str) -> str:
    """将 timeline mermaid 转换为时间线文字"""
    lines = mermaid_content.split('\n')
    title = ""
    events = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped == 'timeline':
            continue

        # title 行
        title_match = re.match(r'title\s+(.+)', stripped)
        if title_match:
            title = title_match.group(1).strip()
            continue

        # section 行
        section_match = re.match(r'section\s+(.+)', stripped)
        if section_match:
            events.append(f"**{section_match.group(1).strip()}**")
            continue

        # 普通事件行（通常缩进，格式为 "日期 : 事件"）
        event_match = re.match(r'(.+?)\s*:\s*(.+)', stripped)
        if event_match:
            events.append(f"{event_match.group(1).strip()} - {event_match.group(2).strip()}")
        elif stripped and not stripped.startswith('%%'):
            events.append(stripped)

    result_parts = []
    if title:
        result_parts.append(f"> **{title}**")

    if events:
        # 最多展示 10 个事件
        for ev in events[:10]:
            result_parts.append(f"> - {ev}")
        if len(events) > 10:
            result_parts.append(f"> *(共 {len(events)} 个事件，更多请查看完整报告)*")

    if not result_parts:
        return "> *[时间线已省略，请查看完整报告]*"

    return "\n".join(result_parts)


def _convert_code_blocks(content: str) -> str:
    """将代码块转换为引用格式"""
    # 匹配 ```language ... ```
    pattern = r'```(\w*)\s*([\s\S]*?)```'

    def replace_code(match):
        language = match.group(1)
        code = match.group(2).strip()

        # 如果是 JSON 且很长，截断
        if len(code) > 500:
            code = code[:500] + "\n... (内容已截断)"

        # 转换为引用格式，每行加 >
        lines = code.split('\n')
        quoted_lines = ['> ' + line for line in lines[:20]]  # 最多 20 行

        if len(lines) > 20:
            quoted_lines.append('> ... (更多内容已省略)')

        lang_hint = f"**[{language}]**\n" if language else ""
        return lang_hint + '\n'.join(quoted_lines)

    return re.sub(pattern, replace_code, content)


def _convert_tables(content: str) -> str:
    """将 Markdown 表格转换为列表格式"""
    lines = content.split('\n')
    result_lines = []
    in_table = False
    table_lines = []

    for line in lines:
        # 检测表格行（以 | 开头或包含多个 |）
        is_table_line = line.strip().startswith('|') or (line.count('|') >= 2 and '|' in line.strip())
        is_separator = bool(re.match(r'^[\s|:-]+$', line))  # 表格分隔行 |---|---|

        if is_table_line and not is_separator:
            in_table = True
            table_lines.append(line)
        elif in_table and is_separator:
            # 跳过分隔行
            continue
        else:
            # 表格结束，转换
            if table_lines:
                result_lines.extend(_table_to_list(table_lines))
                table_lines = []
                in_table = False
            result_lines.append(line)

    # 处理末尾的表格
    if table_lines:
        result_lines.extend(_table_to_list(table_lines))

    return '\n'.join(result_lines)


def _table_to_list(table_lines: list) -> list:
    """将表格行转换为列表

    智能检测表格类型：
    - key-value 型（第一列值各不相同，像字段标签）：用第一列值作 key
    - 普通数据表：用表头名作前缀
    """
    if not table_lines:
        return []

    result = []
    headers = []
    data_rows = []

    for i, line in enumerate(table_lines):
        cells = [c.strip() for c in line.split('|') if c.strip()]
        if i == 0:
            headers = cells
        else:
            data_rows.append(cells)

    if not headers or not data_rows:
        return []

    # 检测是否为 key-value 型表格：
    # 条件：至少 2 列，第一列的值互不相同（看起来像字段标签而不是数据）
    is_kv_table = False
    if len(headers) >= 2 and len(data_rows) >= 2:
        first_col_values = [r[0] for r in data_rows if len(r) > 0]
        if len(first_col_values) == len(set(first_col_values)):
            is_kv_table = True

    for cells in data_rows:
        if not cells:
            continue

        if is_kv_table and len(cells) >= 2:
            # key-value 型：第一列作 key，其余列拼为 value
            key = cells[0]
            # 过滤空值，用逗号或括号拼接
            remaining = [c for c in cells[1:] if c]
            if len(remaining) >= 2:
                # 第二列是主值，后续列作为补充（如来源）
                value = remaining[0] + "（" + "，".join(remaining[1:]) + "）"
            elif remaining:
                value = remaining[0]
            else:
                value = ""
            result.append(f"- **{key}**: {value}")
        elif headers and len(cells) == len(headers):
            # 普通数据表：用表头作前缀
            parts = [f"**{headers[j]}**: {cells[j]}" for j in range(len(cells)) if cells[j]]
            if parts:
                result.append("- " + " | ".join(parts))
        else:
            result.append("- " + " | ".join(cells))

    return result


def _simplify_separators(content: str) -> str:
    """简化分隔线"""
    # 将 --- 或 *** 或 ___ 替换为空行
    content = re.sub(r'^[\s]*[-*_]{3,}[\s]*$', '\n', content, flags=re.MULTILINE)
    return content


def _normalize_headers(content: str) -> str:
    """
    规范化标题层级：
    - #### 及更深层级统一为 ###（钉钉最多 3 级）
    - 确保 # 后有空格
    """
    def fix_header(match):
        hashes = match.group(1)
        text = match.group(2)
        # 最多 3 级
        level = min(len(hashes), 3)
        return '#' * level + ' ' + text

    content = re.sub(r'^(#{1,6})\s*(.*?)$', fix_header, content, flags=re.MULTILINE)
    return content


def _clean_empty_lines(content: str) -> str:
    """清理多余空行（最多保留 1 个空行 = 2 个换行）"""
    content = re.sub(r'\n{3,}', '\n\n', content)
    return content.strip()


# === 测试 ===
if __name__ == "__main__":
    test_content = """
# 测试报告

## 数据表格

| 名称 | 数值 | 说明 |
|------|------|------|
| 指标A | 100 | 正常 |
| 指标B | 200 | 偏高 |

## 代码示例

```python
def hello():
    print("Hello, World!")
```

## 流程图

```mermaid
flowchart LR
    A[开始] --> B[处理]
    B --> C[结束]
```

---

完成！
"""

    print("=== 原始内容 ===")
    print(test_content)
    print("\n=== 钉钉适配后 ===")
    print(_adapt_for_dingtalk(test_content))
