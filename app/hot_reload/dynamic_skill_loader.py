"""
动态技能热加载器
支持在不重启应用的情况下动态加载新增的Skills
"""

import os
import asyncio
import importlib.util
from pathlib import Path
from typing import Set, Dict, List, Optional
from loguru import logger
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class SkillReloadHandler(FileSystemEventHandler):
    """文件系统事件处理器"""

    def __init__(self, skill_directory: str, reload_callback):
        self.skill_directory = Path(skill_directory)
        self.reload_callback = reload_callback
        self.last_modified: Dict[str, float] = {}

    def on_modified(self, event):
        """文件修改事件"""
        if event.is_directory:
            return

        file_path = Path(event.src_path)

        # 只处理Python文件
        if file_path.suffix != '.py':
            return

        # 避免重复触发
        file_key = str(file_path)
        current_time = asyncio.get_event_loop().time()

        if (file_key in self.last_modified and
            current_time - self.last_modified[file_key] < 2.0):
            return

        self.last_modified[file_key] = current_time

        logger.info(f"🔥 检测到文件修改: {file_path.name}")

        # 异步执行热重载
        asyncio.create_task(self.reload_callback(file_path))


class DynamicSkillLoader:
    """动态技能加载器"""

    def __init__(self, skill_directory: str = ".claude/skills"):
        self.skill_directory = Path(skill_directory)
        self.observer: Optional[Observer] = None
        self.loaded_modules: Set[str] = set()
        self.skill_registry = None

    async def start_watching(self):
        """开始监控文件变化"""
        if self.observer:
            await self.stop_watching()

        event_handler = SkillReloadHandler(self.skill_directory, self._handle_file_change)

        self.observer = Observer()
        self.observer.schedule(event_handler, str(self.skill_directory), recursive=True)
        self.observer.start()

        logger.info(f"🔍 开始监控技能目录: {self.skill_directory}")

    async def stop_watching(self):
        """停止监控"""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
            logger.info("⏹️ 停止监控技能目录")

    async def _handle_file_change(self, changed_file: Path):
        """处理文件变化"""
        try:
            # 检查是否是skill目录下的文件
            if not self._is_under_skill_directory(changed_file):
                return

            skill_dir = self._get_skill_directory(changed_file)
            if not skill_dir:
                return

            logger.info(f"🔄 热重载技能模块: {skill_dir.name}")

            # 卸载旧模块
            await self._unload_skill_modules(skill_dir.name)

            # 重新加载技能
            await self._load_skill_directory(skill_dir)

            # 通知注册表更新
            if self.skill_registry:
                await self.skill_registry.refresh_skills()

        except Exception as e:
            logger.error(f"❌ 热重载失败: {str(e)}")

    def _is_under_skill_directory(self, file_path: Path) -> bool:
        """检查文件是否在技能目录下"""
        try:
            file_path.resolve().relative_to(self.skill_directory.resolve())
            return True
        except ValueError:
            return False

    def _get_skill_directory(self, file_path: Path) -> Optional[Path]:
        """获取包含文件的技能目录"""
        current = file_path.parent
        while current != self.skill_directory and current.parent != current:
            if current.parent == self.skill_directory:
                return current
            current = current.parent
        return None

    async def _unload_skill_modules(self, skill_name: str):
        """卸载技能模块"""
        modules_to_remove = [
            f".claude.skills.{skill_name}",
            f".claude.skills.{skill_name}.backtest",
            f".claude.skills.{skill_name}.prediction",
            # ... 其他子模块
        ]

        for module_name in modules_to_remove:
            if module_name in sys.modules:
                del sys.modules[module_name]
                logger.debug(f"🗑️ 卸载模块: {module_name}")

    async def _load_skill_directory(self, skill_dir: Path):
        """加载技能目录"""
        if not skill_dir.exists():
            logger.warning(f"⚠️ 技能目录不存在: {skill_dir}")
            return

        for py_file in skill_dir.glob("*.py"):
            if py_file.name.startswith('_'):
                continue

            try:
                module_name = f".claude.skills.{skill_dir.name}.{py_file.stem}"
                spec = importlib.util.spec_from_file_location(module_name, py_file)

                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    spec.loader.exec_module(module)

                    logger.debug(f"📦 加载模块: {module_name}")
                    self.loaded_modules.add(module_name)

            except Exception as e:
                logger.error(f"❌ 加载技能模块失败 {py_file}: {str(e)}")

    def set_skill_registry(self, registry):
        """设置技能注册表"""
        self.skill_registry = registry

    async def refresh_all_skills(self):
        """刷新所有技能"""
        logger.info("🔄 刷新所有技能...")

        if self.skill_registry:
            await self._unload_all_modules()
            await self._load_all_skill_directories()
            await self.skill_registry.refresh_skills()

    async def _unload_all_modules(self):
        """卸载所有模块"""
        modules_to_remove = [
            name for name in sys.modules.keys()
            if name.startswith('.claude.skills.')
        ]

        for module_name in modules_to_remove:
            del sys.modules[module_name]

        self.loaded_modules.clear()

    async def _load_all_skill_directories(self):
        """加载所有技能目录"""
        for skill_dir in self.skill_directory.iterdir():
            if skill_dir.is_dir() and not skill_dir.name.startswith('__'):
                await self._load_skill_directory(skill_dir)


class HotReloadManager:
    """热重载管理器"""

    def __init__(self):
        self.skill_loader = DynamicSkillLoader()
        self.watchers = []
        self.running = False

    async def start_all_watchers(self):
        """启动所有监控器"""
        if self.running:
            return

        # 监控Skills
        await self.skill_loader.start_watching()

        self.running = True
        logger.info("🔥 热重载系统启动完成")

    async def stop_all_watchers(self):
        """停止所有监控器"""
        if not self.running:
            return

        await self.skill_loader.stop_watching()

        self.running = False
        logger.info("⏹️ 热重载系统停止")

    def set_skill_registry(self, registry):
        """设置技能注册表"""
        self.skill_loader.set_skill_registry(registry)

    async def force_refresh(self):
        """强制刷新所有组件"""
        logger.info("🔄 强制刷新所有组件...")

        await self.skill_loader.refresh_all_skills()

    def get_status(self) -> Dict[str, Any]:
        """获取热重载状态"""
        return {
            "running": self.running,
            "skill_loader": {
                "watching": self.skill_loader.observer is not None,
                "loaded_modules": len(self.skill_loader.loaded_modules),
                "skill_directory": str(self.skill_loader.skill_directory)
            }
        }


# 全局热重载管理器实例
hot_reload_manager = HotReloadManager()