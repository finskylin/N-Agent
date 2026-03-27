"""
Sandbox primitives for AgentCore.

Supports three execution modes:
- subprocess: plain host shell execution
- srt: Anthropic Sandbox Runtime when available, with subprocess fallback
- container: Docker/OCI-style container sandbox with readonly project mount
"""
from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Optional


@dataclass
class SandboxExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    command: str
    sandbox_type: str
    timed_out: bool = False


@dataclass(frozen=True)
class ContainerMount:
    host_path: str
    container_path: str
    readonly: bool = False


class SandboxProvider(ABC):
    @abstractmethod
    async def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: int = 60,
    ) -> SandboxExecutionResult:
        """Execute a shell command inside the sandbox."""

    @property
    @abstractmethod
    def sandbox_type(self) -> str:
        """Human-readable sandbox type."""


class SubprocessSandbox(SandboxProvider):
    """Plain subprocess execution for local development and fallback."""

    def __init__(self, work_dir: Optional[str] = None):
        self._work_dir = work_dir

    @property
    def sandbox_type(self) -> str:
        return "subprocess"

    async def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: int = 60,
    ) -> SandboxExecutionResult:
        effective_cwd = str(Path(cwd or self._work_dir or os.getcwd()).resolve())
        proc_env = os.environ.copy()
        if env:
            proc_env.update({k: str(v) for k, v in env.items()})

        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-lc",
            command,
            cwd=effective_cwd,
            env=proc_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            stdout, stderr = await proc.communicate()
            return SandboxExecutionResult(
                exit_code=-1,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace") or f"Command timeout after {timeout}s",
                command=command,
                sandbox_type=self.sandbox_type,
                timed_out=True,
            )

        return SandboxExecutionResult(
            exit_code=proc.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            command=command,
            sandbox_type=self.sandbox_type,
            timed_out=False,
        )


class SrtSandbox(SubprocessSandbox):
    """
    Minimal sandbox wrapper for AgentCore.

    Uses `srt` if present. Otherwise falls back to a regular subprocess to keep
    local CLI flows functional until a stricter sandbox is available.
    """

    def __init__(
        self,
        settings_path: Optional[str] = None,
        work_dir: Optional[str] = None,
        allow_fallback: bool = True,
    ):
        super().__init__(work_dir=work_dir)
        self._settings_path = settings_path
        self._allow_fallback = allow_fallback

    @property
    def sandbox_type(self) -> str:
        return "srt" if self.is_available() else "subprocess"

    def is_available(self) -> bool:
        return shutil.which("srt") is not None

    async def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: int = 60,
    ) -> SandboxExecutionResult:
        if not self.is_available():
            if not self._allow_fallback:
                raise RuntimeError("srt is not installed and fallback is disabled")
            return await super().execute(command=command, cwd=cwd, env=env, timeout=timeout)

        effective_cwd = str(Path(cwd or self._work_dir or os.getcwd()).resolve())
        proc_env = os.environ.copy()
        if env:
            proc_env.update({k: str(v) for k, v in env.items()})

        argv = ["srt"]
        if self._settings_path:
            argv.extend(["--settings", self._settings_path])
        argv.extend(["sh", "-lc", command])

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=effective_cwd,
            env=proc_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            stdout, stderr = await proc.communicate()
            return SandboxExecutionResult(
                exit_code=-1,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace") or f"Command timeout after {timeout}s",
                command=command,
                sandbox_type=self.sandbox_type,
                timed_out=True,
            )

        return SandboxExecutionResult(
            exit_code=proc.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            command=command,
            sandbox_type=self.sandbox_type,
            timed_out=False,
        )


class ContainerSandbox(SandboxProvider):
    """Run shell commands in a short-lived container with explicit mounts only."""

    def __init__(
        self,
        runtime_bin: str = "docker",
        image: str = "",
        project_root: Optional[str] = None,
        work_dir: Optional[str] = None,
        project_mount_readonly: bool = True,
        network_mode: str = "bridge",
        container_project_root: str = "/workspace/project",
        container_workdir: str = "/workspace/workdir",
        check_runtime: bool = True,
    ):
        self._runtime_bin = runtime_bin
        self._image = image.strip()
        self._project_root = str(Path(project_root).resolve()) if project_root else None
        self._work_dir = work_dir
        self._project_mount_readonly = project_mount_readonly
        self._network_mode = network_mode
        self._container_project_root = str(PurePosixPath(container_project_root))
        self._container_workdir = str(PurePosixPath(container_workdir))
        self._check_runtime = check_runtime

    @property
    def sandbox_type(self) -> str:
        return "container"

    def is_available(self) -> bool:
        return shutil.which(self._runtime_bin) is not None

    async def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: int = 60,
    ) -> SandboxExecutionResult:
        if not self._image:
            raise RuntimeError("Container sandbox requires a configured sandbox image")
        if not self.is_available():
            raise RuntimeError(f"Container runtime not found: {self._runtime_bin}")
        if self._check_runtime:
            await self._ensure_runtime_running(timeout=min(timeout, 10))

        effective_cwd = self._resolve_cwd(cwd)
        mount_plan = self._build_mount_plan(effective_cwd)
        container_cwd = self._resolve_container_cwd(effective_cwd)
        container_name = f"agentcore-sandbox-{uuid.uuid4().hex[:12]}"
        argv = self._build_command(
            command=command,
            env=env,
            container_name=container_name,
            container_cwd=container_cwd,
            mount_plan=mount_plan,
        )

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._stop_container(container_name)
            proc.kill()
            stdout, stderr = await proc.communicate()
            return SandboxExecutionResult(
                exit_code=-1,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace") or f"Command timeout after {timeout}s",
                command=command,
                sandbox_type=self.sandbox_type,
                timed_out=True,
            )

        return SandboxExecutionResult(
            exit_code=proc.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            command=command,
            sandbox_type=self.sandbox_type,
            timed_out=False,
        )

    def _resolve_cwd(self, cwd: Optional[str]) -> str:
        effective = Path(cwd or self._work_dir or os.getcwd()).resolve()
        if not effective.exists():
            raise RuntimeError(f"Sandbox working directory does not exist: {effective}")
        if not effective.is_dir():
            raise RuntimeError(f"Sandbox working directory is not a directory: {effective}")
        return str(effective)

    def _resolve_project_root(self, effective_cwd: str) -> Optional[str]:
        if self._project_root:
            return self._project_root
        return str(Path(effective_cwd).resolve())

    def _build_mount_plan(self, effective_cwd: str) -> list[ContainerMount]:
        mounts: list[ContainerMount] = []
        project_root = self._resolve_project_root(effective_cwd)
        if project_root:
            mounts.append(
                ContainerMount(
                    host_path=project_root,
                    container_path=self._container_project_root,
                    readonly=self._project_mount_readonly,
                )
            )

        if not project_root:
            return mounts

        cwd_path = Path(effective_cwd).resolve()
        project_path = Path(project_root).resolve()
        if cwd_path == project_path:
            return mounts

        if self._is_relative_to(cwd_path, project_path):
            relative = cwd_path.relative_to(project_path)
            mounts.append(
                ContainerMount(
                    host_path=str(cwd_path),
                    container_path=str(PurePosixPath(self._container_project_root) / relative.as_posix()),
                    readonly=False,
                )
            )
            return mounts

        mounts.append(
            ContainerMount(
                host_path=str(cwd_path),
                container_path=self._container_workdir,
                readonly=False,
            )
        )
        return mounts

    def _resolve_container_cwd(self, effective_cwd: str) -> str:
        project_root = self._resolve_project_root(effective_cwd)
        if not project_root:
            return self._container_workdir

        cwd_path = Path(effective_cwd).resolve()
        project_path = Path(project_root).resolve()
        if cwd_path == project_path:
            return self._container_project_root
        if self._is_relative_to(cwd_path, project_path):
            relative = cwd_path.relative_to(project_path)
            return str(PurePosixPath(self._container_project_root) / relative.as_posix())
        return self._container_workdir

    def _build_command(
        self,
        command: str,
        env: Optional[Dict[str, str]],
        container_name: str,
        container_cwd: str,
        mount_plan: list[ContainerMount],
    ) -> list[str]:
        argv = [self._runtime_bin, "run", "--rm", "--name", container_name, "-i"]
        if self._network_mode:
            argv.extend(["--network", self._network_mode])

        for mount in mount_plan:
            mount_spec = f"{mount.host_path}:{mount.container_path}"
            if mount.readonly:
                mount_spec = f"{mount_spec}:ro"
            argv.extend(["-v", mount_spec])

        container_env = {"HOME": "/tmp"}
        if env:
            container_env.update({k: str(v) for k, v in env.items()})
        for key, value in container_env.items():
            argv.extend(["-e", f"{key}={value}"])

        argv.extend(["-w", container_cwd, self._image, "sh", "-lc", command])
        return argv

    async def _ensure_runtime_running(self, timeout: int) -> None:
        proc = await asyncio.create_subprocess_exec(
            self._runtime_bin,
            "info",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"Container runtime check timed out: {self._runtime_bin} info") from exc
        if proc.returncode != 0:
            raise RuntimeError(f"Container runtime is not ready: {self._runtime_bin}")

    async def _stop_container(self, container_name: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            self._runtime_bin,
            "stop",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False


def create_sandbox_provider(config: Optional[object] = None) -> SandboxProvider:
    """Create a sandbox provider from config or environment variables."""

    provider = _get_config_value(config, "sandbox_provider", env_name="V4_SANDBOX_PROVIDER", default="auto")
    provider = str(provider).strip().lower() or "auto"

    if provider == "subprocess":
        return SubprocessSandbox(work_dir=_get_config_value(config, "sandbox_work_dir", env_name="V4_SANDBOX_WORK_DIR"))

    if provider == "srt":
        return SrtSandbox(
            settings_path=_get_config_value(config, "sandbox_settings_path", env_name="V4_SANDBOX_SETTINGS_PATH"),
            work_dir=_get_config_value(config, "sandbox_work_dir", env_name="V4_SANDBOX_WORK_DIR"),
        )

    if provider in {"container", "auto"}:
        image = str(
            _get_config_value(config, "sandbox_image", "sandbox_container_image", env_name="V4_SANDBOX_IMAGE", default="")
        ).strip()
        if image:
            return ContainerSandbox(
                runtime_bin=str(
                    _get_config_value(
                        config,
                        "sandbox_runtime_bin",
                        "sandbox_container_runtime_bin",
                        env_name="V4_SANDBOX_RUNTIME_BIN",
                        default="docker",
                    )
                ),
                image=image,
                project_root=_get_config_value(config, "project_root", env_name="PROJECT_ROOT"),
                work_dir=_get_config_value(config, "sandbox_work_dir", env_name="V4_SANDBOX_WORK_DIR"),
                project_mount_readonly=_to_bool(
                    _get_config_value(
                        config,
                        "sandbox_project_mount_readonly",
                        env_name="V4_SANDBOX_PROJECT_MOUNT_READONLY",
                        default=True,
                    )
                ),
                network_mode=str(
                    _get_config_value(config, "sandbox_network_mode", env_name="V4_SANDBOX_NETWORK_MODE", default="bridge")
                ),
                container_project_root=str(
                    _get_config_value(
                        config,
                        "sandbox_container_project_root",
                        env_name="V4_SANDBOX_CONTAINER_PROJECT_ROOT",
                        default="/workspace/project",
                    )
                ),
                container_workdir=str(
                    _get_config_value(
                        config,
                        "sandbox_container_workdir",
                        env_name="V4_SANDBOX_CONTAINER_WORKDIR",
                        default="/workspace/workdir",
                    )
                ),
                check_runtime=_to_bool(
                    _get_config_value(config, "sandbox_check_runtime", env_name="V4_SANDBOX_CHECK_RUNTIME", default=True)
                ),
            )
        if provider == "container":
            raise ValueError("V4_SANDBOX_IMAGE must be set when sandbox provider is container")

    return SrtSandbox(
        settings_path=_get_config_value(config, "sandbox_settings_path", env_name="V4_SANDBOX_SETTINGS_PATH"),
        work_dir=_get_config_value(config, "sandbox_work_dir", env_name="V4_SANDBOX_WORK_DIR"),
    )


def _get_config_value(
    config: Optional[object],
    *attr_names: str,
    env_name: Optional[str] = None,
    default=None,
):
    for attr_name in attr_names:
        if config is not None and hasattr(config, attr_name):
            value = getattr(config, attr_name)
            if value not in (None, ""):
                return value
    if env_name:
        env_value = os.getenv(env_name)
        if env_value not in (None, ""):
            return env_value
    return default


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
