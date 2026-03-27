"""
Skill Executor -- V4 技能执行器 (AgentCore 版本)

职责:
1. 通过 subprocess 执行 skill 脚本（stdin JSON → stdout JSON）
2. 支持动态技能执行（workflow/script 类型）
3. 记录执行耗时

所有 skill 通过独立进程执行，不依赖 Skill 基类。
"""
import json
import sys
import time
from typing import Dict, Any, Optional, Callable
from pathlib import Path
from loguru import logger

from .skill_discovery import SkillDiscovery


class V4SkillExecutor:
    """
    V4 技能执行器 — 纯 subprocess 模式

    所有 skill 通过 `python3 scripts/xxx.py` 执行:
    - stdin 传入 JSON 参数
    - stdout 读取 JSON 结果
    - 不依赖 Skill 基类
    """

    def __init__(self, discovery: SkillDiscovery):
        self._discovery = discovery
        self._loaded = False

        # 动态技能支持
        self._dynamic_loader = None
        self._dynamic_executor = None
        self._llm_client = None

    def set_dynamic_loader(self, loader) -> None:
        self._dynamic_loader = loader

    def set_llm_client(self, llm_client) -> None:
        self._llm_client = llm_client

    def _get_dynamic_executor(self):
        if self._dynamic_executor is None:
            try:
                from .dynamic_skill_executor import DynamicSkillExecutor
                self._dynamic_executor = DynamicSkillExecutor(
                    skill_invoker=self._create_skill_invoker(),
                    llm_client=self._llm_client
                )
            except ImportError as e:
                logger.warning(f"[V4SkillExecutor] DynamicSkillExecutor not available: {e}")
        return self._dynamic_executor

    def _create_skill_invoker(self) -> Callable:
        async def invoke_skill(skill_name: str, params: Dict[str, Any]) -> Any:
            result = await self.execute(skill_name, params)
            if isinstance(result, dict) and "data" in result:
                return result["data"]
            return result
        return invoke_skill

    def load_all(self) -> int:
        """扫描所有 skill 脚本路径（不再加载 Skill 类实例）"""
        count = 0
        for meta in self._discovery.get_executable_skills():
            if meta.script_paths:
                count += 1
        self._loaded = True
        logger.info(f"[V4SkillExecutor] Found {count} executable skills (subprocess mode)")
        return count

    def reload(self):
        self._loaded = False
        self.load_all()

    def get_instance(self, skill_name: str) -> Optional[Any]:
        """兼容接口 — subprocess 模式下始终返回 None"""
        return None

    def get_all_instances(self) -> Dict[str, Any]:
        """兼容接口 — subprocess 模式下返回空 dict"""
        return {}

    async def execute(
        self,
        skill_name: str,
        args: Dict[str, Any],
        user_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> Any:
        """
        执行技能

        优先级:
        1. 动态技能（从数据库加载）
        2. subprocess 执行脚本（stdin JSON → stdout JSON）
        """
        start_time = time.time()

        # 1. 优先检查动态技能
        if self._dynamic_loader:
            dynamic_skill = self._dynamic_loader.get_by_name(skill_name)
            if dynamic_skill:
                result = await self._execute_dynamic_skill(
                    dynamic_skill, args, user_id, session_id
                )
                await self._record_execution_time(skill_name, start_time)
                return result

        # 2. subprocess 执行脚本
        script_path = self._find_script_path(skill_name)
        if script_path:
            # toolbox_execute / sandbox_execute 允许更长的超时（视频处理、复杂脚本等）
            _timeout = 600 if skill_name in {"toolbox_execute", "sandbox_execute"} else 120
            result = await self._execute_via_subprocess(skill_name, script_path, args, timeout=_timeout)
            await self._record_execution_time(skill_name, start_time)
            return result

        # 3. 未找到
        return {"error": f"Skill '{skill_name}' not found"}

    def _find_script_path(self, skill_name: str) -> Optional[str]:
        """从 SkillDiscovery 查找 skill 的脚本路径"""
        try:
            get_fn = getattr(self._discovery, "get_by_name", None) or getattr(self._discovery, "get", None)
            if get_fn:
                meta = get_fn(skill_name)
                if meta and getattr(meta, "script_paths", None):
                    for sp in meta.script_paths:
                        if Path(sp).exists():
                            return sp
        except Exception:
            pass
        return None

    async def _execute_via_subprocess(
        self, skill_name: str, script_path: str, args: Dict[str, Any],
        timeout: int = 120,
    ) -> Any:
        """通过 subprocess 执行脚本: stdin JSON → stdout JSON"""
        import asyncio as _aio

        input_json = json.dumps(args, ensure_ascii=False, default=str)
        try:
            proc = await _aio.create_subprocess_exec(
                sys.executable, script_path,
                stdin=_aio.subprocess.PIPE,
                stdout=_aio.subprocess.PIPE,
                stderr=_aio.subprocess.PIPE,
            )
            stdout, stderr = await _aio.wait_for(
                proc.communicate(input=input_json.encode("utf-8")),
                timeout=timeout,
            )
            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()

            if stderr_text:
                logger.debug(f"[V4SkillExecutor] '{skill_name}' stderr: {stderr_text[:500]}")

            if not stdout_text:
                return {"error": f"Skill '{skill_name}' produced no output", "stderr": stderr_text[:500]}

            try:
                return json.loads(stdout_text)
            except json.JSONDecodeError:
                return {"result": stdout_text}

        except _aio.TimeoutError:
            logger.warning(f"[V4SkillExecutor] '{skill_name}' subprocess timed out after {timeout}s")
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return {"error": f"Skill '{skill_name}' timed out after {timeout}s"}
        except Exception as e:
            # WriteUnixTransport closed 是 asyncio 高并发下的偶发竞态，重试一次
            if "WriteUnixTransport" in str(e) or "handler is closed" in str(e):
                logger.warning(f"[V4SkillExecutor] '{skill_name}' pipe race, retrying once...")
                await _aio.sleep(0.2)
                try:
                    proc = await _aio.create_subprocess_exec(
                        sys.executable, script_path,
                        stdin=_aio.subprocess.PIPE,
                        stdout=_aio.subprocess.PIPE,
                        stderr=_aio.subprocess.PIPE,
                    )
                    stdout, stderr = await _aio.wait_for(
                        proc.communicate(input=input_json.encode("utf-8")),
                        timeout=timeout,
                    )
                    stdout_text = stdout.decode("utf-8", errors="replace").strip()
                    if stdout_text:
                        try:
                            return json.loads(stdout_text)
                        except json.JSONDecodeError:
                            return {"result": stdout_text}
                except Exception as e2:
                    logger.error(f"[V4SkillExecutor] '{skill_name}' retry failed: {e2}")
                    return {"error": str(e2)}
            logger.error(f"[V4SkillExecutor] '{skill_name}' subprocess failed: {e}")
            return {"error": str(e)}

    @staticmethod
    async def _record_execution_time(skill_name: str, start_time: float):
        duration = round(time.time() - start_time, 2)
        logger.info(f"[V4SkillExecutor] Skill '{skill_name}' executed in {duration}s")

    @staticmethod
    async def get_average_execution_time(skill_name: str) -> Optional[float]:
        return None

    async def _execute_dynamic_skill(
        self,
        skill_data: Dict[str, Any],
        args: Dict[str, Any],
        user_id: Optional[str],
        session_id: Optional[str]
    ) -> Any:
        skill_name = skill_data.get("skill_name", "unknown")
        logger.info(f"[V4SkillExecutor] Executing dynamic skill: {skill_name}")

        executor = self._get_dynamic_executor()
        if not executor:
            return {"error": "Dynamic skill executor not available"}

        try:
            query = args.get("query") or args.get("user_query", "")
            params = {k: v for k, v in args.items() if k not in ("query", "user_query")}

            result = await executor.execute(
                skill_data=skill_data,
                query=query,
                params=params,
                user_id=user_id,
                session_id=session_id,
            )
            return result

        except Exception as e:
            logger.error(f"[V4SkillExecutor] Dynamic skill execution error: {e}")
            return {"error": str(e)}
