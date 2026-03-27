"""
AgentCore CLI 入口

用法:
    sthg-agent run "查询内容"     -- 单次执行
    sthg-agent chat               -- 交互式 REPL
    sthg-agent serve --port 8000  -- 启动 HTTP 服务（委托给 FastAPI）
"""
import sys
import asyncio
import argparse
from pathlib import Path
from loguru import logger


def _setup_logging(verbose: bool = False):
    """配置日志"""
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(sys.stderr, level=level, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")


async def _run_once(message: str, env_file: str = ".env"):
    """单次执行模式"""
    from .config import V4Config
    from .skill_discovery import SkillDiscovery
    from .skill_metadata_provider import init_skill_metadata_provider
    from .agent import V4AgentRequest, DataCollector

    config = V4Config.from_env(env_file)
    discovery = SkillDiscovery(skills_dir=config.skills_dir)
    discovery.scan()
    provider = init_skill_metadata_provider(discovery)

    logger.info(f"[CLI] Skills loaded: {len(discovery.get_all())}")
    logger.info(f"[CLI] Query: {message}")

    # 创建请求
    request = V4AgentRequest(message=message)
    collector = DataCollector()
    collector.set_metadata_provider(provider)

    # 此处仅验证核心组件加载正常
    # 完整的 SDK 执行需要 V4NativeAgent 实例
    logger.info("[CLI] Core components loaded successfully")
    logger.info(f"[CLI] Config: model={config.anthropic_model}, base_url={config.anthropic_base_url[:30] if config.anthropic_base_url else 'N/A'}")

    print(f"\n[AgentCore] Ready to process: {message}")
    print(f"[AgentCore] Skills: {len(discovery.get_all())}")
    print(f"[AgentCore] Config mode: CLI (from_env)")


async def _chat_repl(env_file: str = ".env"):
    """交互式 REPL 模式"""
    from .config import V4Config
    from .skill_discovery import SkillDiscovery
    from .skill_metadata_provider import init_skill_metadata_provider

    config = V4Config.from_env(env_file)
    discovery = SkillDiscovery(skills_dir=config.skills_dir)
    discovery.scan()
    init_skill_metadata_provider(discovery)

    print(f"AgentCore REPL (skills: {len(discovery.get_all())})")
    print("Type 'quit' to exit.\n")

    while True:
        try:
            user_input = input("you> ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                print("Bye!")
                break

            # 简单回显模式（完整执行需要 V4NativeAgent）
            print(f"[AgentCore] Received: {user_input}")
            print("[AgentCore] Full agent execution requires V4NativeAgent.\n")

        except (KeyboardInterrupt, EOFError):
            print("\nBye!")
            break


def _serve(port: int = 8000, host: str = "0.0.0.0"):
    """启动 HTTP 服务（委托给 FastAPI，通过模块字符串加载避免直接 import app）"""
    try:
        import uvicorn
        logger.info(f"[CLI] Starting HTTP server on {host}:{port}")
        uvicorn.run("app.main:app", host=host, port=port, reload=False)
    except ImportError as e:
        logger.error(f"[CLI] Cannot start HTTP server: {e}")
        logger.error("[CLI] Install fastapi + uvicorn for serve mode")
        sys.exit(1)


def main():
    """CLI 主入口"""
    parser = argparse.ArgumentParser(
        prog="sthg-agent",
        description="STHG Agent Core CLI"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("--env", default=".env", help="Environment file path")

    subparsers = parser.add_subparsers(dest="command")

    # run 子命令
    run_parser = subparsers.add_parser("run", help="Execute a single query")
    run_parser.add_argument("message", help="Query message")

    # chat 子命令
    subparsers.add_parser("chat", help="Interactive REPL mode")

    # serve 子命令
    serve_parser = subparsers.add_parser("serve", help="Start HTTP server")
    serve_parser.add_argument("--port", type=int, default=8000, help="Server port")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Server host")

    args = parser.parse_args()

    _setup_logging(args.verbose)

    if args.command == "run":
        asyncio.run(_run_once(args.message, args.env))
    elif args.command == "chat":
        asyncio.run(_chat_repl(args.env))
    elif args.command == "serve":
        _serve(args.port, args.host)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
