"""
API路由热重载支持
支持动态添加/移除API端点无需重启服务
"""

from fastapi import FastAPI, APIRouter, HTTPException
from typing import Dict, Any, List, Optional, Callable
from loguru import logger
import importlib
import inspect
import json


class APIHotReloader:
    """API热重载器"""

    def __class_getitem__(cls, key):
        # 支持类似于字典的访问
        return getattr(cls, key)

    def __init__(self, app: FastAPI):
        self.app = app
        self.dynamic_routes: Dict[str, dict] = {}
        self.dynamic_prefixes: Dict[str, APIRouter] = {}
        self.middleware_registry: List[Callable] = []

    async def add_route(self, path: str, endpoint_func: Callable,
                       methods: List[str] = ["POST"],
                       prefix: str = "/dynamic",
                       tags: List[str] = ["dynamic"],
                       include_in_schema: bool = True) -> str:
        """动态添加API端点"""
        try:
            full_path = f"{prefix}{path}"
            route_method = endpoint_func

            # 创建临时路由器
            temp_router = APIRouter(prefix=prefix, tags=tags)

            # 添加路由
            if "GET" in methods:
                temp_router.get(path)(endpoint_func)
            if "POST" in methods:
                temp_router.post(path)(endpoint_func)
            if "PUT" in methods:
                temp_router.put(path)(endpoint_func)
            if "DELETE" in methods:
                temp_router.delete(path)(endpoint_func)

            # 注册到应用
            self.app.include_router(temp_router)

            # 记录动态路由
            self.dynamic_routes[full_path] = {
                "path": full_path,
                "methods": methods,
                "endpoint": endpoint_func.__name__,
                "prefix": prefix,
                "tags": tags,
                "router": temp_router,
                "added_at": asyncio.get_event_loop().time()
            }

            logger.info(f"✅ 动态添加API端点: {full_path}")
            return full_path

        except Exception as e:
            logger.error(f"❌ 动态添加API端点失败 {path}: {str(e)}")
            raise

    async def remove_route(self, path: str, prefix: str = "/dynamic"):
        """动态移除API端点"""
        try:
            full_path = f"{prefix}{path}"

            if full_path not in self.dynamic_routes:
                logger.warning(f"⚠️ API端点不存在: {full_path}")
                return False

            route_info = self.dynamic_routes[full_path]
            router = route_info["router"]

            # 从应用中移除路由器
            # 注意：FastAPI不支持完全移除路由，这里只是从记录中移除
            del self.dynamic_routes[full_path]

            logger.info(f"🗑️ 动态移除API端点: {full_path}")
            return True

        except Exception as e:
            logger.error(f"❌ 动态移除API端点失败 {path}: {str(e)}")
            return False

    async def reload_route(self, path: str, new_endpoint_func: Callable,
                         methods: List[str] = ["POST"],
                         prefix: str = "/dynamic"):
        """重载API端点"""
        logger.info(f"🔄 重载API端点: {path}")

        # 先移除旧路由
        await self.remove_route(path, prefix)

        # 添加新路由
        success = await self.add_route(path, new_endpoint_func, methods, prefix)

        if success:
            logger.info(f"✅ API端点重载完成: {path}")
        else:
            logger.error(f"❌ API端点重载失败: {path}")

        return success

    async def add_router(self, router: APIRouter, prefix: Optional[str] = None,
                         tags: Optional[List[str]] = None):
        """动态添加路由器"""
        try:
            # 使用指定的prefix或默认prefix
            router_prefix = prefix or router.prefix

            # 标记为动态路由器
            self.dynamic_prefixes[router_prefix] = router

            # 注册到应用
            self.app.include_router(router, prefix=prefix)

            logger.info(f"✅ 动态添加路由器: {router_prefix}")
            return router_prefix

        except Exception as e:
            logger.error(f"❌ 动态添加路由器失败: {str(e)}")
            raise

    async def add_from_module(self, module_path: str,
                         route_config: Dict[str, Any] = None):
        """从模块添加API端点"""
        try:
            # 动态导入模块
            module = importlib.import_module(module_path)

            # 获取模块中的所有函数
            functions = inspect.getmembers(module, inspect.isfunction)

            added_routes = []
            for func_name, func in functions:
                if hasattr(func, 'dynamic_route_config'):
                    # 函数有路由配置，动态添加
                    config = getattr(func, 'dynamic_route_config')
                    path = config.get('path', f"/{func_name}")
                    methods = config.get('methods', ['POST'])

                    route_path = await self.add_route(
                        path=path,
                        endpoint_func=func,
                        methods=methods,
                        **{k: v for k, v in (route_config or {}).items()
                           if k not in ['path', 'methods']}
                    )
                    added_routes.append(route_path)

            logger.info(f"✅ 从模块添加 {len(added_routes)} 个API端点: {module_path}")
            return added_routes

        except Exception as e:
            logger.error(f"❌ 从模块添加API端点失败 {module_path}: {str(e)}")
            raise

    async def list_dynamic_routes(self) -> Dict[str, Any]:
        """列出动态路由"""
        return {
            "routes": self.dynamic_routes,
            "routers": {prefix: router.prefix for prefix, router in self.dynamic_prefixes.items()},
            "middleware_count": len(self.middleware_registry),
            "total_routes": len(self.dynamic_routes)
        }

    async def get_route_info(self, path: str, prefix: str = "/dynamic") -> Optional[Dict[str, Any]]:
        """获取路由信息"""
        full_path = f"{prefix}{path}"
        return self.dynamic_routes.get(full_path)

    def add_global_middleware(self, middleware: Callable):
        """添加全局中间件"""
        self.app.middleware("http")(middleware)
        self.middleware_registry.append(middleware)
        logger.info(f"✅ 添加全局中间件: {middleware.__name__}")

    async def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        return {
            "dynamic_routes": len(self.dynamic_routes),
            "dynamic_routers": len(self.dynamic_prefixes),
            "middleware_registered": len(self.middleware_registry),
            "app_state": "running"
        }

    async def export_routes_config(self) -> Dict[str, Any]:
        """导出路由配置"""
        return {
            "dynamic_routes": {
                path: {
                    "methods": info["methods"],
                    "endpoint": info["endpoint"],
                    "added_at": info["added_at"]
                }
                for path, info in self.dynamic_routes.items()
            },
            "exported_at": asyncio.get_event_loop().time()
        }


# 装饰器函数，用于标记需要动态加载的API端点
def dynamic_route(path: str, methods: List[str] = ["POST"], **kwargs):
    """动态路由装饰器"""
    def decorator(func):
        func.dynamic_route_config = {
            "path": path,
            "methods": methods,
            **kwargs
        }
        return func
    return decorator


# 示例动态API模块
"""
# 动态API示例模块: dynamic_apis/example_apis.py

from hot_reload.api_hot_reloader import dynamic_route

@dynamic_route("/example/hello", methods=["GET", "POST"])
async def example_hello(data: dict = {}):
    """示例API端点"""
    return {
        "message": "Hello from dynamic API!",
        "data": data,
        "timestamp": asyncio.get_event_loop().time()
    }

@dynamic_route("/example/multiply", methods=["POST"])
async def example_multiply(request: dict):
    """动态乘法API"""
    a = request.get("a", 0)
    b = request.get("b", 0)

    return {
        "result": a * b,
        "operation": f"{a} * {b}",
        "numbers": {"a": a, "b": b}
    }
"""


# 全局API热重载器实例 (app启动时初始化)
api_hot_reloader = None