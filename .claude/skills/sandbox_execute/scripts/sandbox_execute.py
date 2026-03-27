"""
Sandbox Execute Skill
安全沙箱代码执行器 - 基于 E2B Firecracker microVM

在完全隔离的云端沙箱中执行代码，适合：
- 执行不可信的用户代码
- 数据分析实验
- 代码验证和测试
"""

from typing import Any, Dict, List
from loguru import logger
import time
import os



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

class SandboxExecuteSkill:
    """
    安全沙箱代码执行技能

    使用 E2B Code Interpreter SDK 在 Firecracker microVM 中执行代码。
    每次执行创建独立沙箱，完全隔离，用后自动销毁。
    """

    SUPPORTED_LANGUAGES = ["python", "javascript", "bash"]

    @property
    def name(self) -> str:
        return "sandbox_execute"

    @property
    def description(self) -> str:
        return (
            "在安全隔离的云端沙箱中执行代码（基于E2B Firecracker microVM）。"
            "支持Python/JavaScript/Bash，每次执行在独立微型虚拟机中运行，"
            "完全隔离，用后自动销毁。适合执行不可信代码、数据分析实验、代码验证。"
        )

    @property
    def category(self) -> str:
        return "execution"

    @property
    def dependencies(self) -> List[str]:
        return []

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "code": {
                "type": "string",
                "required": True,
                "description": "要执行的代码"
            },
            "language": {
                "type": "string",
                "enum": self.SUPPORTED_LANGUAGES,
                "default": "python",
                "description": "编程语言: python (默认), javascript, bash"
            },
            "timeout": {
                "type": "integer",
                "default": 30,
                "description": "执行超时时间（秒），默认 30"
            },
        }

    async def execute(self, context: dict) -> dict:
        """在 E2B 沙箱中执行代码"""
        start_time = time.time()
        params = context or {}

        code = params.get("code", "")
        if not code:
            # 尝试从 query 中提取代码
            code = params.get("query", context.user_query or "")

        if not code:
            return SkillResult(
                status=SkillStatus.ERROR,
                error="缺少必要参数: code（要执行的代码）",
                execution_time_ms=(time.time() - start_time) * 1000,
            )

        language = params.get("language", "python")
        timeout = min(params.get("timeout", 30), 120)  # 最大 120 秒

        logger.info(f"[SandboxExecute] language={language}, timeout={timeout}, code_len={len(code)}")

        try:
            result = await self._run_in_sandbox(code, language, timeout)
            execution_time = (time.time() - start_time) * 1000

            return SkillResult(
                status=SkillStatus.SUCCESS,
                data=result,
                message=f"代码在沙箱中执行成功 ({language})",
                execution_time_ms=execution_time,
                metadata={
                    "source": "sandbox_execute",
                    "language": language,
                    "sandbox": "e2b",
                },
            )

        except ImportError:
            logger.error("[SandboxExecute] e2b_code_interpreter not installed")
            execution_time = (time.time() - start_time) * 1000
            return SkillResult(
                status=SkillStatus.ERROR,
                error=(
                    "沙箱执行失败: e2b_code_interpreter 未安装。"
                    "请在 agent-service 环境中安装 e2b-code-interpreter 并配置 E2B_API_KEY。"
                ),
                execution_time_ms=execution_time,
                metadata={"source": "sandbox_execute", "language": language},
            )

        except Exception as e:
            logger.error(f"[SandboxExecute] Error: {e}")
            execution_time = (time.time() - start_time) * 1000
            return SkillResult(
                status=SkillStatus.ERROR,
                error=f"沙箱执行失败: {str(e)}",
                execution_time_ms=execution_time,
                metadata={"source": "sandbox_execute", "language": language},
            )

    async def _run_in_sandbox(self, code: str, language: str, timeout: int) -> Dict[str, Any]:
        """使用 E2B Code Interpreter 执行代码"""
        from e2b_code_interpreter import Sandbox

        api_key = os.environ.get("E2B_API_KEY", "")
        if not api_key:
            raise ValueError(
                "E2B_API_KEY 未配置。请在 .env 中设置 E2B_API_KEY。"
                "获取方式: https://e2b.dev/docs"
            )

        sandbox_kwargs = {"api_key": api_key, "timeout": timeout}

        with Sandbox.create(**sandbox_kwargs) as sandbox:
            if language == "python":
                execution = sandbox.run_code(code)
            elif language == "javascript":
                execution = sandbox.run_code(code, language="javascript")
            elif language == "bash":
                # Bash 通过 subprocess 方式执行
                execution = sandbox.run_code(
                    f"import subprocess; result = subprocess.run({repr(code)}, "
                    f"shell=True, capture_output=True, text=True); "
                    f"print(result.stdout); "
                    f"if result.stderr: print('STDERR:', result.stderr)"
                )
            else:
                raise ValueError(f"不支持的语言: {language}")

            # 解析执行结果
            stdout = getattr(execution, "text", "") or ""
            logs = getattr(execution, "logs", None)
            error = getattr(execution, "error", None)

            result = {
                "stdout": stdout,
                "success": error is None,
            }

            if logs:
                stdout_logs = getattr(logs, "stdout", [])
                stderr_logs = getattr(logs, "stderr", [])
                if stdout_logs:
                    result["logs_stdout"] = stdout_logs
                if stderr_logs:
                    result["logs_stderr"] = stderr_logs

            if error:
                result["error"] = str(error)
                result["success"] = False

            return result

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

    parser = argparse.ArgumentParser(description="Run SandboxExecuteSkill directly")
    parser.add_argument("--code", type=str, dest="code")
    args = parser.parse_args()
    for k, v in vars(args).items():
        if v is not None:
            params[k] = v

    async def run():
        skill = SandboxExecuteSkill()
        result = await skill.execute(params)
        out = result if isinstance(result, dict) else {"data": str(result)}
        print(json.dumps(out, ensure_ascii=False, default=str, indent=2))

    asyncio.run(run())


if __name__ == "__main__":
    _main()
