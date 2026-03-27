"""
热重载主程序
集成所有热重载功能，提供无需重启的动态加载能力
"""

import asyncio
from fastapi import FastAPI
from loguru import logger
from pathlib import Path

# 导入热重载组件
from .dynamic_skill_loader import hot_reload_manager
from .mcp_hot_reloader import mcp_hot_reloader
from .api_hot_reloader import APIHotReloader

from app.main import app


class HotReloadServer:
    """热重载服务器"""

    def __init__(self, app: FastAPI):
        self.app = app
        self.skill_manager = hot_reload_manager
        self.mcp_manager = mcp_hot_reloader
        self.api_manager = None
        self.running = False

    async def initialize(self):
        """初始化所有热重载管理器"""
        logger.info("🔥 初始化热重载系统...")

        # 初始化技能热重载
        await self.skill_manager.start_watching()

        # 连接到技能注册表
        from .claude.skills.registry import registry
        self.skill_manager.set_skill_registry(registry)

        # 初始化MCP热重载
        await self.mcp_manager.initialize()

        # 初始化API热重载
        self.api_manager = APIHotReloader(self.app)

        self.running = True
        logger.info("🚀 热重载系统启动完成")

    async def shutdown(self):
        """关闭热重载系统"""
        if not self.running:
            return

        logger.info("⏹️ 关闭热重载系统...")

        await self.skill_manager.stop_watching()
        self.running = False

        logger.info("✅ 热重载系统已关闭")

    async def refresh_all(self):
        """强制刷新所有组件"""
        if not self.running:
            logger.warning("⚠️ 热重复系统未运行")
            return

        logger.info("🔄 强制刷新所有组件...")

        # 刷新技能
        await self.skill_manager.refresh_all_skills()

        # MCP服务器不需要刷新，工具是动态添加的

        logger.info("✅ 所有组件刷新完成")

    def get_status(self) -> dict:
        """获取热重载系统状态"""
        return {
            "running": self.running,
            "skill_manager": {
                "watching": await self.skill_manager.last_modified if self.skill_manager else False,
                "status": self.skill_manager.get_status()
            },
            "mcp_manager": {
                "initialized": self.mcp_manager.mcp_server is not None,
                "status": await self.mcp_manager.health_check()
            },
            "api_manager": {
                "initialized": self.api_manager is not None,
                "status": await self.api_manager.health_check() if self.api_manager else None
            }
        }

    async def add_api_endpoint(self, path: str, endpoint_func, methods: list = None):
        """添加动态API端点"""
        if not self.api_manager:
            raise RuntimeError("API管理器未初始化")

        return await self.api_manager.add_route(
            path=path,
            endpoint_func=endpoint_func,
            methods=methods or ["POST"]
        )

    async def add_mcp_tool(self, tool_func, tool_name: str = None):
        """添加动态MCP工具"""
        if not self.mcp_manager:
            raise RuntimeError("MCP管理器未初始化")

        return await self.mcp_manager.add_tool(tool_func, tool_name)

    async def force_refresh_skills(self):
        """强制刷新技能"""
        if not self.skill_manager:
            raise RuntimeError("技能管理器未初始化")

        await self.skill_manager.refresh_all_skills()


# 全局热重载服务器实例
hot_reload_server = None


# 启动热重载服务的FastAPI应用
async def start_hot_reload_app():
    """启动支持热重载的应用"""
    logger.info("🚀 启动支持热重载的智能UI系统")

    # 创建热重载服务器
    global hot_reload_server
    hot_reload_server = HotReloadServer(app)

    # 初始化热重载系统
    await hot_reload_server.initialize()

    # 添加热重载管理API端点
    @app.post("/hot-reload/refresh")
    async def refresh_all():
        """刷新所有组件"""
        try:
            await hot_reload_server.refresh_all()
            return {
                "code": 200,
                "message": "所有组件刷新完成",
                "status": hot_reload_server.get_status()
            }
        except Exception as e:
            return {
                "code": 500,
                "error": str(e),
                "message": "刷新失败"
            }

    @app.get("/hot-reload/status")
    async def hot_reload_status():
        """获取热重载状态"""
        return {
            "code": 200,
            "message": "success",
            "data": hot_reload_server.get_status()
        }

    @app.post("/hot-reload/api")
    async def add_dynamic_api(request: dict):
        """添加动态API端点"""
        try:
            # 这里应该有一个端点注册表
            # 暂时返回空响应
            path = request.get("path", "/dynamic/test")
            methods = request.get("methods", ["POST"])

            # 示例端点
            async def test_endpoint(data: dict = {}):
                return {"message": "动态API端点", "data": data}

            route_path = await hot_reload_server.add_api_endpoint(path, test_endpoint, methods)

            return {
                "code": 200,
                "message": f"动态API端点添加成功: {route_path}",
                "data": {"path": route_path, "methods": methods}
            }

        except Exception as e:
            return {
                "code": 500,
                "error": str(e),
                "message": "添加API端点失败"
            }

    @app.post("/hot-reload/mcp-tool")
    async def add_mcp_tool_request(request: dict):
        """添加MCP工具请求"""
        try:
            # 这里应该有工具注册机制
            # 暂时返回空响应
            tool_name = request.get("tool_name", "dynamic_tool")

            # 示例工具（占位，不实际使用）
            async def dynamic_tool_func(args):
                input_data = args.get("input", "")
                return {
                    "result": f"动态处理: {input_data}",
                    "tool": "dynamic_tool"
                }

            success = await hot_reload_server.add_mcp_tool(dynamic_tool_func, tool_name)

            if success:
                return {
                    "code": 200,
                    "message": f"MCP工具添加成功: {tool_name}",
                    "data": {"tool_name": tool_name}
                }
            else:
                return {
                    "code": 500,
                    "message": "MCP工具添加失败",
                    "data": {"tool_name": tool_name}
                }

        except Exception as e:
            return {
                "code": 500,
                "error": str(e),
                "message": "添加MCP工具失败"
            }

    # 应用关闭时清理热重载资源
    @app.on_event("shutdown")
    async def cleanup():
        await hot_reload_server.shutdown()

    logger.info("✅ 热重载应用启动完成")

    return app


if __name__ == "__main__":
    import uvicorn

    async def main():
        # 启动热重载应用
        await start_hot_reload_app()

        # 启动服务器
        uvicorn.run(
            "hot_reload_mcp_tools.app:app",
            host="0.0.0.0",
            port=8000,
            reload=True,
            log_level="info"
        )

    asyncio.run(main())