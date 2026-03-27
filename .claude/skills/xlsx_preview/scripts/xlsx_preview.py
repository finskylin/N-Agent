"""
XLSX Preview Skill
Excel 表格预览技能 - 将 xlsx 文件渲染到数据看板
"""

# Imports

# ── file_storage helpers (no app.services dependency) ────────────────────────
from typing import Any, Dict
import os as _os
import uuid as _uuid
from pathlib import Path as _Path
from urllib.parse import quote as _quote, urlparse as _urlparse, unquote as _unquote

def _get_public_base_url() -> str:
    base = _os.getenv("AGENT_PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        return base
    host = _os.getenv("AGENT_EXTERNAL_HOST", "127.0.0.1")
    port = _os.getenv("AGENT_SERVICE_PORT", "8000")
    return f"http://{host}:{port}"

def _get_generated_files_dir() -> _Path:
    configured = _os.getenv("LOCAL_GENERATED_FILES_DIR", "").strip()
    return _Path(configured) if configured else _Path("app/data/generated")

def _ensure_download_url(file_path: str, download_url: str = "") -> str:
    if download_url:
        return download_url
    if not file_path or not _os.path.exists(file_path):
        return ""
    file_name = _os.path.basename(file_path)
    return f"{_get_public_base_url()}/api/files/download/{file_name}"

def _download_remote_file(file_url: str, allowed_extensions: list = None) -> dict:
    import requests
    if not file_url or not file_url.startswith(("http://", "https://")):
        raise ValueError(f"无效的文件 URL: {file_url}")
    parsed = _urlparse(file_url)
    url_path = _unquote(parsed.path)
    original_name = url_path.split("/")[-1] if "/" in url_path else "downloaded_file"
    ext = ("." + original_name.rsplit(".", 1)[-1].lower()) if "." in original_name else ""
    if allowed_extensions and ext not in allowed_extensions:
        raise ValueError(f"不支持的文件类型: {ext}，允许: {allowed_extensions}")
    short_id = str(_uuid.uuid4())[:8]
    local_name = f"{original_name.rsplit('.', 1)[0]}_{short_id}{ext}" if ext else f"{original_name}_{short_id}"
    save_dir = _get_generated_files_dir()
    save_dir.mkdir(parents=True, exist_ok=True)
    local_path = save_dir / local_name
    resp = requests.get(file_url, timeout=60, stream=True)
    resp.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    download_url = f"{_get_public_base_url()}/api/files/download/{local_name}"
    return {"file_path": str(local_path), "download_url": download_url, "file_name": local_name}
# ──────────────────────────────────────────────────────────────────────────────


# ── 兼容层：SkillResult / SkillStatus（老架构接口，保持向后兼容）──
class _SkillStatus:
    SUCCESS = "success"
    ERROR = "error"
    PARTIAL = "partial"

class _SkillResult(dict):
    """轻量兼容类：SkillResult(status=..., data=..., error=...) 直接作为 dict 使用"""
    def __init__(self, status=None, data=None, error=None, **kwargs):
        d = {}
        if status is not None:
            d["status"] = status
        if data is not None:
            if isinstance(data, dict):
                d.update(data)
            else:
                d["data"] = data
        if error is not None:
            d["error"] = error
        d.update(kwargs)
        super().__init__(d)

SkillResult = _SkillResult
SkillStatus = _SkillStatus
# ────────────────────────────────────────────────────────────────────────────

class XlsxPreviewSkill:
    """Excel 表格生成与预览技能"""

    @property
    def name(self) -> str:
        return "xlsx_preview"

    @property
    def description(self) -> str:
        return "生成或预览 Excel 表格 (.xlsx)。支持从数据生成表格，或预览现有表格，提供下载链接和数据预览。"

    @property
    def category(self) -> str:
        return "office_preview"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "file_path": {"type": "string", "required": False, "description": "xlsx 文件路径 (预览模式)"},
            "data": {
                "type": "object", 
                "required": False, 
                "description": "用于生成表格的数据 (生成模式)。格式: {'sheets': {'Name': [rows]}} OR {'data': [rows]}"
            },
            "file_url": {"type": "string", "required": False, "description": "xlsx 文件 URL"},
            "sheet_name": {"type": "string", "required": False, "description": "指定工作表名称"},
            "max_rows": {"type": "integer", "default": 100, "description": "最大预览行数"}
        }

    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "file_name": "string",
            "file_size": "integer",
            "file_data_base64": "base64 encoded file content",
            "sheet_names": "list of sheet names",
            "sheets_data": "dict with sheet data preview",
            "download_url": "string (optional)"
        }

    async def _extract_sheets_data(self, file_path: str, max_rows: int = 100) -> Dict[str, Any]:
        """提取所有工作表数据"""
        try:
            import pandas as pd

            # 读取所有工作表
            excel_file = pd.ExcelFile(file_path)
            sheets_data = {}

            for sheet_name in excel_file.sheet_names:
                df = pd.read_excel(excel_file, sheet_name=sheet_name, nrows=max_rows)
                
                # Handle NaN/Inf for JSON serialization
                df = df.where(pd.notnull(df), None)

                # 转换为字典格式
                sheets_data[sheet_name] = {
                    "columns": df.columns.tolist(),
                    "rows": df.to_dict('records'),  # 每行作为字典
                    "row_count": len(df),
                    "column_count": len(df.columns)
                }

            return sheets_data

        except Exception as e:
            print(f"[XlsxPreview] Data extraction failed: {e}")
            return {}

    async def _read_file_as_base64(self, file_path: str) -> tuple[str, int]:
        """读取文件并转换为 base64"""
        try:
            with open(file_path, 'rb') as f:
                file_data = f.read()
                base64_data = base64.b64encode(file_data).decode('utf-8')
                return base64_data, os.path.getsize(file_path)
        except Exception as e:
            print(f"[XlsxPreview] Failed to read file: {e}")
            return "", 0

    def _try_fix_json(self, json_str: str) -> Dict[str, Any] | None:
        """
        尝试修复常见的 LLM 生成 JSON 错误
        1. 缺少引号的值 (如 2020年总统选举 -> "2020年总统选举")
        2. 单引号替换为双引号
        3. 尾随逗号
        """
        import re

        try:
            # 尝试 1: 直接解析
            return json.loads(json_str)
        except:
            pass

        fixed = json_str

        try:
            # 修复 1: 单引号替换为双引号
            fixed = fixed.replace("'", '"')

            # 修复 2: 移除尾随逗号 (在 ] 或 } 前的逗号)
            fixed = re.sub(r',\s*([}\]])', r'\1', fixed)

            # 修复 3: 尝试修复缺少引号的中文值
            # 匹配模式: ", 非引号开头的中文内容, " 或 ", 非引号开头的中文内容]
            # 例如: ", 2020年总统选举民进党提名" -> ", "2020年总统选举民进党提名"
            def fix_unquoted_value(match):
                prefix = match.group(1)  # ", " 或 "[ "
                value = match.group(2)   # 未加引号的值
                suffix = match.group(3)  # ", " 或 "]" 或 "}"
                return f'{prefix}"{value}"{suffix}'

            # 匹配 ", 未引号的值," 或 "[ 未引号的值," 等模式
            fixed = re.sub(
                r'([\[,]\s*)([^"\[\]{},]+?)(\s*[,\]\}])',
                fix_unquoted_value,
                fixed
            )

            return json.loads(fixed)
        except Exception as e:
            print(f"[XlsxPreview] JSON auto-fix failed: {e}")
            return None

    async def execute(self, context: dict) -> dict:
        """执行 xlsx 生成或预览"""
        import json

        try:
            # Prevent NoneType error if params is None
            if context is None:
                context = context or {}

            file_path = context.get("file_path", "")
            file_url = context.get("file_url", "")
            data_input = context.get("data", None)
            sheet_name = context.get("sheet_name", "")
            max_rows = context.get("max_rows", 100)
            download_url = ""

            # 模式 1: 生成模式
            if data_input:
                # 如果 data_input 是字符串，尝试解析为 JSON
                if isinstance(data_input, str):
                    try:
                        data_input = json.loads(data_input)
                        print(f"[XlsxPreview] Parsed JSON string to dict: {list(data_input.keys()) if isinstance(data_input, dict) else type(data_input)}")
                    except json.JSONDecodeError as e:
                        # 尝试修复常见的 JSON 错误
                        print(f"[XlsxPreview] JSON parse failed: {e}, attempting auto-fix...")
                        fixed_json = self._try_fix_json(data_input)
                        if fixed_json:
                            data_input = fixed_json
                            print(f"[XlsxPreview] JSON auto-fixed successfully")
                        else:
                            return SkillResult(
                                status=SkillStatus.ERROR,
                                error=f"无法解析 data 参数为 JSON: {str(e)}"
                            )

                return SkillResult(
                    status=SkillStatus.ERROR,
                    error="请通过 file_url 或 file_path 参数提供文件"
                )

            # 模式 2: URL 预览模式（下载远程文件到本地）
            elif file_url:
                try:
                    dl_result = _download_remote_file(file_url, allowed_extensions=['.xlsx', '.xls'])
                    file_path = dl_result["file_path"]
                    download_url = dl_result["download_url"]
                    print(f"[XlsxPreview] Downloaded remote file: {file_url} -> {file_path}")
                except Exception as e:
                    return SkillResult(
                        status=SkillStatus.ERROR,
                        error=f"远程文件下载失败: {str(e)}"
                    )

            elif not file_path or not os.path.exists(file_path):
                return SkillResult(
                    status=SkillStatus.ERROR,
                    error=f"未提供数据且文件不存在: {file_path}"
                )

            # 验证文件类型
            if not file_path.lower().endswith(('.xlsx', '.xls')):
                return SkillResult(
                    status=SkillStatus.ERROR,
                    error="仅支持 .xlsx 或 .xls 文件格式"
                )

            file_name = os.path.basename(file_path)

            # 提取工作表数据 (预览)
            sheets_data = await self._extract_sheets_data(file_path, max_rows)

            if not sheets_data:
                print(f"[XlsxPreview] Metric extraction returned empty, but file exists. Continuing for download.")
                sheets_data = {}  # Continue with empty data so UI shows download link


            # 读取文件为 base64
            file_data_base64, file_size = await self._read_file_as_base64(file_path)

            # 如果指定了工作表，只返回该工作表
            if sheet_name:
                if sheet_name not in sheets_data:
                    return SkillResult(
                        status=SkillStatus.ERROR,
                        error=f"工作表 '{sheet_name}' 不存在，可用工作表: {list(sheets_data.keys())}"
                    )
                sheets_data = {sheet_name: sheets_data[sheet_name]}

            # 确保有可访问的下载 URL
            if not download_url:
                download_url = _ensure_download_url(file_path, download_url)

            # 构建返回数据
            data = {
                "file_name": file_name,
                "file_size": file_size,
                "file_data_base64": file_data_base64,
                "sheet_names": list(sheets_data.keys()),
                "sheets_data": sheets_data,
                "download_url": download_url,
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            }

            return SkillResult(
                status=SkillStatus.SUCCESS,
                data=data,
                message=f"Successfully processed Excel file: {file_name}. Please render the UI using [UI_RENDER: office_preview, xlsx_preview]"
            )

        except Exception as e:
            import traceback
            traceback.print_exc()
            return SkillResult(
                status=SkillStatus.ERROR,
                error=f"表格处理失败: {str(e)}"
            )


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

    parser = argparse.ArgumentParser(description="Run XlsxPreviewSkill directly")
    parser.add_argument("--file-path", type=str, dest="file_path")
    parser.add_argument("--data", type=str, dest="data")
    args = parser.parse_args()
    for k, v in vars(args).items():
        if v is not None:
            params[k] = v

    async def run():
        skill = XlsxPreviewSkill()
        result = await skill.execute(params)
        out = result if isinstance(result, dict) else {"data": str(result)}
        print(json.dumps(out, ensure_ascii=False, default=str, indent=2))

    asyncio.run(run())


if __name__ == "__main__":
    _main()
