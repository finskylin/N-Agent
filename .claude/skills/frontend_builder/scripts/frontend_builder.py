"""
Frontend Builder Skill — LLM 驱动的前端组件生成器

当 Layer 1 (for_ui.components) 和 Layer 2 (SKILL.md data_hints) 均未匹配到
合适的 Vue 组件时，使用 LLM 根据数据结构动态生成 HTML 可视化页面，
输出为 html_render 组件（前端通过 DynamicRenderer.vue 以 iframe srcdoc 渲染）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, TYPE_CHECKING

from loguru import logger

# SkillContext / SkillResult / SkillStatus are framework types injected at runtime;
# the subprocess entry point passes a plain dict instead.
# Use TYPE_CHECKING guard so annotations don't cause NameError at import time.
if TYPE_CHECKING:
    SkillContext = Any
    SkillResult = Any
    SkillStatus = Any


class FrontendBuilderSkill:
    name = "frontend_builder"
    description = (
        "动态生成前端可视化页面。当数据没有匹配的预定义组件时，"
        "使用 LLM 根据数据结构实时生成 HTML 页面进行可视化展示。"
    )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "data": {"type": "object", "description": "需要可视化的数据"},
                "description": {"type": "string", "description": "数据描述/可视化需求"},
            },
        }

    async def execute(self, context) -> dict:
        """LLM 驱动的 HTML 生成（subprocess 模式：context 为 plain dict）"""
        # 兼容 dict（subprocess）和 SkillContext 对象两种调用方式
        if isinstance(context, dict):
            params = context
        else:
            params = getattr(context, "params", context)

        data = params.get("data", {})
        description = params.get("description", "") or params.get("requirement", "") or ""

        if not data:
            return {"error": "No data provided"}

        try:
            html_code = await self._generate_html(data, description)
            if not html_code:
                return {"error": "HTML generation failed (LLM and fallback both failed)"}

            return {
                "for_ui": {
                    "components": [
                        {
                            "component": "html_render",
                            "data": {
                                "html": html_code,
                                "title": description or "数据可视化",
                            },
                        }
                    ]
                }
            }
        except Exception as e:
            logger.error(f"[FrontendBuilder] Generation failed: {e}")
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # HTML generation pipeline
    # ------------------------------------------------------------------

    async def _generate_html(self, data: dict, description: str) -> Optional[str]:
        """Call LLM to generate HTML visualization, with fallback."""
        # 1. Build data summary for LLM
        data_summary = self._summarize_data_structure(data)

        # 2. Load available JS libraries text from config
        js_libraries = self._load_js_libraries_text()

        # 3. Load system prompt from config/prompts/frontend_builder_system.md
        system_prompt = self._load_system_prompt(js_libraries)
        if not system_prompt:
            logger.warning("[FrontendBuilder] No system prompt found, using fallback")
            return self._generate_fallback_html(data, description)

        # 4. Build user prompt
        data_json = json.dumps(data, ensure_ascii=False, default=str)
        if len(data_json) > 8000:
            data_json = data_json[:8000] + "... (truncated)"

        user_prompt = (
            f"数据描述：{description or '根据数据自动生成可视化展示'}\n"
            f"数据结构：{data_summary}\n"
            f"完整数据：{data_json}"
        )

        # 5. Call LLM
        html_code = await self._call_llm_for_html(system_prompt, user_prompt)
        if html_code:
            logger.info(f"[FrontendBuilder] LLM generated HTML ({len(html_code)} chars)")
            return html_code

        # 6. Fallback: generate simple HTML without LLM
        logger.info("[FrontendBuilder] LLM failed, using fallback HTML generator")
        return self._generate_fallback_html(data, description)

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    async def _call_llm_for_html(self, system: str, user_prompt: str) -> Optional[str]:
        """Lightweight LLM call for HTML generation (direct HTTP, no agent_core deps)."""
        import httpx
        import os
        try:
            base_url = (os.getenv("ANTHROPIC_BASE_URL") or os.getenv("LLM_BASE_URL") or "").rstrip("/")
            api_key = os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("LLM_API_KEY") or ""
            model = os.getenv("ANTHROPIC_SMALL_FAST_MODEL") or os.getenv("ANTHROPIC_MODEL") or ""
            if not base_url or not api_key or not model:
                logger.warning("[FrontendBuilder] LLM env vars not configured")
                return None
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "max_tokens": 4096,
                        "temperature": 0.2,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"]
                if text:
                    return self._extract_html(text)
                return None
        except Exception as e:
            logger.warning(f"[FrontendBuilder] LLM call failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_html(text: str) -> Optional[str]:
        """Extract HTML from LLM response, stripping markdown code blocks."""
        text = text.strip()
        # Try extracting from ```html ... ``` block
        match = re.search(
            r"```(?:html)?\s*(<!DOCTYPE.*?</html>)\s*```",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()
        # If text starts with <!DOCTYPE or <html, use directly
        lower = text.lower()
        if lower.startswith("<!doctype") or lower.startswith("<html"):
            return text
        return None

    @staticmethod
    def _summarize_data_structure(data: Any) -> str:
        """Generate a data structure summary for LLM."""
        if isinstance(data, dict):
            summary: Dict[str, str] = {}
            for k, v in list(data.items())[:30]:
                if isinstance(v, list):
                    if v and isinstance(v[0], dict):
                        summary[k] = f"list[{len(v)}] of dicts, keys={list(v[0].keys())[:10]}"
                    else:
                        summary[k] = f"list[{len(v)}]"
                elif isinstance(v, dict):
                    summary[k] = f"dict, keys={list(v.keys())[:10]}"
                else:
                    summary[k] = type(v).__name__
            return json.dumps(summary, ensure_ascii=False)
        return str(type(data).__name__)

    @staticmethod
    def _load_js_libraries_text() -> str:
        """Load JS libraries list from app/config/ui/js_preloads.json."""
        try:
            config_path = Path(__file__).resolve().parents[4] / "app" / "config" / "ui" / "js_preloads.json"
            if config_path.exists():
                config = json.loads(config_path.read_text(encoding="utf-8"))
                libs = config.get("libraries", [])
                return "\n".join(
                    f"- {lib['name']} ({lib.get('version', '')}) — {lib.get('usage', '')}"
                    for lib in libs
                )
        except Exception as e:
            logger.debug(f"[FrontendBuilder] Failed to load JS preloads config: {e}")
        return "- ECharts 5, D3.js 7, Chart.js 4, Leaflet 1.9.4"

    @staticmethod
    def _load_system_prompt(js_libraries: str) -> str:
        """Load and format the system prompt (no app dependency)."""
        import os
        from pathlib import Path
        try:
            # Search config/prompts/ and app/prompts/ relative to service root
            script_dir = Path(__file__).parent
            service_root = script_dir.parents[3]  # skills/frontend_builder/scripts/ -> service_root
            candidates = [
                service_root / "app" / "prompts" / "frontend_builder_system.md",
                service_root / "config" / "prompts" / "frontend_builder_system.md",
            ]
            for candidate in candidates:
                if candidate.exists():
                    text = candidate.read_text(encoding="utf-8")
                    return text.replace("{js_libraries}", js_libraries)
        except Exception as e:
            logger.debug(f"[FrontendBuilder] Failed to load system prompt: {e}")
        return ""

    # ------------------------------------------------------------------
    # Fallback: pure-Python HTML generator (no LLM required)
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_fallback_html(data: dict, description: str) -> str:
        """Generate simple HTML table/cards without LLM (fallback)."""
        title = description or "数据可视化"
        data_json = json.dumps(data, ensure_ascii=False, default=str)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 0; padding: 16px; background: #f8f9fc; color: #1e293b; }}
.title {{ font-size: 18px; font-weight: 700; margin-bottom: 16px; }}
.card {{ background: #fff; border-radius: 12px; padding: 16px; margin-bottom: 12px; box-shadow: 0 4px 6px -1px rgba(15,23,42,0.06); }}
.card h3 {{ font-size: 14px; color: #64748b; margin: 0 0 8px; }}
.card .value {{ font-size: 16px; font-weight: 600; }}
table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(15,23,42,0.06); }}
th {{ background: #f1f5f9; text-align: left; padding: 10px 12px; font-size: 13px; font-weight: 600; color: #475569; }}
td {{ padding: 8px 12px; border-top: 1px solid #e2e8f0; font-size: 13px; }}
tr:hover td {{ background: #f8fafc; }}
</style>
</head>
<body>
<div class="title">{title}</div>
<div id="root"></div>
<script>
window.__RENDER_DATA__ = {data_json};
var data = window.__RENDER_DATA__;
var root = document.getElementById("root");

function renderCards(obj) {{
    Object.keys(obj).forEach(function(k) {{
        var v = obj[k];
        if (typeof v === "object" && v !== null && !Array.isArray(v)) return;
        if (Array.isArray(v)) return;
        var card = document.createElement("div");
        card.className = "card";
        card.innerHTML = "<h3>" + k + "</h3><div class=\\"value\\">" + String(v) + "</div>";
        root.appendChild(card);
    }});
}}

function renderTable(arr) {{
    if (!arr.length) return;
    var keys = Object.keys(arr[0]);
    var html = "<table><thead><tr>" + keys.map(function(k) {{ return "<th>" + k + "</th>"; }}).join("") + "</tr></thead><tbody>";
    arr.slice(0, 50).forEach(function(row) {{
        html += "<tr>" + keys.map(function(k) {{ return "<td>" + (row[k] != null ? String(row[k]) : "") + "</td>"; }}).join("") + "</tr>";
    }});
    html += "</tbody></table>";
    root.innerHTML += html;
}}

if (Array.isArray(data)) {{
    renderTable(data);
}} else if (typeof data === "object") {{
    var rendered = false;
    Object.keys(data).forEach(function(k) {{
        if (Array.isArray(data[k]) && data[k].length > 0 && typeof data[k][0] === "object") {{
            var h = document.createElement("h3");
            h.textContent = k;
            h.style.cssText = "margin: 16px 0 8px; font-size: 15px; color: #334155;";
            root.appendChild(h);
            var div = document.createElement("div");
            div.id = "table-" + k;
            root.appendChild(div);
            var tkeys = Object.keys(data[k][0]);
            var thtml = "<table><thead><tr>" + tkeys.map(function(tk) {{ return "<th>" + tk + "</th>"; }}).join("") + "</tr></thead><tbody>";
            data[k].slice(0, 50).forEach(function(row) {{
                thtml += "<tr>" + tkeys.map(function(tk) {{ return "<td>" + (row[tk] != null ? String(row[tk]) : "") + "</td>"; }}).join("") + "</tr>";
            }});
            thtml += "</tbody></table>";
            div.innerHTML = thtml;
            rendered = true;
        }}
    }});
    renderCards(data);
}}
</script>
</body>
</html>"""


def _main():
    """直接执行入口: python3 script.py --param1 value1
    也支持 JSON stdin: echo '{"param1": "v1"}' | python3 script.py
    """
    import argparse
    import asyncio
    import json
    import sys

    params = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read().strip()
            if raw:
                params = json.loads(raw)
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Run FrontendBuilderSkill directly")
    parser.add_argument("--data", type=str, dest="data")
    parser.add_argument("--description", type=str, dest="description")
    args = parser.parse_args()
    for k, v in vars(args).items():
        if v is not None:
            if k == "data":
                try:
                    params[k] = json.loads(v)
                except Exception:
                    params[k] = v
            else:
                params[k] = v

    async def run():
        skill = FrontendBuilderSkill()
        result = await skill.execute(params)
        out = result if isinstance(result, dict) else {"data": str(result)}
        print(json.dumps(out, ensure_ascii=False, default=str, indent=2))

    asyncio.run(run())


if __name__ == "__main__":
    _main()
