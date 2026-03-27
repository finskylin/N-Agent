"""
Report Generator Skill
把已经整理好的分析内容、履历、研究结论生成为正式报告文件（md/html），返回可下载的文件链接。

输入（stdin JSON）:
  title, query, report_type, report_name, markdown, summary, sections,
  sources, confidence_level, confidence_reasoning, uncertainties, skip_png

输出（stdout JSON）:
  report_id, title, query, report_type, report_name, markdown, html,
  md_download_url, html_download_url, png_download_url, generated_at
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _slugify(value: str, default: str = "report") -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or default


def _gen_dir() -> Path:
    default = str(Path(os.getenv("APP_ROOT", "/app")) / "data" / "generated")
    d = Path(os.getenv("LOCAL_GENERATED_FILES_DIR", default))
    d.mkdir(parents=True, exist_ok=True)
    return d


# ──────────────────────────────────────────────
# Markdown 构建
# ──────────────────────────────────────────────

def _build_markdown(params: Dict[str, Any]) -> str:
    title = _safe_text(params.get("title"))
    query = _safe_text(params.get("query"))
    report_type = _safe_text(params.get("report_type")) or "analysis"
    summary = _safe_text(params.get("summary"))
    sections = _ensure_list(params.get("sections"))
    sources = _ensure_list(params.get("sources"))
    confidence_level = _safe_text(params.get("confidence_level"))
    confidence_reasoning = _safe_text(params.get("confidence_reasoning"))
    uncertainties = _ensure_list(params.get("uncertainties"))

    parts: List[str] = [f"# {title}", ""]
    if query:
        parts.append(f"> 查询主题：{query}")
    parts.append(f"> 报告类型：{report_type}")
    parts.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    parts.extend(["", "---", ""])

    if summary:
        parts.extend([summary, ""])

    for item in sections:
        if isinstance(item, dict):
            section_title = _safe_text(item.get("title")) or "未命名章节"
            content = _safe_text(item.get("content"))
            if section_title or content:
                parts.extend([f"## {section_title}", "", content, ""])
        elif isinstance(item, str) and item.strip():
            parts.extend([item.strip(), ""])

    # 可信度
    clean_unc = [_safe_text(u) for u in uncertainties if _safe_text(u)]
    if confidence_level or confidence_reasoning or clean_unc:
        parts.extend(["## 可信度评估", ""])
        if confidence_level:
            parts.append(f"- 可信度等级：{confidence_level}")
        if confidence_reasoning:
            parts.append(f"- 判断依据：{confidence_reasoning}")
        if clean_unc:
            parts.append("- 主要不确定性：")
            for u in clean_unc:
                parts.append(f"  - {u}")
        parts.append("")

    # 来源
    norm_sources = _normalize_sources(sources)
    if norm_sources:
        parts.extend(["## 参考来源", ""])
        for idx, s in enumerate(norm_sources, 1):
            name = s.get("name") or s.get("url") or f"来源 {idx}"
            url = s.get("url", "")
            suffix_parts = [s.get("author"), s.get("publication_date")]
            suffix_parts = [x for x in suffix_parts if x and x != "—"]
            suffix = f" — {' | '.join(suffix_parts)}" if suffix_parts else ""
            if url:
                parts.append(f"{idx}. [{name}]({url}){suffix}")
            else:
                parts.append(f"{idx}. {name}{suffix}")
        parts.append("")

    return "\n".join(parts).strip()


def _normalize_sources(sources: List[Any]) -> List[Dict[str, Any]]:
    result = []
    for item in sources:
        if isinstance(item, dict):
            name = _safe_text(item.get("name") or item.get("title"))
            url = _safe_text(item.get("url") or item.get("link"))
            author = _safe_text(item.get("author") or item.get("org"))
            date = _safe_text(item.get("publication_date") or item.get("date"))
            if name or url:
                result.append({
                    "name": name, "url": url,
                    "author": author or "—", "publication_date": date or "—",
                })
        elif isinstance(item, str) and item.strip():
            result.append({"name": item.strip(), "url": "", "author": "—", "publication_date": "—"})
    return result


# ──────────────────────────────────────────────
# HTML 渲染
# ──────────────────────────────────────────────

def _render_inline(text: str) -> str:
    escaped = _html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*(.+?)\*", r"<em>\1</em>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', escaped)
    return escaped


def _is_table_separator(line: str) -> bool:
    """判断是否是表格分隔行，如 |---|---|"""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    return bool(re.match(r"^\|[\s\-:|]+\|", stripped))


def _parse_table_row(line: str) -> List[str]:
    """解析表格行，返回单元格列表"""
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def _markdown_to_html(markdown: str) -> str:
    lines = markdown.splitlines()
    blocks: List[str] = []
    in_list = False
    in_table = False
    table_header_done = False

    def close_list():
        nonlocal in_list
        if in_list:
            blocks.append("</ul>")
            in_list = False

    def close_table():
        nonlocal in_table, table_header_done
        if in_table:
            blocks.append("</tbody></table>")
            in_table = False
            table_header_done = False

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        # 表格行
        if stripped.startswith("|"):
            close_list()
            if _is_table_separator(stripped):
                # 分隔行：标记 header 已完成
                table_header_done = True
                continue
            cells = _parse_table_row(stripped)
            if not in_table:
                # 第一行是表头
                blocks.append('<table><thead><tr>')
                blocks.append("".join(f"<th>{_render_inline(c)}</th>" for c in cells))
                blocks.append('</tr></thead><tbody>')
                in_table = True
                table_header_done = False
            elif not table_header_done:
                # 分隔行前的第二行（不常见，跳过）
                continue
            else:
                blocks.append("<tr>")
                blocks.append("".join(f"<td>{_render_inline(c)}</td>" for c in cells))
                blocks.append("</tr>")
            continue

        if not stripped:
            close_list()
            close_table()
            continue
        if stripped == "---":
            close_list()
            close_table()
            blocks.append("<hr />")
            continue
        if stripped.startswith("### "):
            close_list()
            close_table()
            blocks.append(f"<h3>{_html.escape(stripped[4:])}</h3>")
            continue
        if stripped.startswith("## "):
            close_list()
            close_table()
            blocks.append(f"<h2>{_html.escape(stripped[3:])}</h2>")
            continue
        if stripped.startswith("# "):
            close_list()
            close_table()
            blocks.append(f"<h1>{_html.escape(stripped[2:])}</h1>")
            continue
        if stripped.startswith("> "):
            close_list()
            close_table()
            blocks.append(f"<blockquote>{_render_inline(stripped[2:])}</blockquote>")
            continue
        if stripped.startswith("- "):
            close_table()
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            blocks.append(f"<li>{_render_inline(stripped[2:])}</li>")
            continue
        close_list()
        close_table()
        blocks.append(f"<p>{_render_inline(stripped)}</p>")

    close_list()
    close_table()
    return "\n".join(blocks)


def _render_html(title: str, query: str, report_type: str, markdown: str) -> str:
    safe_title = _html.escape(title)
    safe_query = _html.escape(query)
    safe_report_type = _html.escape(report_type)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 把 markdown 序列化为 JS 字符串（安全嵌入）
    md_json = json.dumps(markdown, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{safe_title}</title>
  <script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.3/dist/mermaid.min.js"></script>
  <style>
    :root {{
      --bg: #f5f1e8;
      --panel: #fffdf8;
      --text: #1f1f1f;
      --muted: #6b665c;
      --border: #d9d0bf;
      --accent: #9d5c2f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Georgia", "Songti SC", serif;
      color: var(--text);
      background: linear-gradient(180deg, #efe7d6 0%, var(--bg) 100%);
    }}
    .page {{
      max-width: 960px;
      margin: 28px auto;
      padding: 32px 40px;
      background: var(--panel);
      border: 1px solid var(--border);
      box-shadow: 0 18px 48px rgba(60,45,20,0.08);
      border-radius: 18px;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 24px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--border);
    }}
    h1 {{ margin: 0 0 10px; font-size: 34px; font-weight: 700; line-height: 1.2; }}
    h2 {{ font-size: 22px; font-weight: 700; margin: 28px 0 10px;
          padding-top: 16px; border-top: 1px solid var(--border); }}
    h3 {{ font-size: 17px; font-weight: 700; margin: 18px 0 8px; }}
    p {{ font-size: 15px; line-height: 1.8; margin: 8px 0; }}
    ul, ol {{ padding-left: 22px; }}
    li {{ font-size: 15px; line-height: 1.75; }}
    blockquote {{
      margin: 14px 0; padding: 10px 16px;
      border-left: 4px solid var(--accent);
      background: rgba(157,92,47,0.06);
      border-radius: 0 8px 8px 0;
      color: var(--muted);
    }}
    code {{
      padding: 2px 6px; border-radius: 5px;
      background: rgba(0,0,0,0.06);
      font-family: "SFMono-Regular", "Consolas", monospace;
      font-size: 13px;
    }}
    pre {{ background: #1e1e2e; border-radius: 10px; padding: 16px; overflow-x: auto; }}
    pre code {{ background: none; color: #cdd6f4; font-size: 13px; }}
    hr {{ border: none; border-top: 1px solid var(--border); margin: 22px 0; }}
    table {{
      width: 100%; border-collapse: collapse; margin: 18px 0; font-size: 14px;
    }}
    thead tr {{ background: var(--accent); color: #fff; }}
    thead th {{
      padding: 10px 14px; text-align: left;
      font-weight: 600; letter-spacing: 0.02em;
    }}
    tbody tr:nth-child(even) {{ background: rgba(157,92,47,0.05); }}
    tbody tr:hover {{ background: rgba(157,92,47,0.10); transition: background 0.15s; }}
    td {{ padding: 9px 14px; border-bottom: 1px solid var(--border); vertical-align: top; }}
    strong {{ font-weight: 700; }}
    em {{ font-style: italic; }}
    .mermaid {{ text-align: center; margin: 20px 0; }}
    .mermaid svg {{ max-width: 100%; }}
  </style>
</head>
<body>
  <article class="page">
    <h1>{safe_title}</h1>
    <div class="meta">主题：{safe_query} &nbsp;|&nbsp; 类型：{safe_report_type} &nbsp;|&nbsp; 生成时间：{generated_at}</div>
    <div id="content"></div>
  </article>
  <script>
    var md = {md_json};
    // 配置 marked：mermaid 代码块 → <div class="mermaid">
    marked.use({{
      renderer: {{
        code: function(token) {{
          var lang = (token && token.lang) || "";
          var text = (token && token.text) || (typeof token === "string" ? token : "");
          if (lang === "mermaid") {{
            return '<div class="mermaid">' + text + '</div>';
          }}
          return '<pre><code class="language-' + lang + '">' + text + '</code></pre>';
        }}
      }}
    }});
    // 防御奇数个 ``` 的截断问题
    if ((md.match(/```/g) || []).length % 2 !== 0) {{ md += '\\n```\\n'; }}
    document.getElementById('content').innerHTML = marked.parse(md);

    // 初始化 Mermaid 10.x（仅渲染 flowchart/sequenceDiagram/gantt/pie/classDiagram/stateDiagram/erDiagram）
    mermaid.initialize({{ startOnLoad: false, theme: 'default', securityLevel: 'loose' }});
    var blocks = document.querySelectorAll('.mermaid');
    blocks.forEach(function(block, idx) {{
      var src = block.textContent || block.innerText;
      mermaid.render('mermaid-svg-' + idx, src).then(function(r) {{
        block.innerHTML = r.svg;
      }}).catch(function(err) {{
        block.innerHTML = '<pre style="padding:12px;background:#f8f9fc;border-radius:8px;font-size:12px;color:#64748b;white-space:pre-wrap">' + src.replace(/</g,'&lt;') + '</pre>';
      }});
    }});
  </script>
</body>
</html>"""


# ──────────────────────────────────────────────
# 主逻辑
# ──────────────────────────────────────────────

def execute(params: Dict[str, Any]) -> Dict[str, Any]:
    title = _safe_text(params.get("title"))
    query = _safe_text(params.get("query", ""))
    report_type = _safe_text(params.get("report_type")) or "analysis"
    report_name = _slugify(
        _safe_text(params.get("report_name")) or title or report_type,
        default="report",
    )

    if not title:
        return {"error": "缺少 title 参数"}

    markdown = _safe_text(params.get("markdown"))
    if not markdown:
        markdown = _build_markdown(params)

    if not markdown:
        return {"error": "缺少 markdown/summary/sections，无法生成报告"}

    html_content = _render_html(
        title=title,
        query=query or title,
        report_type=report_type,
        markdown=markdown,
    )

    report_id = _safe_text(params.get("report_id")) or str(uuid.uuid4())[:12]
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    skip_png = params.get("skip_png", False)

    md_file_path, html_file_path, png_file_path = "", "", ""
    try:
        gen_dir = _gen_dir()
        safe_name = f"{report_name}_{report_id}"

        # 写 .md
        _md = gen_dir / f"{safe_name}.md"
        _md.write_text(markdown, encoding="utf-8")
        md_file_path = str(_md)

        # 写 .html
        _html = gen_dir / f"{safe_name}.html"
        _html.write_text(html_content, encoding="utf-8")
        html_file_path = str(_html)

        # 截图 .png（可选）
        if not skip_png:
            try:
                from playwright.sync_api import sync_playwright
                _png = gen_dir / f"{safe_name}.png"
                with sync_playwright() as pw:
                    browser = pw.chromium.launch()
                    page = browser.new_page(viewport={"width": 1200, "height": 800})
                    page.goto(f"file://{_html}", wait_until="networkidle")
                    # Wait for mermaid async rendering (mermaid.render returns Promise)
                    page.wait_for_timeout(2500)
                    page.screenshot(path=str(_png), full_page=True)
                    browser.close()
                png_file_path = str(_png)
            except Exception as e:
                print(f"[ReportGenerator] PNG screenshot skipped: {e}", file=sys.stderr)

    except Exception as e:
        print(f"[ReportGenerator] File save error: {e}", file=sys.stderr)

    return {
        "report_id": report_id,
        "title": title,
        "query": query,
        "report_type": report_type,
        "report_name": report_name,
        "markdown": markdown,
        "html": html_content,
        "md_file_path": md_file_path,
        "html_file_path": html_file_path,
        "png_file_path": png_file_path,
        "content_type": "report",
        "generated_at": generated_at,
    }


# ──────────────────────────────────────────────
# 入口（stdin JSON → stdout JSON）
# ──────────────────────────────────────────────

if __name__ == "__main__":
    params: Dict[str, Any] = {}

    # 1. 从 stdin 读取 JSON
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read().strip()
            if raw:
                params = json.loads(raw)
        except Exception as e:
            print(json.dumps({"error": f"stdin JSON parse error: {e}"}))
            sys.exit(1)

    # 2. 命令行参数补充（方便直接测试）
    import argparse
    parser = argparse.ArgumentParser(description="ReportGenerator skill")
    parser.add_argument("--title", type=str)
    parser.add_argument("--query", type=str)
    parser.add_argument("--report-type", type=str, dest="report_type")
    parser.add_argument("--report-name", type=str, dest="report_name")
    parser.add_argument("--markdown", type=str)
    parser.add_argument("--summary", type=str)
    parser.add_argument("--skip-png", action="store_true", dest="skip_png")
    args = parser.parse_args()
    for key, value in vars(args).items():
        if value is not None and (not isinstance(value, bool) or value):
            params[key] = value

    result = execute(params)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
