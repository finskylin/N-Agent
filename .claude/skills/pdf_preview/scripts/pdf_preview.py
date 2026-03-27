"""
PDF Preview Skill
PDF 文档预览技能 - 将 pdf 文件渲染到数据看板
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

class PdfPreviewSkill:
    """PDF 文档生成与预览技能"""

    @property
    def name(self) -> str:
        return "pdf_preview"

    @property
    def description(self) -> str:
        return "生成或预览 PDF 文档。支持从数据生成文档，或预览现有文件，提供下载链接和文本预览。"

    @property
    def category(self) -> str:
        return "office_preview"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "file_path": {"type": "string", "required": False, "description": "pdf 文件路径 (预览模式)"},
            "data": {
                "type": "object", 
                "required": False, 
                "description": "用于生成文档的数据 (生成模式)。格式: {'title': str, 'content': str}"
            },
            "file_url": {"type": "string", "required": False, "description": "pdf 文件 URL"},
            "extract_text_only": {"type": "boolean", "default": False, "description": "是否仅提取文本"}
        }

    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "file_name": "string",
            "file_size": "integer",
            "file_data_base64": "base64 encoded file content",
            "text_content": "extracted text content",
            "page_count": "integer",
            "download_url": "string (optional)"
        }

    async def _extract_text_from_pdf(self, file_path: str) -> tuple[str, int]:
        """从 PDF 文件提取文本内容"""
        try:
            import pypdf

            text_content = []
            reader = pypdf.PdfReader(file_path)
            page_count = len(reader.pages)

            for page in reader.pages:
                text = page.extract_text()
                if text and text.strip():
                    text_content.append(text.strip())

            return "\n\n".join(text_content), page_count

        except Exception as e:
            print(f"[PdfPreview] Text extraction failed: {e}")
            return "", 0

    async def _read_file_as_base64(self, file_path: str) -> tuple[str, int]:
        """读取文件并转换为 base64"""
        try:
            with open(file_path, 'rb') as f:
                file_data = f.read()
                base64_data = base64.b64encode(file_data).decode('utf-8')
                return base64_data, os.path.getsize(file_path)
        except Exception as e:
            print(f"[PdfPreview] Failed to read file: {e}")
            return "", 0

    async def execute(self, context: dict) -> dict:
        """执行 pdf 生成或预览"""
        try:
            print(f"[PdfPreview] Execute started. Context type: {type(context)}")
            
            # --- ROBUST PARAMS EXTRACTION ---
            params = {}
            if context is None:
                print("[PdfPreview] Context is None")
                params = {}
            elif isinstance(context, dict):
                print("[PdfPreview] Context is dict")
                params = context.get('params', {})
            else:
                # Assume object
                try:
                    params = getattr(context, 'params', {})
                except Exception as e:
                     print(f"[PdfPreview] Error getting params from object: {e}")
                     params = {}
            
            if params is None:
                print("[PdfPreview] Params was None, resetting to empty dict")
                params = {}
                
            print(f"[PdfPreview] Final params keys: {params.keys()}")
            
            file_path = params.get("file_path", "")
            file_url = params.get("file_url", "")
            data_input = params.get("data", None)
            extract_text_only = params.get("extract_text_only", False)
            download_url = ""

            # 模式 1: 生成模式
            if data_input:
                # 处理 LLM 传入的 JSON 字符串情况
                if isinstance(data_input, str):
                    import json
                    try:
                        data_input = json.loads(data_input)
                        print(f"[PdfPreview] Parsed JSON string to dict: {list(data_input.keys()) if isinstance(data_input, dict) else type(data_input)}")
                    except json.JSONDecodeError as e:
                        return SkillResult(
                            status=SkillStatus.ERROR,
                            error=f"data 参数 JSON 解析失败: {str(e)}"
                        )

                return SkillResult(
                    status=SkillStatus.ERROR,
                    error="请通过 file_url 或 file_path 参数提供文件"
                )

            # 模式 2: URL 预览模式（下载远程文件到本地）
            elif file_url:
                try:
                    dl_result = _download_remote_file(file_url, allowed_extensions=['.pdf'])
                    file_path = dl_result["file_path"]
                    download_url = dl_result["download_url"]
                    print(f"[PdfPreview] Downloaded remote file: {file_url} -> {file_path}")
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
            if not file_path.lower().endswith('.pdf'):
                return SkillResult(
                    status=SkillStatus.ERROR,
                    error="仅支持 .pdf 文件格式"
                )

            file_name = os.path.basename(file_path)

            # 提取文本内容
            text_content, page_count = await self._extract_text_from_pdf(file_path)

            # 读取文件为 base64
            file_data_base64, file_size = await self._read_file_as_base64(file_path)

            # 确保有可访问的下载 URL
            if not download_url:
                download_url = _ensure_download_url(file_path, download_url)

            # 构建返回数据
            data = {
                "file_name": file_name,
                "file_size": file_size,
                "file_data_base64": file_data_base64,
                "text_content": text_content,
                "page_count": page_count,
                "download_url": download_url,
                "mime_type": "application/pdf"
            }

            return SkillResult(
                status=SkillStatus.SUCCESS,
                data=data,
                message=f"Successfully processed PDF document: {file_name}. Please render the UI using [UI_RENDER: office_preview, pdf_preview]"
            )

        except Exception as e:
            import traceback
            traceback.print_exc()
            return SkillResult(
                status=SkillStatus.ERROR,
                error=f"文档处理失败: {str(e)}"
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

    parser = argparse.ArgumentParser(description="Run PdfPreviewSkill directly")
    parser.add_argument("--file-path", type=str, dest="file_path")
    parser.add_argument("--data", type=str, dest="data")
    args = parser.parse_args()
    for k, v in vars(args).items():
        if v is not None:
            params[k] = v

    async def run():
        skill = PdfPreviewSkill()
        result = await skill.execute(params)
        out = result if isinstance(result, dict) else {"data": str(result)}
        print(json.dumps(out, ensure_ascii=False, default=str, indent=2))

    asyncio.run(run())


if __name__ == "__main__":
    _main()
