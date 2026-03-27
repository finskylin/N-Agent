"""
GAIA Benchmark Runner

黑盒评测模块 — 通过 HTTP 调用 /api/v1/chat/v4/send 评测 Agent 能力。
零 import agent_core/，完全在 app 层运行。
"""
from app.benchmark.runner import GaiaBenchmarkRunner

__all__ = ["GaiaBenchmarkRunner"]
