"""
LLM Provider — 统一 LLM 调用层（自包含，无 app 层依赖）

包含两层能力：

1. LiteLLMProvider (class)
   AgentLoop 核心循环使用，支持全 messages 数组 + tools 的流式/非流式调用。
   从 V4Config 构建端点，内置 circuit-breaker failover。

2. LLMEndpointProvider (singleton)
   辅助模块 / Skill 层统一入口（call_anthropic_api / stream_anthropic_api）。
   从 JSON 配置加载 N 个端点，支持：
   - 角色提升 failover（累积失败 → 自动提升备用端点）
   - 竞速模式（asyncio.FIRST_COMPLETED）
   - HTTP 连接池（httpx.AsyncClient 按 base_url 复用）
   - 429 自动重试
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from loguru import logger

from .message_types import LLMResponse, ToolCallRequest


# ═══════════════════════════════════════════════════════════════════════
# § 1  环境变量插值 & JSON 配置加载
# ═══════════════════════════════════════════════════════════════════════

_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)}")


def _interpolate_env(value: str) -> str:
    """将 ${VAR_NAME} 占位符替换为 os.getenv() 的值，未设置则返回空字符串"""
    def _replace(match):
        return os.getenv(match.group(1), "")
    return _ENV_VAR_PATTERN.sub(_replace, value)


def _load_json_endpoints(path: str) -> Optional[dict]:
    """
    加载 JSON 端点配置文件并做环境变量插值。

    Returns:
        解析后的配置 dict，文件不存在或解析失败返回 None
    """
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[LLMProvider] Failed to load {path}: {e}")
        return None

    for ep in raw.get("endpoints", []):
        for key in ("base_url", "api_key", "model", "small_fast_model"):
            val = ep.get(key, "")
            if isinstance(val, str) and "${" in val:
                ep[key] = _interpolate_env(val)
    return raw


# ═══════════════════════════════════════════════════════════════════════
# § 2  性能配置（懒加载）
# ═══════════════════════════════════════════════════════════════════════

def _load_performance_config() -> dict:
    """加载 agent_core/config/performance.json，失败返回空 dict"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "config", "performance.json")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[LLMProvider] Failed to load performance.json: {e}")
    return {}


_perf_config: Optional[dict] = None


def _get_perf_config() -> dict:
    global _perf_config
    if _perf_config is None:
        _perf_config = _load_performance_config()
    return _perf_config


# ═══════════════════════════════════════════════════════════════════════
# § 3  HTTP 连接池
# ═══════════════════════════════════════════════════════════════════════

_http_pools: Dict[str, "httpx.AsyncClient"] = {}
_pool_lock = threading.Lock()


async def _aiter_lines_with_timeout(response, idle_timeout: float = 60.0):
    """
    逐行迭代 httpx streaming response，每行等待加 idle_timeout 保护。
    防止 stream 中途静默导致 aiter_lines() 永久挂死。
    """
    aiter = response.aiter_lines().__aiter__()
    while True:
        try:
            line = await asyncio.wait_for(aiter.__anext__(), timeout=idle_timeout)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"LLM stream idle timeout after {idle_timeout}s (no data received)"
            )
        yield line


def _get_http_client(base_url: str, timeout: float) -> "httpx.AsyncClient":
    """
    获取或创建 HTTP 连接池（按 base_url 分组）

    配置从 performance.json → http_pool 读取。
    pool 未启用时返回一次性 client。
    自动检测并替换不可用的 client（如 event loop 切换后的残留连接）。
    """
    import httpx

    cfg = _get_perf_config().get("http_pool", {})
    if not cfg.get("enabled", True):
        return httpx.AsyncClient(timeout=timeout)

    key = base_url.rstrip("/")
    with _pool_lock:
        client = _http_pools.get(key)
        needs_create = client is None or client.is_closed

        if client is not None and not needs_create:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    needs_create = True
            except RuntimeError:
                needs_create = True

        if needs_create:
            max_conn = cfg.get("max_connections", 20)
            max_keepalive = cfg.get("max_keepalive", 10)
            keepalive_expiry = cfg.get("keepalive_expiry_seconds", 30)
            _http_pools[key] = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_connections=max_conn,
                    max_keepalive_connections=max_keepalive,
                    keepalive_expiry=float(keepalive_expiry),
                ),
                timeout=httpx.Timeout(timeout, connect=5.0),
            )
            logger.info(
                f"[HTTPPool] Created pool for {key}: "
                f"max_conn={max_conn}, keepalive={max_keepalive}"
            )
            client = _http_pools[key]
    return client


def _invalidate_pool(base_url: str):
    """清除指定 base_url 的连接池（竞速取消后调用，避免残留污染下次请求）"""
    key = base_url.rstrip("/")
    with _pool_lock:
        client = _http_pools.pop(key, None)
        if client:
            logger.debug(f"[HTTPPool] Invalidated pool for {key} (race cancel cleanup)")


async def shutdown_http_pools():
    """进程退出时关闭所有连接池"""
    with _pool_lock:
        for key, client in list(_http_pools.items()):
            try:
                await client.aclose()
                logger.info(f"[HTTPPool] Closed pool for {key}")
            except Exception:
                pass
        _http_pools.clear()


# ═══════════════════════════════════════════════════════════════════════
# § 4  LLMEndpoint & CircuitState 数据结构
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class LLMEndpoint:
    """单个 LLM 端点配置（LLMEndpointProvider 使用）"""
    name: str
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    small_fast_model: str = ""
    priority: int = 0
    enabled: bool = True
    context_window: int = 0  # 0 = 未声明，由 ContextWindowGuard fallback 处理

    def to_env_vars(self) -> dict:
        env_vars = {}
        if self.api_key:
            env_vars["ANTHROPIC_AUTH_TOKEN"] = self.api_key
        if self.base_url:
            env_vars["ANTHROPIC_BASE_URL"] = self.base_url
        if self.model:
            env_vars["ANTHROPIC_MODEL"] = self.model
        env_vars["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        return env_vars

    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url)


@dataclass
class _EndpointCircuitState:
    """LLMEndpointProvider 的熔断器状态（角色提升版）"""
    failure_count: int = 0
    last_failure_time: float = 0.0
    recovery_success_count: int = 0


# ═══════════════════════════════════════════════════════════════════════
# § 5  LLMEndpointProvider — 进程级单例，N 端点 + failover + race mode
# ═══════════════════════════════════════════════════════════════════════

class LLMEndpointProvider:
    """
    LLM 端点管理器（进程级单例）

    Failover 策略:
    - 即时 failover: 当前端点失败立即 fallback 到下一个（用户无感知）
    - 角色提升: 首选端点累积失败 >= threshold 次 → 下一个端点自动提升为默认优先
    - 成功恢复: 被降级端点作为 fallback 累积成功 >= threshold 次后恢复原始优先级
    - max_retry_endpoints: 限制每次请求最多尝试几个端点
    """
    _instance: Optional["LLMEndpointProvider"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._endpoints: List[LLMEndpoint] = []
        self._max_retry_endpoints: int = 2
        self._threshold: int = 3
        self._states: Dict[str, _EndpointCircuitState] = {}
        self._promoted_endpoint: Optional[str] = None
        self._demoted_endpoint: Optional[str] = None

    @classmethod
    def get_instance(cls) -> "LLMEndpointProvider":
        """获取或创建单例（从 JSON 配置或 Settings 初始化）"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    inst = cls()
                    inst._init_from_settings()
                    cls._instance = inst
        return cls._instance

    @classmethod
    def reset(cls):
        """重置单例（测试用）"""
        with cls._lock:
            cls._instance = None

    def _resolve_json_path(self) -> str:
        """解析 JSON 配置文件路径（优先从环境变量读，否则用默认）"""
        custom = os.getenv("LLM_ENDPOINTS_CONFIG_PATH", "")
        if custom and os.path.exists(custom):
            return custom

        # 默认路径：agent_core/llm_endpoints.json
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        agent_core_path = os.path.join(base, "llm_endpoints.json")
        if os.path.exists(agent_core_path):
            return agent_core_path
        project_root = os.path.dirname(base)
        return os.path.join(project_root, "config", "llm_endpoints.json")

    def _init_from_settings(self):
        """从 JSON 配置或 app.config.settings 读取端点配置"""
        json_path = self._resolve_json_path()
        json_cfg = _load_json_endpoints(json_path)
        if json_cfg and json_cfg.get("endpoints"):
            self._init_from_json(json_cfg, json_path)
        else:
            self._init_from_env_compat()

    def _init_from_json(self, cfg: dict, json_path: str):
        """从 JSON 配置初始化 N 个端点"""
        self._max_retry_endpoints = cfg.get("max_retry_endpoints", 2)
        self._threshold = cfg.get("failover_threshold", 3)

        for ep_cfg in cfg.get("endpoints", []):
            if not ep_cfg.get("enabled", True):
                continue
            ep = LLMEndpoint(
                name=ep_cfg.get("name", "unknown"),
                base_url=ep_cfg.get("base_url", ""),
                api_key=ep_cfg.get("api_key", ""),
                model=ep_cfg.get("model", ""),
                small_fast_model=ep_cfg.get("small_fast_model", ""),
                priority=ep_cfg.get("priority", 999),
                enabled=True,
                context_window=ep_cfg.get("context_window", 0),
            )
            if ep.is_configured():
                self._endpoints.append(ep)

        self._endpoints.sort(key=lambda x: x.priority)
        for ep in self._endpoints:
            self._states[ep.name] = _EndpointCircuitState()

        ep_summary = ", ".join(
            f"{ep.name}(cw={ep.context_window or 'unset'})" for ep in self._endpoints
        )
        logger.info(
            f"[LLMProvider] JSON mode: {len(self._endpoints)} endpoints loaded "
            f"from {json_path}, max_retry={self._max_retry_endpoints}, "
            f"order=[{ep_summary}]"
        )

    def _init_from_env_compat(self):
        """兼容模式：从环境变量构建 primary+backup 两个端点（无 app 依赖）"""
        try:
            primary = LLMEndpoint(
                name="primary",
                base_url=os.getenv("ANTHROPIC_BASE_URL", ""),
                api_key=os.getenv("ANTHROPIC_AUTH_TOKEN", ""),
                model=os.getenv("ANTHROPIC_MODEL", ""),
                small_fast_model=os.getenv("ANTHROPIC_SMALL_FAST_MODEL", ""),
                priority=1,
            )
            if primary.is_configured():
                self._endpoints.append(primary)

            backup = LLMEndpoint(
                name="backup",
                base_url=os.getenv("ANTHROPIC_BASE_URL_BACKUP", ""),
                api_key=os.getenv("ANTHROPIC_AUTH_TOKEN_BACKUP", ""),
                model=os.getenv("ANTHROPIC_MODEL_BACKUP", ""),
                small_fast_model=os.getenv("ANTHROPIC_SMALL_FAST_MODEL_BACKUP", ""),
                priority=2,
            )
            if backup.is_configured():
                self._endpoints.append(backup)

            self._threshold = int(os.getenv("LLM_FAILOVER_THRESHOLD", "3"))
            for ep in self._endpoints:
                self._states[ep.name] = _EndpointCircuitState()

            logger.info(
                f"[LLMProvider] Compat mode: {len(self._endpoints)} endpoints, "
                f"primary={primary.base_url}, "
                f"backup={'configured' if backup.is_configured() else 'not configured'}, "
                f"threshold={self._threshold}"
            )
        except Exception as e:
            logger.error(f"[LLMProvider] Init failed: {e}")

    def get_endpoints_ordered(self) -> List[LLMEndpoint]:
        """
        获取端点列表（按优先级排序 + 提升调整 + max_retry 截断）
        """
        if not self._endpoints:
            return []
        ordered = list(self._endpoints)
        if self._promoted_endpoint:
            for i, ep in enumerate(ordered):
                if ep.name == self._promoted_endpoint and i > 0:
                    promoted = ordered.pop(i)
                    ordered.insert(0, promoted)
                    break
        return ordered[:self._max_retry_endpoints]

    def get_all_endpoints(self) -> List[LLMEndpoint]:
        return list(self._endpoints)

    def get_active_context_window(self) -> int:
        """返回当前最优先 endpoint 声明的 context_window（0 = 未声明）"""
        endpoints = self.get_endpoints_ordered()
        if endpoints:
            return endpoints[0].context_window
        return 0

    def get_next_endpoint_after(self, current_name: str) -> Optional[LLMEndpoint]:
        ordered = list(self._endpoints)
        if self._promoted_endpoint:
            for i, ep in enumerate(ordered):
                if ep.name == self._promoted_endpoint and i > 0:
                    promoted = ordered.pop(i)
                    ordered.insert(0, promoted)
                    break
        for i, ep in enumerate(ordered):
            if ep.name == current_name and i + 1 < len(ordered):
                return ordered[i + 1]
        return None

    def report_failure(self, endpoint_name: str):
        """报告端点失败，累积达到 threshold 则提升下一个端点为默认优先"""
        state = self._states.get(endpoint_name)
        if state:
            state.failure_count += 1
            state.last_failure_time = time.time()
            state.recovery_success_count = 0
            logger.warning(
                f"[LLMProvider] Endpoint '{endpoint_name}' failure "
                f"#{state.failure_count} (threshold={self._threshold})"
            )
            if state.failure_count >= self._threshold and not self._promoted_endpoint:
                next_ep = self.get_next_endpoint_after(endpoint_name)
                if next_ep:
                    self._promoted_endpoint = next_ep.name
                    self._demoted_endpoint = endpoint_name
                    logger.warning(
                        f"[LLMProvider] '{endpoint_name}' failed {state.failure_count} times, "
                        f"promoting '{next_ep.name}' as default endpoint"
                    )

    def report_success(self, endpoint_name: str):
        """报告端点成功，被降级端点累积 threshold 次后恢复原始优先级"""
        state = self._states.get(endpoint_name)
        if not state:
            return
        if self._demoted_endpoint and endpoint_name == self._demoted_endpoint:
            state.recovery_success_count += 1
            logger.info(
                f"[LLMProvider] Demoted endpoint '{endpoint_name}' success "
                f"#{state.recovery_success_count}/{self._threshold}"
            )
            if state.recovery_success_count >= self._threshold:
                logger.info(
                    f"[LLMProvider] '{endpoint_name}' recovered, restoring original priority"
                )
                state.failure_count = 0
                state.recovery_success_count = 0
                self._promoted_endpoint = None
                self._demoted_endpoint = None
        else:
            if state.failure_count > 0:
                state.failure_count = 0
                state.recovery_success_count = 0

    @property
    def primary(self) -> Optional[LLMEndpoint]:
        return self._endpoints[0] if self._endpoints else None

    @property
    def backup(self) -> Optional[LLMEndpoint]:
        return self._endpoints[1] if len(self._endpoints) > 1 else None

    def has_backup(self) -> bool:
        return len(self._endpoints) >= 2


# ═══════════════════════════════════════════════════════════════════════
# § 6  单端点调用 & 统一辅助函数（call_anthropic_api / stream_anthropic_api）
# ═══════════════════════════════════════════════════════════════════════

async def _call_single_endpoint(
    ep: LLMEndpoint,
    prompt: str,
    use_small_fast: bool = False,
    system: Optional[str] = None,
    max_tokens: int = 1024,
    timeout: float = 30.0,
    temperature: Optional[float] = None,
) -> str:
    """对单个端点发起非流式 LLM 调用（内部方法）"""
    import httpx

    model = ep.small_fast_model if (use_small_fast and ep.small_fast_model) else ep.model
    if not model:
        raise ValueError(f"No model configured for endpoint '{ep.name}'")

    url = f"{ep.base_url.rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": ep.api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    if temperature is not None:
        body["temperature"] = temperature

    cfg = _get_perf_config().get("http_pool", {})
    pool_enabled = cfg.get("enabled", True)

    # 若池化 client 因竞速取消残留而损坏，清除后重试一次
    resp = None
    for _attempt in range(2):
        client = _get_http_client(ep.base_url, timeout)
        try:
            if pool_enabled:
                resp = await client.post(url, headers=headers, json=body, timeout=timeout)
            else:
                async with httpx.AsyncClient(timeout=timeout) as _c:
                    resp = await _c.post(url, headers=headers, json=body)
            break
        except RuntimeError as e:
            if "Event loop is closed" in str(e) and _attempt == 0 and pool_enabled:
                logger.warning(f"[HTTPPool] Stale client for {ep.base_url}, recreating")
                _invalidate_pool(ep.base_url)
                continue
            raise

    # 429 rate limit 自动重试
    if resp.status_code == 429:
        retry_after_hdr = resp.headers.get("retry-after")
        wait_time = float(retry_after_hdr) if retry_after_hdr and retry_after_hdr.isdigit() else 1.0
        for retry_i in range(2):
            backoff = wait_time * (retry_i + 1)
            logger.warning(
                f"[LLMProvider] 429 on {ep.name}, retry {retry_i + 1}/2 "
                f"after {backoff:.1f}s"
            )
            await asyncio.sleep(backoff)
            client = _get_http_client(ep.base_url, timeout)
            resp = await client.post(url, headers=headers, json=body, timeout=timeout)
            if resp.status_code != 429:
                break
        if resp.status_code == 429:
            raise RuntimeError(f"429 Too Many Requests on {ep.name} (exhausted retries)")

    resp.raise_for_status()
    data = resp.json()
    texts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(texts)


async def call_anthropic_api(
    prompt: str,
    use_small_fast: bool = False,
    system: Optional[str] = None,
    max_tokens: int = 1024,
    timeout: float = 30.0,
    temperature: Optional[float] = None,
) -> str:
    """
    非流式调用 Anthropic Messages API（带 failover + 竞速模式 + 连接池）

    辅助模块 / Skill 层统一入口，不依赖全 messages 数组。
    """
    provider = LLMEndpointProvider.get_instance()
    endpoints = provider.get_endpoints_ordered()

    if not endpoints:
        logger.error("[LLMProvider] No endpoints configured")
        return ""

    # 竞速模式
    race_cfg = _get_perf_config().get("llm_race", {})
    race_enabled = race_cfg.get("enabled", False)
    race_concurrency = race_cfg.get("concurrency", 2)
    race_timeout = race_cfg.get("timeout_seconds", 20)

    if race_enabled and len(endpoints) >= 2:
        race_start = time.time()
        race_eps = endpoints[:race_concurrency]
        tasks = {
            asyncio.create_task(
                _call_single_endpoint(ep, prompt, use_small_fast, system,
                                      max_tokens, race_timeout, temperature)
            ): ep
            for ep in race_eps
        }
        try:
            done, pending = await asyncio.wait(
                tasks.keys(),
                return_when=asyncio.FIRST_COMPLETED,
                timeout=race_timeout,
            )
            for t in pending:
                t.cancel()
                t.add_done_callback(lambda _t: _t.exception() if not _t.cancelled() else None)
                _invalidate_pool(tasks[t].base_url)
            for t in done:
                ep = tasks[t]
                if t.exception() is None:
                    provider.report_success(ep.name)
                    elapsed = (time.time() - race_start) * 1000
                    logger.info(
                        f"[LLMRace] Racing {len(race_eps)} endpoints, "
                        f"winner: {ep.name} in {elapsed:.0f}ms"
                    )
                    return t.result()
                else:
                    provider.report_failure(ep.name)
                    logger.warning(
                        f"[LLMRace] Endpoint '{ep.name}' failed: "
                        f"{type(t.exception()).__name__}: {t.exception()}"
                    )
        except asyncio.TimeoutError:
            for t in tasks:
                t.cancel()
                t.add_done_callback(lambda _t: _t.exception() if not _t.cancelled() else None)
                _invalidate_pool(tasks[t].base_url)
            logger.warning("[LLMRace] All race tasks timed out")

        # 竞速全部失败，继续尝试剩余端点
        remaining = endpoints[race_concurrency:]
        for ep in remaining:
            try:
                result = await _call_single_endpoint(
                    ep, prompt, use_small_fast, system, max_tokens, timeout, temperature)
                provider.report_success(ep.name)
                return result
            except Exception as e:
                provider.report_failure(ep.name)
                logger.warning(f"[LLMProvider] Fallback '{ep.name}' failed: {e}")

        raise RuntimeError("All LLM endpoints failed (race + fallback)")

    # 串行 failover 模式
    last_error = None
    for ep in endpoints:
        try:
            result = await _call_single_endpoint(
                ep, prompt, use_small_fast, system, max_tokens, timeout, temperature)
            provider.report_success(ep.name)
            return result
        except Exception as e:
            provider.report_failure(ep.name)
            last_error = e
            logger.warning(f"[LLMProvider] call_anthropic_api failed on '{ep.name}': {e}")

    raise RuntimeError(f"All LLM endpoints failed: {last_error}")


async def stream_anthropic_api(
    prompt: str,
    model_override: Optional[str] = None,
    system: Optional[str] = None,
    max_tokens: int = 32768,
    timeout: float = 120.0,
    skip_endpoints: Optional[List[str]] = None,
) -> AsyncIterator[str]:
    """
    流式调用 Anthropic Messages API（带 failover）

    Args:
        skip_endpoints: 跳过的端点名称列表（如 ["primary"]，用于 Policy Error 备用重试）

    Yields:
        文本 chunk
    """
    provider = LLMEndpointProvider.get_instance()
    endpoints = provider.get_endpoints_ordered()

    if not endpoints:
        logger.error("[LLMProvider] No endpoints configured for streaming")
        return

    if skip_endpoints:
        endpoints = [ep for ep in endpoints if ep.name not in skip_endpoints]
        if not endpoints:
            logger.error("[LLMProvider] No endpoints left after skipping")
            return

    last_error = None
    for ep in endpoints:
        model = model_override or ep.model
        if not model:
            continue

        url = f"{ep.base_url.rstrip('/')}/v1/messages"
        headers = {
            "x-api-key": ep.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system

        try:
            client = _get_http_client(ep.base_url, timeout)
            async with client.stream("POST", url, headers=headers, json=body,
                                     timeout=timeout) as resp:
                if resp.status_code == 429:
                    raise RuntimeError(
                        f"429 Too Many Requests on {ep.name} "
                        f"(retry-after={resp.headers.get('retry-after', 'unknown')})"
                    )
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    raise RuntimeError(
                        f"LLM API returned {resp.status_code}: {error_body[:200]}"
                    )

                provider.report_success(ep.name)

                _stream_idle = _get_perf_config().get("llm_stream", {}).get("idle_timeout_seconds", 30.0)
                async for line in _aiter_lines_with_timeout(resp, idle_timeout=min(timeout, _stream_idle)):
                    if line.startswith("data: "):
                        data_str = line[6:]
                    elif line.startswith("data:"):
                        data_str = line[5:]
                    else:
                        continue
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        event_data = json.loads(data_str)
                        if event_data.get("type") == "content_block_delta":
                            text = event_data.get("delta", {}).get("text", "")
                            if text:
                                yield text
                    except json.JSONDecodeError:
                        continue
            return  # 成功，退出

        except Exception as e:
            provider.report_failure(ep.name)
            last_error = e
            logger.warning(f"[LLMProvider] stream_anthropic_api failed on '{ep.name}': {e}")

    raise RuntimeError(f"All LLM stream endpoints failed: {last_error}")


def get_small_fast_llm_call():
    """
    获取 small_fast 版本的 LLM 调用函数。

    返回 async callable: (prompt: str) -> str
    使用 small_fast_model，适用于知识蒸馏、反思、偏好学习等辅助场景。
    配置: performance.json → small_fast_auxiliary.enabled
    """
    cfg = _get_perf_config().get("small_fast_auxiliary", {})
    enabled = cfg.get("enabled", True)

    async def _small_fast_call(prompt: str) -> str:
        return await call_anthropic_api(
            prompt=prompt,
            use_small_fast=enabled,
            max_tokens=2048,
            timeout=60.0,
        )

    return _small_fast_call


def get_anthropic_client_config(use_llm_config: bool = False) -> dict:
    """
    获取当前优先端点的 Anthropic 客户端配置（供 Skill 层构造 AsyncAnthropic 客户端）

    Returns:
        {"base_url": str, "api_key": str, "model": str}
    """
    if use_llm_config:
        llm_key = os.getenv("LLM_API_KEY", "")
        llm_url = os.getenv("LLM_BASE_URL", "")
        llm_model = os.getenv("LLM_MODEL", "")
        if llm_key and llm_url:
            return {"base_url": llm_url, "api_key": llm_key,
                    "model": llm_model or "glm-4.7"}

    provider = LLMEndpointProvider.get_instance()
    endpoints = provider.get_endpoints_ordered()
    if endpoints:
        ep = endpoints[0]
        return {"base_url": ep.base_url, "api_key": ep.api_key, "model": ep.model}

    try:
        return {
            "base_url": os.getenv("ANTHROPIC_BASE_URL", ""),
            "api_key": os.getenv("ANTHROPIC_AUTH_TOKEN", ""),
            "model": os.getenv("ANTHROPIC_MODEL", ""),
        }
    except Exception:
        return {"base_url": "", "api_key": "", "model": ""}


# ═══════════════════════════════════════════════════════════════════════
# § 7  LiteLLMProvider — AgentLoop 核心循环专用（全 messages + tools）
# ═══════════════════════════════════════════════════════════════════════

# LiteLLMProvider 内部使用的简单端点描述（与 LLMEndpoint 独立）
@dataclass
class _Endpoint:
    name: str
    base_url: str
    api_key: str
    model: str
    small_fast_model: str = ""
    priority: int = 0
    support_image: bool = False


# Circuit breaker 参数（LiteLLMProvider 专用）
_CB_FAILURE_THRESHOLD = 3
_CB_RECOVERY_TIMEOUT = 60.0


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    last_failure_ts: float = 0.0
    skip_until: float = 0.0


class LiteLLMProvider:
    """
    LLM 调用提供者 — AgentLoop 核心循环专用

    - 从 V4Config 构建端点列表（主 → 备 → C 端点）
    - 内置 circuit-breaker failover
    - 支持全 messages + tools 的流式/非流式调用
    - 支持 Anthropic extended thinking
    """

    def __init__(self, config):
        self._config = config
        self._endpoints: List[_Endpoint] = self._build_endpoints(config)
        self._circuit: Dict[str, _CircuitState] = {
            ep.name: _CircuitState() for ep in self._endpoints
        }

    def _build_endpoints(self, config) -> List[_Endpoint]:
        """
        优先从 llm_endpoints.json 构建端点列表（与 LLMEndpointProvider 共享同一配置文件）。
        json 不可用时降级到空列表并报警。
        同时从 json 读取 failover_threshold / recovery_timeout 更新全局熔断参数。
        """
        # 1. 定位 llm_endpoints.json（与 LLMEndpointProvider._resolve_json_path 逻辑一致）
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        json_path = os.path.join(base, "llm_endpoints.json")
        if not os.path.exists(json_path):
            project_root = os.path.dirname(base)
            json_path = os.path.join(project_root, "config", "llm_endpoints.json")

        json_cfg = _load_json_endpoints(json_path) if os.path.exists(json_path) else None

        if not json_cfg or not json_cfg.get("endpoints"):
            logger.warning(
                "[LiteLLMProvider] llm_endpoints.json not found or empty, "
                "no endpoints configured."
            )
            return []

        # 2. 从 json 更新全局熔断参数
        global _CB_FAILURE_THRESHOLD, _CB_RECOVERY_TIMEOUT
        if "failover_threshold" in json_cfg:
            _CB_FAILURE_THRESHOLD = int(json_cfg["failover_threshold"])
        if "recovery_timeout" in json_cfg:
            _CB_RECOVERY_TIMEOUT = float(json_cfg["recovery_timeout"])

        # 3. 解析端点列表
        endpoints = []
        for ep_cfg in json_cfg["endpoints"]:
            if not ep_cfg.get("enabled", True):
                continue
            ep = _Endpoint(
                name=ep_cfg.get("name", "unknown"),
                base_url=ep_cfg.get("base_url", ""),
                api_key=ep_cfg.get("api_key", ""),
                model=ep_cfg.get("model", ""),
                small_fast_model=ep_cfg.get("small_fast_model", ""),
                priority=ep_cfg.get("priority", 999),
                support_image=str(ep_cfg.get("support_image", "false")).upper() in ("TRUE", "1", "YES"),
            )
            if ep.api_key and ep.base_url:
                endpoints.append(ep)

        endpoints.sort(key=lambda e: e.priority)

        logger.info(
            f"[LiteLLMProvider] Loaded {len(endpoints)} endpoints from {json_path}: "
            f"[{', '.join(f'{e.name}(p={e.priority})' for e in endpoints)}], "
            f"failure_threshold={_CB_FAILURE_THRESHOLD}, recovery_timeout={_CB_RECOVERY_TIMEOUT}s"
        )
        return endpoints

    def _get_http_client_for_endpoint(self, base_url: str, timeout: float):
        """使用全局连接池"""
        return _get_http_client(base_url, timeout)

    def _get_ordered_endpoints(self, require_image: bool = False) -> List[_Endpoint]:
        """返回按 priority 排序、跳过熔断端点的端点列表。
        当 require_image=True 时，support_image=True 的端点优先排到前面。
        """
        now = time.time()
        available = []
        skipped = []
        for ep in sorted(self._endpoints, key=lambda e: e.priority):
            state = self._circuit.get(ep.name)
            if state and state.skip_until > now:
                skipped.append(ep)
            else:
                available.append(ep)
        if not available:
            logger.warning(
                "[LiteLLMProvider] All endpoints in circuit-open state, "
                "using least-recently failed as fallback"
            )
            return skipped

        # 图片场景：support_image 端点优先，其他保持原顺序兜底
        if require_image:
            image_eps = [ep for ep in available if ep.support_image]
            other_eps = [ep for ep in available if not ep.support_image]
            if image_eps:
                logger.info(
                    f"[LiteLLMProvider] Image detected, prioritizing vision endpoints: "
                    f"{[ep.name for ep in image_eps]}"
                )
                return image_eps + other_eps

        return available

    def _report_success(self, name: str) -> None:
        state = self._circuit.get(name)
        if state:
            state.consecutive_failures = 0
            state.skip_until = 0.0

    def _report_failure(self, name: str) -> None:
        state = self._circuit.get(name)
        if not state:
            return
        state.consecutive_failures += 1
        state.last_failure_ts = time.time()
        if state.consecutive_failures >= _CB_FAILURE_THRESHOLD:
            state.skip_until = time.time() + _CB_RECOVERY_TIMEOUT
            logger.warning(
                f"[LiteLLMProvider] Endpoint '{name}' circuit OPEN "
                f"(consecutive_failures={state.consecutive_failures}), "
                f"will retry after {_CB_RECOVERY_TIMEOUT}s"
            )

    @staticmethod
    def _messages_contain_image(messages: List[Dict[str, Any]]) -> bool:
        """检测消息列表中是否包含图片内容"""
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image":
                        return True
                    if isinstance(block, dict) and block.get("type") == "image_url":
                        return True
                    # Anthropic 格式
                    if isinstance(block, dict) and block.get("source", {}).get("type") == "base64":
                        return True
        return False

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[dict]] = None,
        model: Optional[str] = None,
        max_tokens: int = 8096,
        system: Optional[str] = None,
        timeout: float = 120.0,
    ) -> LLMResponse:
        """非流式调用 LLM"""
        require_image = self._messages_contain_image(messages)
        endpoints = self._get_ordered_endpoints(require_image=require_image)
        if not endpoints:
            raise RuntimeError("[LiteLLMProvider] No LLM endpoints configured")

        last_error = None
        for ep in endpoints:
            try:
                result = await self._call_endpoint(
                    ep=ep,
                    messages=messages,
                    tools=tools,
                    model=model or ep.model,
                    max_tokens=max_tokens,
                    system=system,
                    timeout=timeout,
                    stream=False,
                )
                self._report_success(ep.name)
                return result
            except Exception as e:
                last_error = e
                self._report_failure(ep.name)
                logger.warning(
                    f"[LiteLLMProvider] chat failed on '{ep.name}': "
                    f"{type(e).__name__}: {e}"
                )

        raise RuntimeError(f"[LiteLLMProvider] All endpoints failed: {last_error}")

    async def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[dict]] = None,
        model: Optional[str] = None,
        max_tokens: int = 8096,
        system: Optional[Union[str, List[dict]]] = None,  # 支持 str 或 cache_control blocks
        timeout: float = 120.0,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        流式调用 LLM，支持竞速模式（llm_race.enabled=true 时并发多端点，谁先吐出第一个 event 就用谁）

        Yields:
            {type: "text_delta", delta: str}
            {type: "tool_call_delta", ...}
            {type: "tool_call_complete", tool_call: ToolCallRequest}
            {type: "llm_response", response: LLMResponse}
        """
        require_image = self._messages_contain_image(messages)
        endpoints = self._get_ordered_endpoints(require_image=require_image)
        if not endpoints:
            raise RuntimeError("[LiteLLMProvider] No LLM endpoints configured")

        # ── 竞速模式 ─────────────────────────────────────────────────────────
        race_cfg = _get_perf_config().get("llm_race", {})
        race_enabled = race_cfg.get("enabled", False)
        race_concurrency = race_cfg.get("concurrency", 2)

        if race_enabled and len(endpoints) >= 2:
            race_eps = endpoints[:race_concurrency]
            # 每个端点一条 async generator，通过 Queue 汇聚事件
            # sentinel = None 表示该端点已结束（正常或异常）
            _SENTINEL = object()
            queue: asyncio.Queue = asyncio.Queue()
            winner_name: Optional[str] = None
            winner_ep: Optional[_Endpoint] = None
            ep_errors: Dict[str, Exception] = {}

            async def _feed(ep: _Endpoint) -> None:
                nonlocal winner_name, winner_ep
                try:
                    first = True
                    async for event in self._stream_endpoint(
                        ep=ep,
                        messages=messages,
                        tools=tools,
                        model=model or ep.model,
                        max_tokens=max_tokens,
                        system=system,
                        timeout=timeout,
                    ):
                        if first:
                            first = False
                            # 第一个出 event 的端点成为 winner
                            if winner_name is None:
                                winner_name = ep.name
                                winner_ep = ep
                        # 只有 winner 的 event 才放入 queue，其他端点丢弃
                        if winner_name == ep.name:
                            await queue.put((ep.name, event))
                    if winner_name == ep.name:
                        await queue.put((ep.name, _SENTINEL))
                except asyncio.CancelledError:
                    if winner_name == ep.name:
                        await queue.put((ep.name, _SENTINEL))
                except Exception as e:
                    ep_errors[ep.name] = e
                    logger.warning(
                        f"[LiteLLMProvider] stream race failed on '{ep.name}': "
                        f"{type(e).__name__}: {e}"
                    )
                    if winner_name == ep.name:
                        # winner 出错，通知消费者
                        await queue.put((ep.name, _SENTINEL))

            async def _watch_all_failed() -> None:
                """所有 race 任务都结束且没有 winner 时，往 queue 放哨兵触发 fallback"""
                await asyncio.gather(*tasks, return_exceptions=True)
                if winner_name is None:
                    await queue.put(("__all_failed__", _SENTINEL))

            tasks = [asyncio.create_task(_feed(ep)) for ep in race_eps]
            asyncio.create_task(_watch_all_failed())
            race_start = time.time()

            _race_timed_out = False
            try:
                # 等待第一个 event 或所有任务结束
                while True:
                    try:
                        _q_idle = race_cfg.get("queue_idle_timeout_seconds", 30.0)
                        item = await asyncio.wait_for(queue.get(), timeout=_q_idle)
                    except asyncio.TimeoutError:
                        # queue_idle_timeout_seconds 内没有新 token → winner 流挂死，标记为失败以触发 fallback
                        _timeout_target = winner_name or "unknown"
                        logger.warning(
                            f"[LiteLLMProvider] stream race idle timeout (30s) on '{_timeout_target}', "
                            f"marking as failed and falling back"
                        )
                        if winner_name:
                            ep_errors[winner_name] = RuntimeError(
                                f"stream idle timeout (120s) on '{winner_name}'"
                            )
                        _race_timed_out = True
                        break

                    ep_name, event = item
                    if event is _SENTINEL:
                        if ep_name == "__all_failed__":
                            # 所有 race 端点都快速失败，立即进入 fallback（不再等 120s timeout）
                            logger.warning("[LiteLLMProvider] stream race all endpoints failed immediately, fallback now")
                        # winner 流结束 或 全部失败
                        break
                    if winner_name and ep_name == winner_name:
                        if winner_name != getattr(self, "_last_race_winner", None):
                            elapsed = (time.time() - race_start) * 1000
                            logger.info(
                                f"[LLMRace:stream] winner: {winner_name} in {elapsed:.0f}ms"
                            )
                            self._last_race_winner = winner_name
                        yield event
            finally:
                for t in tasks:
                    t.cancel()
                for t in tasks:
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
                if winner_ep:
                    if winner_ep.name not in ep_errors:
                        self._report_success(winner_ep.name)
                    else:
                        self._report_failure(winner_ep.name)

            # 竞速成功，直接返回
            if winner_name and winner_name not in ep_errors:
                return

            # 竞速全部失败，降级到串行 failover（剩余端点）
            logger.warning("[LiteLLMProvider] stream race all failed, falling back to serial")
            remaining = [ep for ep in endpoints if ep.name not in {e.name for e in race_eps}]
            fallback_eps = remaining or endpoints  # 若无剩余就重试全部
        else:
            fallback_eps = endpoints

        # ── 串行 failover ────────────────────────────────────────────────────
        last_error = None
        for ep in fallback_eps:
            try:
                async for event in self._stream_endpoint(
                    ep=ep,
                    messages=messages,
                    tools=tools,
                    model=model or ep.model,
                    max_tokens=max_tokens,
                    system=system,
                    timeout=timeout,
                ):
                    yield event
                self._report_success(ep.name)
                return
            except Exception as e:
                last_error = e
                self._report_failure(ep.name)
                logger.warning(
                    f"[LiteLLMProvider] stream failed on '{ep.name}': "
                    f"{type(e).__name__}: {e}"
                )

        raise RuntimeError(f"[LiteLLMProvider] All stream endpoints failed: {last_error}")

    async def chat_with_retry(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[dict]] = None,
        model: Optional[str] = None,
        max_tokens: int = 8096,
        system: Optional[str] = None,
        max_retries: int = 2,
        timeout: float = 120.0,
    ) -> LLMResponse:
        """带重试的非流式调用"""
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                return await self.chat(
                    messages=messages, tools=tools, model=model,
                    max_tokens=max_tokens, system=system, timeout=timeout,
                )
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        f"[LiteLLMProvider] Retry {attempt + 1}/{max_retries} "
                        f"after {wait}s: {e}"
                    )
                    await asyncio.sleep(wait)

        raise RuntimeError(f"[LiteLLMProvider] chat_with_retry failed: {last_error}")

    # ──────────────────────── 内部实现 ────────────────────────

    async def _call_endpoint(
        self,
        ep: _Endpoint,
        messages: List[Dict[str, Any]],
        tools: Optional[List[dict]],
        model: str,
        max_tokens: int,
        system: Optional[Union[str, List[dict]]],
        timeout: float,
        stream: bool,
    ) -> LLMResponse:
        url = f"{ep.base_url.rstrip('/')}/v1/messages"
        headers = {
            "x-api-key": ep.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = self._build_request_body(
            messages=messages, tools=tools, model=model,
            max_tokens=max_tokens, system=system, stream=False,
        )
        client = self._get_http_client_for_endpoint(ep.base_url, timeout)
        resp = await client.post(url, headers=headers, json=body, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(
                f"LLM API returned {resp.status_code}: {resp.text[:300]}"
            )
        return self._parse_response(resp.json())

    async def _stream_endpoint(
        self,
        ep: _Endpoint,
        messages: List[Dict[str, Any]],
        tools: Optional[List[dict]],
        model: str,
        max_tokens: int,
        system: Optional[Union[str, List[dict]]],
        timeout: float,
    ) -> AsyncIterator[Dict[str, Any]]:
        url = f"{ep.base_url.rstrip('/')}/v1/messages"
        headers = {
            "x-api-key": ep.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = self._build_request_body(
            messages=messages, tools=tools, model=model,
            max_tokens=max_tokens, system=system, stream=True,
        )

        accumulated_text: List[str] = []
        tool_calls_buffer: Dict[int, Dict] = {}
        finish_reason = "stop"
        usage: Dict[str, int] = {}
        thinking_parts: List[str] = []
        thinking_signature: Optional[str] = None   # dashscope 要求回写时携带 signature

        client = self._get_http_client_for_endpoint(ep.base_url, timeout)
        async with client.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
            if resp.status_code != 200:
                error_body = await resp.aread()
                raise RuntimeError(
                    f"LLM stream returned {resp.status_code}: {error_body[:300]}"
                )

            async for line in _aiter_lines_with_timeout(resp, idle_timeout=min(timeout, 60.0)):
                if line.startswith("data: "):
                    data_str = line[6:]
                elif line.startswith("data:"):
                    data_str = line[5:]
                else:
                    continue
                if data_str.strip() == "[DONE]":
                    break
                try:
                    event_data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                event_type = event_data.get("type", "")

                # 检测 API 错误响应（如内容审核拒绝）
                if "error" in event_data and not event_type:
                    err = event_data["error"]
                    err_code = err.get("code", "")
                    err_msg = err.get("message", "unknown error")
                    logger.warning(
                        f"[LLMProvider:stream] ep={ep.name} API error: "
                        f"code={err_code}, message={err_msg[:200]}"
                    )
                    raise RuntimeError(
                        f"LLM API error (code={err_code}): {err_msg[:200]}"
                    )

                if event_type == "content_block_start":
                    block = event_data.get("content_block", {})
                    block_index = event_data.get("index", 0)
                    if block.get("type") == "tool_use":
                        tool_calls_buffer[block_index] = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "arguments_parts": [],
                        }
                    elif block.get("type") == "thinking":
                        # 记录 thinking 块起始的 signature（dashscope 在此处携带）
                        sig = block.get("signature")
                        if sig is not None:
                            thinking_signature = sig

                elif event_type == "content_block_delta":
                    delta = event_data.get("delta", {})
                    delta_type = delta.get("type", "")
                    block_index = event_data.get("index", 0)

                    if delta_type == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            accumulated_text.append(text)
                            yield {"type": "text_delta", "delta": text}
                    elif delta_type == "thinking_delta":
                        thinking = delta.get("thinking", "")
                        if thinking:
                            thinking_parts.append(thinking)
                    elif delta_type == "input_json_delta":
                        partial = delta.get("partial_json", "")
                        if block_index in tool_calls_buffer:
                            tool_calls_buffer[block_index]["arguments_parts"].append(partial)
                            yield {
                                "type": "tool_call_delta",
                                "tool_call_id": tool_calls_buffer[block_index]["id"],
                                "name": tool_calls_buffer[block_index]["name"],
                                "arguments_partial": partial,
                            }

                elif event_type == "content_block_stop":
                    block_index = event_data.get("index", 0)
                    # 某些 provider 在 stop 事件中携带 signature
                    sig = event_data.get("signature")
                    if sig is not None:
                        thinking_signature = sig
                    if block_index in tool_calls_buffer:
                        tc = tool_calls_buffer[block_index]
                        args_str = "".join(tc["arguments_parts"])
                        logger.info(
                            f"[LLMProvider:stream] tool_call '{tc['name']}' "
                            f"args_parts={len(tc['arguments_parts'])}, "
                            f"args_str='{args_str[:200]}'"
                        )
                        try:
                            arguments = json.loads(args_str) if args_str else {}
                        except json.JSONDecodeError:
                            arguments = {"_raw": args_str}
                        tool_call = ToolCallRequest(
                            id=tc["id"], name=tc["name"], arguments=arguments,
                        )
                        yield {"type": "tool_call_complete", "tool_call": tool_call}

                elif event_type == "message_delta":
                    delta = event_data.get("delta", {})
                    finish_reason = delta.get("stop_reason", "stop") or "stop"
                    u = event_data.get("usage", {})
                    if u:
                        usage["output_tokens"] = u.get("output_tokens", 0)

                elif event_type == "message_start":
                    u = event_data.get("message", {}).get("usage", {})
                    if u:
                        usage["input_tokens"] = u.get("input_tokens", 0)
                        usage["cache_read_input_tokens"] = u.get("cache_read_input_tokens", 0)
                        usage["cache_creation_input_tokens"] = u.get("cache_creation_input_tokens", 0)

        # 构建最终 LLMResponse
        tool_calls = []
        for idx, tc in sorted(tool_calls_buffer.items()):
            args_str = "".join(tc["arguments_parts"])
            try:
                arguments = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                arguments = {"_raw": args_str}
            tool_calls.append(ToolCallRequest(id=tc["id"], name=tc["name"], arguments=arguments))

        response = LLMResponse(
            content="".join(accumulated_text) if accumulated_text else None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            thinking_content="".join(thinking_parts) if thinking_parts else None,
            thinking_signature=thinking_signature,
            model=model,
        )
        yield {"type": "llm_response", "response": response}

    def _build_request_body(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[dict]],
        model: str,
        max_tokens: int,
        system: Optional[Union[str, List[dict]]],
        stream: bool,
    ) -> dict:
        body: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            "thinking": {"type": "disabled"},
        }
        if system:
            if isinstance(system, list):
                # cache_control blocks 格式：[{"type": "text", "text": "...", "cache_control": {"type": "ephemeral"}}]
                body["system"] = system
            else:
                body["system"] = system  # 字符串格式，保持兼容
        if stream:
            body["stream"] = True
        if tools:
            anthropic_tools = []
            for tool in tools:
                if tool.get("type") == "function":
                    fn = tool["function"]
                    anthropic_tools.append({
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters",
                                               {"type": "object", "properties": {}}),
                    })
                else:
                    anthropic_tools.append(tool)
            body["tools"] = anthropic_tools
        return body

    def _parse_response(self, data: dict) -> LLMResponse:
        text_parts, tool_calls, thinking_parts = [], [], []
        thinking_signature: Optional[str] = None
        for block in data.get("content", []):
            btype = block.get("type", "")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(ToolCallRequest(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input", {}),
                ))
            elif btype == "thinking":
                thinking_parts.append(block.get("thinking", ""))
                sig = block.get("signature")
                if sig is not None:
                    thinking_signature = sig

        stop_reason = data.get("stop_reason", "stop")
        finish_reason = (
            "tool_use" if stop_reason == "tool_use"
            else "max_tokens" if stop_reason == "max_tokens"
            else "stop"
        )
        usage = data.get("usage", {})
        return LLMResponse(
            content="".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage={
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            },
            thinking_content="".join(thinking_parts) if thinking_parts else None,
            thinking_signature=thinking_signature,
            model=data.get("model", ""),
        )


# ═══════════════════════════════════════════════════════════════════════
# § 8  全局 provider 单例（call_llm 供辅助模块使用）
# ═══════════════════════════════════════════════════════════════════════

_global_provider: Optional[LiteLLMProvider] = None


def set_global_provider(provider: LiteLLMProvider) -> None:
    """注册全局 LiteLLMProvider（由 native_agent 启动时调用）"""
    global _global_provider
    _global_provider = provider


async def call_llm(
    prompt: str,
    use_small_fast: bool = False,
    system: Optional[str] = None,
    max_tokens: int = 1024,
    timeout: float = 30.0,
    temperature: Optional[float] = None,
) -> str:
    """
    全局非流式 LLM 调用 — 辅助模块统一入口。

    优先使用 LLMEndpointProvider（JSON 配置 + race mode + failover），
    降级到全局 LiteLLMProvider。

    Usage::

        from agent_core.agentloop.llm_provider import call_llm
        text = await call_llm(prompt, use_small_fast=True, max_tokens=512)
    """
    try:
        return await call_anthropic_api(
            prompt=prompt,
            use_small_fast=use_small_fast,
            system=system,
            max_tokens=max_tokens,
            timeout=timeout,
            temperature=temperature,
        )
    except Exception:
        pass

    # 降级到 LiteLLMProvider
    provider = _global_provider
    if provider is None:
        logger.warning("[call_llm] No provider available")
        return ""

    messages = [{"role": "user", "content": prompt}]
    endpoints = provider._get_ordered_endpoints()
    if not endpoints:
        return ""

    ep = endpoints[0]
    model = ep.small_fast_model if (use_small_fast and ep.small_fast_model) else ep.model
    try:
        response = await provider.chat(
            messages=messages, system=system, model=model,
            max_tokens=max_tokens, timeout=timeout,
        )
        return response.content or ""
    except Exception as e:
        logger.warning(f"[call_llm] Fallback provider also failed: {e}")
        return ""
