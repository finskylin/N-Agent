"""
Docker Operator Skill

执行环境提供者（按优先级）：
  0. toolbox    → SSH 进 agent-toolbox，完整 Linux 环境 + Playwright + 数据处理库（推荐首选）
  1. 已有容器   → docker exec（需要进入特定已有容器）
  2. 新建沙箱   → docker run（需要隔离或特殊镜像）
stdin JSON → stdout JSON
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import signal
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── 配置 ──────────────────────────────────────────────────────────────────────

_DOCKER_HOST = os.getenv("DOCKER_HOST", "unix:///var/run/docker.sock")
_AGENT_SERVICE_URL = os.getenv("AGENT_SERVICE_URL", "http://localhost:8000")

# Toolbox SSH 配置
_TOOLBOX_HOST = os.getenv("TOOLBOX_HOST", "agent-toolbox")
_TOOLBOX_PORT = int(os.getenv("TOOLBOX_PORT", "22"))
_TOOLBOX_USER = os.getenv("TOOLBOX_USER", "root")
_TOOLBOX_SSH_KEY = os.getenv("TOOLBOX_SSH_KEY_PATH", "/root/.ssh/id_ed25519")
_TOOLBOX_SSH_PASS = os.getenv("TOOLBOX_SSH_PASSWORD", "")

# stdout 截断（toolbox action 返回给 LLM 的最大字符数）
_MAX_STDOUT = 4000
# toolbox 工作目录
_TOOLBOX_WORKSPACE = "/opt/agent-workspace"
# 产物文件路径模式
_ARTIFACT_RE = re.compile(
    r'(/opt/agent-workspace/\S+\.(?:xlsx|csv|pdf|png|jpg|json|html|md|docx|pptx|parquet|zip))'
)
_MIME_MAP = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".csv": "text/csv", ".json": "application/json", ".html": "text/html",
    ".md": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".parquet": "application/octet-stream", ".zip": "application/zip",
}


def _register_task(
    name: str,
    exec_mode: str,
    command: str = "",
    pid: Optional[int] = None,
    container_id: str = "",
    container_name: str = "",
    log_path: str = "",
    created_by: str = "",
    session_id: str = "",
    group_id: str = "",
) -> Optional[str]:
    """向 TaskService 注册后台任务，返回 task_id（失败返回 None）"""
    try:
        import urllib.request
        import json as _json
        payload = _json.dumps({
            "name": name,
            "exec_mode": exec_mode,
            "command": command,
            "pid": pid,
            "container_id": container_id,
            "container_name": container_name,
            "log_path": log_path,
            "created_by": created_by,
            "session_id": session_id,
            "group_id": group_id,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{_AGENT_SERVICE_URL}/api/v1/tasks/",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
            return data.get("task_id")
    except Exception:
        return None


def _register_group(
    session_id: str,
    user_id: str = "1",
    channel: str = "web",
    callback_prompt: str = "",
) -> Optional[str]:
    """创建 TaskGroup，返回 group_id（失败返回 None）"""
    try:
        import urllib.request
        import json as _json
        payload = _json.dumps({
            "session_id": session_id,
            "user_id": user_id,
            "channel": channel,
            "callback_prompt": callback_prompt or None,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{_AGENT_SERVICE_URL}/api/v1/task_groups/",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
            return data.get("group_id")
    except Exception:
        return None


def _add_task_to_group(group_id: str, task_id: str) -> bool:
    """将 task 加入 group"""
    try:
        import urllib.request
        import json as _json
        payload = _json.dumps({"task_id": task_id}).encode("utf-8")
        req = urllib.request.Request(
            f"{_AGENT_SERVICE_URL}/api/v1/task_groups/{group_id}/add",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False

# 沙箱资源限制
_SANDBOX_MAX_CONTAINERS = int(os.getenv("SANDBOX_MAX_CONTAINERS", "5"))
_SANDBOX_DEFAULT_MEM = os.getenv("SANDBOX_DEFAULT_MEM", "2g")
_SANDBOX_DEFAULT_CPUS = float(os.getenv("SANDBOX_DEFAULT_CPUS", "2"))
_SANDBOX_MAX_MEM = os.getenv("SANDBOX_MAX_MEM", "8g")
_SANDBOX_MAX_CPUS = float(os.getenv("SANDBOX_MAX_CPUS", "4"))
_SANDBOX_DEFAULT_TIMEOUT = int(os.getenv("SANDBOX_DEFAULT_TIMEOUT", "3600"))
_SANDBOX_MAX_TIMEOUT = int(os.getenv("SANDBOX_MAX_TIMEOUT", "86400"))
_SANDBOX_MAX_HOST_USAGE = float(os.getenv("SANDBOX_MAX_HOST_USAGE", "70"))
_SANDBOX_EXITED_RETAIN = int(os.getenv("SANDBOX_EXITED_RETAIN", "300"))
_LABEL_KEY = "created_by"
_LABEL_VAL = "agent-service"


# ── Toolbox SSH ───────────────────────────────────────────────────────────────

async def _ssh_run(command: str, timeout: int = 60) -> Dict[str, Any]:
    """通过 asyncssh 在 agent-toolbox 执行命令，返回 {success, stdout, stderr, returncode}"""
    try:
        import asyncssh
    except ImportError:
        return {"success": False, "stdout": "", "stderr": "asyncssh 未安装: pip install asyncssh", "returncode": -1}

    if not _TOOLBOX_HOST:
        return {"success": False, "stdout": "", "stderr": "TOOLBOX_HOST 未配置", "returncode": -1}

    kwargs: Dict[str, Any] = {
        "host": _TOOLBOX_HOST,
        "port": _TOOLBOX_PORT,
        "username": _TOOLBOX_USER,
        "known_hosts": None,
    }
    if _TOOLBOX_SSH_KEY and os.path.exists(_TOOLBOX_SSH_KEY):
        kwargs["client_keys"] = [_TOOLBOX_SSH_KEY]
    elif _TOOLBOX_SSH_PASS:
        kwargs["password"] = _TOOLBOX_SSH_PASS

    try:
        async with asyncssh.connect(**kwargs) as conn:
            r = await asyncio.wait_for(conn.run(command, check=False), timeout=timeout)
            return {
                "success": r.returncode == 0,
                "stdout": r.stdout or "",
                "stderr": r.stderr or "",
                "returncode": r.returncode,
            }
    except asyncio.TimeoutError:
        return {"success": False, "stdout": "", "stderr": f"超时({timeout}s)", "returncode": -1}
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": str(e), "returncode": -1}


def _truncate_stdout(s: str) -> tuple:
    """截断 stdout，返回 (截断后内容, 是否截断)"""
    if len(s) > _MAX_STDOUT:
        return s[:_MAX_STDOUT] + f"\n...[截断，完整输出 {len(s)} 字符]", True
    return s, False


def _upload_artifact_to_storage(file_bytes: bytes, file_name: str) -> str:
    """保存到本地对象存储并返回下载 URL"""
    from urllib.parse import quote
    try:
        store_dir = Path(os.getenv("LOCAL_OBJECT_STORE_DIR", "data/object_storage"))
        dest = store_dir / "uploads" / "toolbox" / file_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(file_bytes)
        base_url = (os.getenv("AGENT_PUBLIC_BASE_URL") or
                    f"http://{os.getenv('AGENT_EXTERNAL_HOST', '127.0.0.1')}:{os.getenv('AGENT_SERVICE_PORT', '8000')}")
        token = f"object_storage/uploads/toolbox/{file_name}"
        return f"{base_url.rstrip('/')}/api/files/download?path={quote(token, safe='/:_-.()')}"
    except Exception:
        return ""


async def _collect_artifacts(stdout: str) -> List[Dict[str, Any]]:
    """从 stdout 提取产物路径，通过 SSH base64 读取后上传到本地存储"""
    paths = list(dict.fromkeys(_ARTIFACT_RE.findall(stdout)))  # 去重保序
    result = []
    for p in paths:
        name = p.rsplit("/", 1)[-1]
        ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        artifact: Dict[str, Any] = {"type": "file", "path": p, "name": name}
        r = await _ssh_run(f"base64 -w 0 {p}", timeout=30)
        if r["success"] and r["stdout"].strip():
            try:
                file_bytes = base64.b64decode(r["stdout"].strip())
                url = _upload_artifact_to_storage(file_bytes, name)
                if url:
                    artifact["download_url"] = url
            except Exception:
                pass
        result.append(artifact)
    return result


def _wrap_with_done_marker(cmd: str, log_path: str) -> str:
    """
    将任务命令包装成后台执行形式，完成后写入 .done 文件。
    约定：<log_path>.done 内容为退出码，TaskMonitor 通过检测此文件判断任务完成。
    """
    done_path = f"{log_path}.done"
    # nohup 后台执行，stdout/stderr 写入日志，完成后写退出码到 .done
    return (
        f"nohup bash -c '{cmd}; echo $? > {done_path}' "
        f"> {log_path} 2>&1 &"
    )


async def _run_background_toolbox(
    cmd: str,
    params: Dict[str, Any],
    task_name: str,
) -> Dict[str, Any]:
    """
    在 toolbox 后台执行命令，注册 TaskService + TaskGroup，立即返回。
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # 使用共享卷路径，agent-service 和 agent-toolbox 均挂载 toolbox-logs 到此路径
    log_path = f"/opt/agent-workspace/logs/bg_{ts}_{uuid.uuid4().hex[:6]}.log"
    wrapped = _wrap_with_done_marker(cmd, log_path)

    # 启动后台任务（SSH 立即返回）
    r = await _ssh_run(wrapped, timeout=10)
    if not r["success"]:
        return {"success": False, "error": f"启动后台任务失败: {r['stderr']}", "exec_env": "toolbox"}

    # 注册到 TaskService
    session_id = str(params.get("session_id", ""))
    group_id = str(params.get("group_id", ""))
    user_id = str(params.get("user_id", "1"))
    channel = str(params.get("channel", "web"))
    callback_prompt = str(params.get("callback_prompt", ""))

    task_id = _register_task(
        name=task_name[:80],
        exec_mode="toolbox",
        command=cmd,
        log_path=log_path,
        created_by=str(params.get("created_by", "")),
        session_id=session_id,
        group_id=group_id,
    )

    # 若有 session_id 且没有 group_id，自动创建任务组
    if session_id and not group_id:
        group_id = _register_group(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            callback_prompt=callback_prompt,
        ) or ""

    # 将任务加入任务组
    if group_id and task_id:
        _add_task_to_group(group_id, task_id)

    out: Dict[str, Any] = {
        "for_llm": {
            "status": "background_started",
            "task_id": task_id or "",
            "log_path": log_path,
            "message": "异步任务已在后台启动。",
            "next_action": "立即通知用户任务已启动，然后结束本轮回答。不要调用 async_task 轮询进度，系统会在任务完成后自动回调通知用户。",
        },
        "success": True,
        "exec_env": "toolbox",
        "background": True,
    }
    if group_id:
        out["for_llm"]["group_id"] = group_id
    return out


async def _action_toolbox(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    在 agent-toolbox 容器（现存 Linux 环境）执行操作。

    支持的 toolbox_action：
      command     - 执行 bash 命令（background=true 时后台执行）
      script      - 运行脚本 (bash/python/node)（background=true 时后台执行）
      file_write  - 写入文件
      file_read   - 读取文件
      file_delete - 删除文件
      install     - 安装软件包 (apt/pip/npm)
      status      - 工具箱状态
      playwright  - 浏览器自动化 (screenshot/scrape/pdf)

    后台任务参数（background=true 时生效）：
      session_id      - 所属会话（必填，用于 Agent 回调）
      group_id        - 任务组 ID（可选，不填则自动创建）
      user_id         - 用户 ID（默认 "1"）
      channel         - 回调渠道（默认 "web"）
      callback_prompt - 完成后的提示词（可选，默认模板）
    """
    toolbox_action = (params.get("toolbox_action") or params.get("action", "")).strip()
    if toolbox_action == "toolbox":
        return {"success": False, "error": "请用 toolbox_action 指定子操作：command/script/file_write/file_read/file_delete/install/status/playwright"}

    timeout = int(params.get("timeout", 600))
    background = params.get("background", False)

    # ── command ──
    if toolbox_action == "command":
        cmd = params.get("command", "")
        working_dir = params.get("working_dir", _TOOLBOX_WORKSPACE)
        env_vars = params.get("env", {}) or {}
        env_str = " ".join(f'export {k}="{v}";' for k, v in env_vars.items())
        full_cmd = f"{env_str} cd {working_dir} && {cmd}".strip()

        if background:
            return await _run_background_toolbox(full_cmd, params, task_name=cmd[:60])

        r = await _ssh_run(full_cmd, timeout=timeout)
        stdout, truncated = _truncate_stdout(r["stdout"])
        artifacts = await _collect_artifacts(r["stdout"]) if r["success"] else []
        out: Dict[str, Any] = {
            "success": r["success"], "exec_env": "toolbox",
            "stdout": stdout, "stderr": r["stderr"],
        }
        if truncated:
            out["stdout_truncated"] = True
        if artifacts:
            out["artifacts"] = artifacts
        return out

    # ── script ──
    elif toolbox_action == "script":
        script = params.get("script", "")
        script_type = params.get("script_type", "bash")
        working_dir = params.get("working_dir", f"{_TOOLBOX_WORKSPACE}/scripts")
        suffix = {"python": ".py", "node": ".js", "bash": ".sh"}.get(script_type, ".sh")
        interpreter = {"python": "python3", "node": "node", "bash": "bash"}.get(script_type, "bash")
        tmp = f"/tmp/tb_script_{uuid.uuid4().hex[:8]}{suffix}"
        b64 = base64.b64encode(script.encode()).decode()
        write_r = await _ssh_run(f"echo {b64} | base64 -d > {tmp}", timeout=15)
        if not write_r["success"]:
            return {"success": False, "error": f"写脚本失败: {write_r['stderr']}", "exec_env": "toolbox"}

        run_cmd = f"chmod +x {tmp} && cd {working_dir} && {interpreter} {tmp}; rm -f {tmp}"

        if background:
            return await _run_background_toolbox(run_cmd, params, task_name=f"{script_type}_script")

        run_r = await _ssh_run(run_cmd, timeout=timeout)
        stdout, truncated = _truncate_stdout(run_r["stdout"])
        artifacts = await _collect_artifacts(run_r["stdout"]) if run_r["success"] else []
        out = {
            "success": run_r["success"], "exec_env": "toolbox",
            "stdout": stdout, "stderr": run_r["stderr"],
        }
        if truncated:
            out["stdout_truncated"] = True
        if artifacts:
            out["artifacts"] = artifacts
        return out

    # ── file_write ──
    elif toolbox_action == "file_write":
        path = params.get("file_path", "")
        content = params.get("file_content", "")
        if not path:
            return {"success": False, "error": "file_path 必填", "exec_env": "toolbox"}
        b64 = base64.b64encode(content.encode()).decode()
        r = await _ssh_run(f"mkdir -p $(dirname {path}) && echo {b64} | base64 -d > {path}", timeout=30)
        return {"success": r["success"], "exec_env": "toolbox", "path": path,
                "error": r["stderr"] if not r["success"] else ""}

    # ── file_read ──
    elif toolbox_action == "file_read":
        path = params.get("file_path", "")
        if not path:
            return {"success": False, "error": "file_path 必填", "exec_env": "toolbox"}
        r = await _ssh_run(f"cat {path}", timeout=30)
        return {"success": r["success"], "exec_env": "toolbox", "path": path,
                "content": r["stdout"], "error": r["stderr"] if not r["success"] else ""}

    # ── file_delete ──
    elif toolbox_action == "file_delete":
        path = params.get("file_path", "")
        if not path:
            return {"success": False, "error": "file_path 必填", "exec_env": "toolbox"}
        r = await _ssh_run(f"rm -f {path}", timeout=15)
        return {"success": r["success"], "exec_env": "toolbox", "path": path,
                "error": r["stderr"] if not r["success"] else ""}

    # ── install ──
    elif toolbox_action == "install":
        package = params.get("package", "")
        manager = params.get("package_manager", "apt")
        cmd_map = {"apt": f"apt-get install -y {package}", "pip": f"pip3 install {package}", "npm": f"npm install -g {package}"}
        cmd = cmd_map.get(manager, f"apt-get install -y {package}")
        r = await _ssh_run(cmd, timeout=300)
        return {"success": r["success"], "exec_env": "toolbox", "package": package,
                "stdout": r["stdout"], "error": r["stderr"] if not r["success"] else ""}

    # ── status ──
    elif toolbox_action == "status":
        r = await _ssh_run("hostname && uptime && df -h /opt/agent-workspace 2>/dev/null | tail -1 && free -h | grep Mem", timeout=15)
        return {"success": r["success"], "exec_env": "toolbox", "stdout": r["stdout"],
                "error": r["stderr"] if not r["success"] else ""}

    # ── playwright ──
    elif toolbox_action == "playwright":
        playwright_action = params.get("playwright_action", "screenshot")
        url = params.get("url", "")
        if not url:
            return {"success": False, "error": "url 必填", "exec_env": "toolbox"}
        output_path = params.get("output_path", f"{_TOOLBOX_WORKSPACE}/data/playwright_output")
        if playwright_action == "screenshot":
            output_path = output_path if output_path.endswith(".png") else output_path + ".png"
            script = f'''import asyncio
from playwright.async_api import async_playwright
async def run():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await b.new_page(viewport={{"width":1920,"height":1080}})
        await page.goto("{url}", wait_until="networkidle", timeout=60000)
        await asyncio.sleep(2)
        await page.screenshot(path="{output_path}", full_page={str(params.get("full_page",True))})
        await b.close()
        print("{output_path}")
asyncio.run(run())'''
        elif playwright_action == "scrape":
            selector = params.get("selector", "body")
            output_path = output_path if output_path.endswith(".json") else output_path + ".json"
            script = f'''import asyncio, json
from playwright.async_api import async_playwright
async def run():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await b.new_page()
        await page.goto("{url}", wait_until="networkidle", timeout=60000)
        await asyncio.sleep(2)
        els = await page.query_selector_all("{selector}")
        data = [await e.inner_text() for e in els]
        with open("{output_path}","w") as f:
            json.dump({{"url":"{url}","data":data}}, f, ensure_ascii=False)
        await b.close()
        print("{output_path}")
asyncio.run(run())'''
        elif playwright_action == "pdf":
            output_path = output_path if output_path.endswith(".pdf") else output_path + ".pdf"
            script = f'''import asyncio
from playwright.async_api import async_playwright
async def run():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await b.new_page()
        await page.goto("{url}", wait_until="networkidle", timeout=60000)
        await asyncio.sleep(2)
        await page.pdf(path="{output_path}", format="A4", print_background=True)
        await b.close()
        print("{output_path}")
asyncio.run(run())'''
        else:
            return {"success": False, "error": f"不支持的 playwright_action: {playwright_action}", "exec_env": "toolbox"}

        # 执行脚本
        pw_params = {**params, "toolbox_action": "script", "script": script, "script_type": "python"}
        return await _action_toolbox(pw_params)

    else:
        return {
            "success": False,
            "error": f"不支持的 toolbox_action: '{toolbox_action}'。支持: command/script/file_write/file_read/file_delete/install/status/playwright",
            "exec_env": "toolbox",
        }


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _parse_mem_bytes(s: str) -> int:
    """解析内存字符串为字节数: '2g' → 2147483648"""
    s = s.strip().lower()
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    if s[-1] in units:
        return int(float(s[:-1]) * units[s[-1]])
    return int(s)


def _get_docker_client():
    """延迟导入 docker SDK"""
    try:
        import docker
        return docker.from_env()
    except ImportError:
        raise RuntimeError("docker Python SDK 未安装，无法操作其他容器。pip install docker")
    except Exception as e:
        raise RuntimeError(f"无法连接 Docker: {e}")


def _check_host_resources() -> tuple:
    """检查宿主机资源利用率"""
    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=1)
        mem_pct = psutil.virtual_memory().percent
        if cpu_pct > _SANDBOX_MAX_HOST_USAGE or mem_pct > _SANDBOX_MAX_HOST_USAGE:
            return False, f"宿主机资源紧张 (CPU={cpu_pct:.0f}%, MEM={mem_pct:.0f}%，上限 {_SANDBOX_MAX_HOST_USAGE:.0f}%)"
        return True, ""
    except ImportError:
        # psutil 不可用时跳过检查
        return True, ""


def _build_labels(task_name: str, timeout: int, user_id: str = "") -> Dict[str, str]:
    labels = {
        _LABEL_KEY: _LABEL_VAL,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "max_runtime": str(timeout),
        "task_name": task_name or "sandbox_task",
    }
    if user_id:
        labels["user_id"] = str(user_id)
    return labels


# ── Action 实现 ───────────────────────────────────────────────────────────────

def _action_exec(params: Dict[str, Any]) -> Dict[str, Any]:
    """在已有容器内执行命令（docker exec）"""
    command = params.get("command", "").strip()
    if not command:
        return {"success": False, "error": "缺少 command 参数"}

    container = params.get("container", "").strip()
    if not container:
        return {"success": False, "error": "缺少 container 参数，请指定要执行命令的容器名。如需在 toolbox 执行，请用 action=toolbox"}

    background = params.get("background", False)
    workdir = params.get("workdir", "/")
    env_extra = params.get("env", {}) or {}

    client = _get_docker_client()
    try:
        container_obj = client.containers.get(container)
    except Exception as e:
        return {"success": False, "error": f"容器 {container} 不存在: {e}"}

    if background:
        full_cmd = f"cd {workdir} && {command}"
        container_obj.exec_run(
            ["bash", "-c", full_cmd],
            detach=True,
            environment=env_extra or None,
        )
        return {
            "success": True,
            "action": "exec",
            "exec_mode": "docker_exec",
            "container": container,
            "background": True,
            "message": f"命令已在容器 {container} 中后台执行",
        }
    else:
        full_cmd = f"cd {workdir} && {command}"
        exit_code, output = container_obj.exec_run(
            ["bash", "-c", full_cmd],
            environment=env_extra or None,
        )
        text = output.decode("utf-8", errors="replace") if output else ""
        return {
            "success": exit_code == 0,
            "action": "exec",
            "exec_mode": "docker_exec",
            "container": container,
            "returncode": exit_code,
            "stdout": text[-2000:],
        }


def _action_logs(params: Dict[str, Any]) -> Dict[str, Any]:
    """获取容器日志或容器内文件日志"""
    container = params.get("container", "").strip()
    log_path = params.get("log_path", "")
    tail = params.get("tail", 50)
    grep = params.get("grep", "")

    if not container:
        return {"success": False, "error": "缺少 container 参数"}

    client = _get_docker_client()
    try:
        container_obj = client.containers.get(container)
    except Exception as e:
        return {"success": False, "error": f"容器 {container} 不存在: {e}"}

    if log_path:
        # 从容器内文件读日志
        grep_cmd = f" | grep -i '{grep}'" if grep else ""
        exit_code, output = container_obj.exec_run(
            ["bash", "-c", f"tail -n {tail} {log_path}{grep_cmd}"],
        )
        text = output.decode("utf-8", errors="replace") if output else ""
        lines = text.strip().split("\n") if text.strip() else []
        return {
            "success": True,
            "action": "logs",
            "container": container,
            "log_path": log_path,
            "lines": lines,
        }
    else:
        # docker logs
        logs = container_obj.logs(tail=tail).decode("utf-8", errors="replace")
        lines = logs.strip().split("\n") if logs.strip() else []
        if grep:
            pattern = re.compile(grep, re.IGNORECASE)
            lines = [l for l in lines if pattern.search(l)]
        return {
            "success": True,
            "action": "logs",
            "container": container,
            "lines": lines,
        }


def _action_ps(params: Dict[str, Any]) -> Dict[str, Any]:
    """查看容器内进程"""
    container = params.get("container", "").strip()
    grep = params.get("grep", "")

    if not container:
        return {"success": False, "error": "缺少 container 参数"}

    client = _get_docker_client()
    try:
        container_obj = client.containers.get(container)
        top = container_obj.top()
        processes = []
        for proc in top.get("Processes", []):
            processes.append({
                "pid": proc[1] if len(proc) > 1 else "",
                "command": proc[-1] if proc else "",
            })
        if grep:
            pattern = re.compile(grep, re.IGNORECASE)
            processes = [p for p in processes if pattern.search(p.get("command", ""))]
        return {
            "success": True,
            "action": "ps",
            "container": container,
            "processes": processes,
            "count": len(processes),
        }
    except Exception as e:
        return {"success": False, "error": f"查看容器进程失败: {e}"}


def _action_stop(params: Dict[str, Any]) -> Dict[str, Any]:
    """停止容器或容器内进程"""
    container = params.get("container", "").strip()
    pid = params.get("pid")
    sig = params.get("signal", "SIGTERM")

    if not container:
        return {"success": False, "error": "缺少 container 参数"}

    client = _get_docker_client()
    try:
        container_obj = client.containers.get(container)
    except Exception as e:
        return {"success": False, "error": f"容器 {container} 不存在: {e}"}

    if pid:
        # 向容器内指定 PID 发送信号
        sig_num = getattr(signal, sig, signal.SIGTERM)
        try:
            container_obj.exec_run(["kill", f"-{sig_num}", str(pid)])
            return {
                "success": True,
                "action": "stop",
                "container": container,
                "pid": pid,
                "signal": sig,
            }
        except Exception as e:
            return {"success": False, "error": f"停止容器内进程失败: {e}"}
    else:
        # 停止整个容器（安全检查：只允许停止 agent 创建的沙箱容器）
        if container_obj.labels.get(_LABEL_KEY) != _LABEL_VAL:
            return {"success": False, "error": f"容器 {container} 不是 agent 创建的沙箱，拒绝停止"}
        container_obj.stop(timeout=10)
        return {
            "success": True,
            "action": "stop",
            "container": container,
            "message": f"容器 {container} 已停止",
        }


def _action_run(params: Dict[str, Any]) -> Dict[str, Any]:
    """启动新沙箱容器"""
    image = params.get("image", "").strip()
    command = params.get("command", "").strip()
    if not image:
        return {"success": False, "error": "缺少 image 参数"}

    client = _get_docker_client()

    # 检查容器数量上限
    running = client.containers.list(
        filters={"label": f"{_LABEL_KEY}={_LABEL_VAL}", "status": "running"}
    )
    if len(running) >= _SANDBOX_MAX_CONTAINERS:
        return {
            "success": False,
            "error": f"沙箱容器数量已达上限({_SANDBOX_MAX_CONTAINERS})，请先停止已有任务或执行 cleanup",
        }

    # 检查宿主机资源
    ok, msg = _check_host_resources()
    if not ok:
        return {"success": False, "error": msg}

    # 资源限制（截断到 MAX）
    mem_limit = params.get("mem_limit", _SANDBOX_DEFAULT_MEM)
    mem_bytes = min(_parse_mem_bytes(mem_limit), _parse_mem_bytes(_SANDBOX_MAX_MEM))
    cpus = min(float(params.get("cpus", _SANDBOX_DEFAULT_CPUS)), _SANDBOX_MAX_CPUS)
    timeout = min(int(params.get("timeout", _SANDBOX_DEFAULT_TIMEOUT)), _SANDBOX_MAX_TIMEOUT)
    auto_remove = params.get("auto_remove", True)
    name = params.get("name") or f"sandbox_{uuid.uuid4().hex[:8]}"
    task_name = params.get("task_name", name)
    env_extra = params.get("env", {}) or {}
    volumes = params.get("volumes", {}) or {}
    network = params.get("network", "")

    try:
        run_kwargs = {
            "image": image,
            "command": command if command else None,
            "name": name,
            "detach": True,
            "mem_limit": mem_bytes,
            "nano_cpus": int(cpus * 1e9),
            "labels": _build_labels(task_name, timeout),
            "environment": env_extra or None,
            "auto_remove": auto_remove,
        }
        if volumes:
            run_kwargs["volumes"] = {
                host: {"bind": cont, "mode": "rw"}
                for host, cont in volumes.items()
            }
        if network:
            run_kwargs["network"] = network

        container_obj = client.containers.run(**run_kwargs)

        # 注册到 TaskService
        task_id = _register_task(
            name=task_name,
            exec_mode="docker",
            command=command or f"run {image}",
            container_id=container_obj.id[:12],
            container_name=name,
            created_by=params.get("created_by", ""),
            session_id=params.get("session_id", ""),
        )

        result = {
            "success": True,
            "action": "run",
            "container_id": container_obj.id[:12],
            "container_name": name,
            "image": image,
            "mem_limit": mem_limit,
            "cpus": cpus,
            "timeout": timeout,
            "auto_remove": auto_remove,
            "message": f"沙箱容器 {name} 已启动，内存 {mem_limit}，CPU {cpus} 核，最长 {timeout} 秒",
        }
        if task_id:
            result["task_id"] = task_id
        return result
    except Exception as e:
        return {"success": False, "error": f"启动容器失败: {e}"}


def _action_inspect(params: Dict[str, Any]) -> Dict[str, Any]:
    """查看容器详情"""
    container = params.get("container", "")
    if not container:
        return {"success": False, "error": "缺少 container 参数"}

    client = _get_docker_client()
    try:
        container_obj = client.containers.get(container)
        info = container_obj.attrs
        state = info.get("State", {})
        config = info.get("Config", {})
        return {
            "success": True,
            "action": "inspect",
            "container": container,
            "name": info.get("Name", "").lstrip("/"),
            "status": state.get("Status", ""),
            "running": state.get("Running", False),
            "started_at": state.get("StartedAt", ""),
            "finished_at": state.get("FinishedAt", ""),
            "image": config.get("Image", ""),
            "labels": config.get("Labels", {}),
            "exit_code": state.get("ExitCode", 0),
        }
    except Exception as e:
        return {"success": False, "error": f"查看容器详情失败: {e}"}


def _action_pull(params: Dict[str, Any]) -> Dict[str, Any]:
    """拉取镜像"""
    image = params.get("image", "").strip()
    if not image:
        return {"success": False, "error": "缺少 image 参数"}

    client = _get_docker_client()
    try:
        client.images.pull(image)
        return {
            "success": True,
            "action": "pull",
            "image": image,
            "message": f"镜像 {image} 拉取成功",
        }
    except Exception as e:
        return {"success": False, "error": f"拉取镜像失败: {e}"}


def _action_cleanup(params: Dict[str, Any]) -> Dict[str, Any]:
    """清理 agent 创建的沙箱容器"""
    client = _get_docker_client()
    try:
        containers = client.containers.list(
            filters={"label": f"{_LABEL_KEY}={_LABEL_VAL}"},
            all=True,
        )
    except Exception as e:
        return {"success": False, "error": f"列出容器失败: {e}"}

    now = datetime.now(timezone.utc)
    removed = []
    kept = []

    for c in containers:
        c_name = c.name
        labels = c.labels or {}
        created_at_str = labels.get("created_at", "")
        max_runtime = int(labels.get("max_runtime", "3600"))

        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        except Exception:
            created_at = now

        if c.status == "exited":
            # 已停止：检查保留窗口
            finished_at_str = c.attrs.get("State", {}).get("FinishedAt", "")
            try:
                finished_at = datetime.fromisoformat(finished_at_str.replace("Z", "+00:00"))
                elapsed = (now - finished_at).total_seconds()
            except Exception:
                elapsed = _SANDBOX_EXITED_RETAIN + 1  # 解析失败默认清理

            if elapsed > _SANDBOX_EXITED_RETAIN:
                c.remove()
                removed.append({"name": c_name, "status": "exited", "action": "removed"})
            else:
                kept.append({"name": c_name, "status": "exited", "retain_seconds": int(_SANDBOX_EXITED_RETAIN - elapsed)})

        elif c.status == "running":
            runtime = (now - created_at).total_seconds()
            if runtime > max_runtime:
                # 超时：先 SIGTERM，再 SIGKILL
                c.stop(timeout=10)
                c.remove()
                removed.append({"name": c_name, "status": "timeout", "runtime": f"{runtime/60:.0f}min", "action": "stopped+removed"})
            else:
                kept.append({"name": c_name, "status": "running", "runtime": f"{runtime/60:.0f}min"})

    return {
        "success": True,
        "action": "cleanup",
        "removed": len(removed),
        "kept": len(kept),
        "details": removed,
        "kept_details": kept,
    }


# ── 入口 ──────────────────────────────────────────────────────────────────────

_ACTIONS = {
    # toolbox SSH actions（推荐首选，完整 Linux 环境）
    "toolbox": None,          # 特殊处理，转发到 _action_toolbox（async）
    # 本容器 / 已有容器 / 新建容器
    "exec": _action_exec,
    "logs": _action_logs,
    "ps": _action_ps,
    "stop": _action_stop,
    "run": _action_run,
    "inspect": _action_inspect,
    "pull": _action_pull,
    "cleanup": _action_cleanup,
}


class DockerOperatorSkill:
    @property
    def name(self) -> str:
        return "docker_operator"

    @property
    def description(self) -> str:
        return "在指定环境中执行命令或管理容器生命周期（含 toolbox SSH 执行环境）"

    async def execute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        action = (params.get("action") or "").strip().lower()
        if not action:
            return {"success": False, "error": "缺少 action 参数"}

        # toolbox 是 async action，单独处理
        if action == "toolbox":
            try:
                return await _action_toolbox(params)
            except Exception as e:
                return {"success": False, "error": str(e), "exec_env": "toolbox"}

        handler = _ACTIONS.get(action)
        if not handler:
            return {
                "success": False,
                "error": f"不支持的 action: {action}，支持: {[k for k in _ACTIONS if k != 'toolbox'] + ['toolbox']}",
            }
        try:
            return handler(params)
        except Exception as e:
            return {"success": False, "error": str(e)}


def _main() -> None:
    params: Dict[str, Any] = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read().strip()
            if raw:
                params = json.loads(raw)
        except Exception:
            params = {}

    parser = argparse.ArgumentParser(description="Docker Operator Skill")
    parser.add_argument("--action", type=str)
    parser.add_argument("--command", type=str)
    parser.add_argument("--container", type=str)
    parser.add_argument("--log-path", dest="log_path", type=str)
    parser.add_argument("--tail", type=int)
    parser.add_argument("--grep", type=str)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--image", type=str)
    parser.add_argument("--background", type=bool, default=None)
    args = parser.parse_args()
    for key, value in vars(args).items():
        if value is not None:
            params[key] = value

    async def _run() -> None:
        skill = DockerOperatorSkill()
        result = await skill.execute(params)
        print(json.dumps(result, ensure_ascii=False, default=str))

    asyncio.run(_run())


if __name__ == "__main__":
    _main()
