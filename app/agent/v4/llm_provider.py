# shim: 全部功能已迁移至 agent_core/agentloop/llm_provider.py
# 此文件仅保留向后兼容的重导出，保持所有现有调用者不变。
from agent_core.agentloop.llm_provider import (
    LLMEndpoint,
    LLMEndpointProvider,
    call_anthropic_api,
    stream_anthropic_api,
    get_small_fast_llm_call,
    get_anthropic_client_config,
    shutdown_http_pools,
    _interpolate_env,
    _load_json_endpoints,
    _get_perf_config,
    _get_http_client,
    _invalidate_pool,
    _call_single_endpoint,
)

__all__ = [
    "LLMEndpoint",
    "LLMEndpointProvider",
    "call_anthropic_api",
    "stream_anthropic_api",
    "get_small_fast_llm_call",
    "get_anthropic_client_config",
    "shutdown_http_pools",
    "_interpolate_env",
    "_load_json_endpoints",
    "_get_perf_config",
    "_get_http_client",
    "_invalidate_pool",
    "_call_single_endpoint",
]
