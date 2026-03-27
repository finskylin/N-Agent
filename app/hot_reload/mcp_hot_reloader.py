"""
MCP服务热重载支持
支持动态添加/移除Tools无需重启服务
"""

import asyncio
from typing import Dict, Any, List, Optional, Callable
from loguru import logger
from services.intelligent_ui_mcp_server import get_mcp_server


class MCPHotReloader:
    """MCP服务热重载器"""

    def __init__(self):
        self.mcp_server = None
        self.available_tools: Dict[str, Callable] = {}
        self.tool_profiles: Dict[str, Dict[str, Any]] = {}
        self.reload_callbacks: List[Callable] = []

    async def initialize(self):
        """初始化MCP热重载器"""
        self.mcp_server = await get_mcp_server()

        # 获取现有工具
        for tool_obj in self.mcp_server.tools:
            tool_name = getattr(tool_obj, '__name__', str(tool_obj))
            self.available_tools[tool_name] = tool_obj

        logger.info(f"🔧 MCP热重载器初始化完成，当前工具数: {len(self.available_tools)}")

    async def add_tool(self, tool_func: Callable, tool_name: str = None, profile: Dict[str, Any] = None):
        """动态添加工具"""
        try:
            # 确定工具名称
            if not tool_name:
                tool_name = getattr(tool_func, '__name__', str(tool_func))

            # 存储工具配置
            self.tool_profiles[tool_name] = profile or {
                "added_at": asyncio.get_event_loop().time(),
                "auto_generated": False
            }

            # 添加到MCP服务器
            if self.mcp_server:
                self.mcp_server.add_tool(tool_func)
                self.available_tools[tool_name] = tool_func

                logger.info(f"✅ 动态添加工具: {tool_name}")

                # 通知回调
                await self._notify_callbacks("tool_added", tool_name, tool_func)

            return True

        except Exception as e:
            logger.error(f"❌ 动态添加工具失败 {tool_name}: {str(e)}")
            return False

    async def remove_tool(self, tool_name: str):
        """动态移除工具"""
        try:
            if tool_name not in self.available_tools:
                logger.warning(f"⚠️ 工具不存在: {tool_name}")
                return False

            # 从MCP服务器移除
            if self.mcp_server and hasattr(self.mcp_server, 'remove_tool'):
                self.mcp_server.remove_tool(tool_name)
                del self.available_tools[tool_name]
                del self.tool_profiles[tool_name]

                logger.info(f"🗑️ 动态移除工具: {tool_name}")

                # 通知回调
                await self._notify_callbacks("tool_removed", tool_name, None)

            return True

        except Exception as e:
            logger.error(f"❌ 动态移除工具失败 {tool_name}: {str(e)}")
            return False

    async def reload_tool(self, tool_name: str, new_tool_func: Callable):
        """重载工具"""
        logger.info(f"🔄 重载工具: {tool_name}")

        # 先移除旧工具
        if tool_name in self.available_tools:
            await self.remove_tool(tool_name)

        # 添加新工具
        success = await self.add_tool(new_tool_func, tool_name)

        if success:
            logger.info(f"✅ 工具重载完成: {tool_name}")
        else:
            logger.error(f"❌ 工具重载失败: {tool_name}")

        return success

    async def list_tools(self) -> Dict[str, Any]:
        """列出所有工具"""
        tools_info = {}

        for tool_name, tool_func in self.available_tools.items():
            tools_info[tool_name] = {
                "name": tool_name,
                "description": getattr(tool_func, '__doc__', 'No description'),
                "params": getattr(tool_func, '__annotations__', {}),
                "profile": self.tool_profiles.get(tool_name, {}),
                "type": type(tool_func).__name__
            }

        return tools_info

    async def get_tool(self, tool_name: str) -> Optional[Callable]:
        """获取工具"""
        return self.available_tools.get(tool_name)

    def register_reload_callback(self, callback: Callable):
        """注册重载回调"""
        self.reload_callbacks.append(callback)

    async def _notify_callbacks(self, event_type: str, tool_name: str, tool_func: Optional[Callable]):
        """通知重载回调"""
        for callback in self.reload_callbacks:
            try:
                await callback(event_type, tool_name, tool_func)
            except Exception as e:
                logger.error(f"❌ 回调通知失败: {str(e)}")

    async def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        return {
            "mcp_server": self.mcp_server is not None,
            "available_tools": len(self.available_tools),
            "tool_profiles": len(self.tool_profiles),
            "callbacks_registered": len(self.reload_callbacks)
        }

    async def batch_update_tools(self, tools_to_add: List[Callable] = None,
                                 tools_to_remove: List[str] = None) -> Dict[str, Any]:
        """批量更新工具"""
        results = {
            "added": [],
            "removed": [],
            "failed": []
        }

        # 批量移除
        if tools_to_remove:
            for tool_name in tools_to_remove:
                success = await self.remove_tool(tool_name)
                if success:
                    results["removed"].append(tool_name)
                else:
                    results["failed"].append(f"remove:{tool_name}")

        # 批量添加
        if tools_to_add:
            for tool_func in tools_to_add:
                success = await self.add_tool(tool_func)
                if success:
                    tool_name = getattr(tool_func, '__name__', str(tool_func))
                    results["added"].append(tool_name)
                else:
                    results["failed"].append(f"add:{tool_name}")

        return results


# 全局MCP热重载器实例
mcp_hot_reloader = MCPHotReloader()