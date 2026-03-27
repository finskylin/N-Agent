"""
Unified Hot Reload Manager
统一热加载管理器 - 支持 UI Schema、Skills、APIs、Agents 的动态热更新

特性:
1. 基于 watchdog 的文件监控
2. 支持 JSON/Python 配置文件热加载
3. 增量更新，最小化性能影响
4. 事件驱动的回调机制
5. 原子事务重载支持
6. 配置验证和自动回滚
7. 轮询式备用监控
"""

import os
import sys
import json
import asyncio
import hashlib
import shutil
import threading
import time
import importlib
import importlib.util
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable, Set
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from loguru import logger

# 通过环境变量控制是否启用 watchdog（Docker 环境建议设为 false 使用轮询）
import os
WATCHDOG_ENABLED = os.environ.get("WATCHDOG_ENABLED", "true").lower() == "true"

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileDeletedEvent
    WATCHDOG_AVAILABLE = True and WATCHDOG_ENABLED
    if not WATCHDOG_ENABLED:
        logger.info("watchdog 已通过环境变量禁用，使用轮询模式")
except ImportError:
    WATCHDOG_AVAILABLE = False
    Observer = None
    # Define a stub for FileSystemEventHandler when watchdog is not available
    class FileSystemEventHandler:
        """Stub class when watchdog is not installed"""
        pass
    logger.warning("watchdog not installed, hot reload disabled")


class ResourceType(Enum):
    """资源类型"""
    UI_SCHEMA = "ui_schema"
    SKILL = "skill"
    API = "api"
    AGENT = "agent"
    MCP = "mcp"
    CONFIG = "config"
    PLUGIN = "plugin"


class ReloadState(Enum):
    """重载状态"""
    IDLE = "idle"
    PREPARING = "preparing"
    VALIDATING = "validating"
    APPLYING = "applying"
    ROLLING_BACK = "rolling_back"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class HotReloadEvent:
    """热加载事件"""
    resource_type: ResourceType
    resource_name: str
    action: str  # "created", "modified", "deleted"
    file_path: str
    timestamp: float
    data: Optional[Any] = None


@dataclass
class ResourceSnapshot:
    """资源快照 - 用于回滚"""
    resource_id: str
    resource_type: ResourceType
    content: Any
    content_hash: str
    timestamp: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReloadTransaction:
    """重载事务"""
    transaction_id: str
    state: ReloadState
    resources: List[str]
    snapshots: Dict[str, ResourceSnapshot] = field(default_factory=dict)
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    error: Optional[str] = None
    rollback_performed: bool = False


@dataclass
class WatchConfig:
    """监控配置"""
    path: str
    resource_type: ResourceType
    pattern: str = "*.json"
    recursive: bool = True
    debounce_seconds: float = 120.0


class HotReloadEventHandler(FileSystemEventHandler):
    """文件系统事件处理器"""

    def __init__(
        self,
        watch_config: WatchConfig,
        callback: Callable[[HotReloadEvent], None]
    ):
        self.watch_config = watch_config
        self.callback = callback
        self._last_events: Dict[str, float] = {}
        self._debounce_seconds = watch_config.debounce_seconds

    def _should_process(self, file_path: str) -> bool:
        """检查是否应该处理事件 (去抖动)"""
        import time
        current_time = time.time()

        if file_path in self._last_events:
            if current_time - self._last_events[file_path] < self._debounce_seconds:
                return False

        self._last_events[file_path] = current_time
        return True

    def _matches_pattern(self, file_path: str) -> bool:
        """检查文件是否匹配模式"""
        from fnmatch import fnmatch
        return fnmatch(Path(file_path).name, self.watch_config.pattern)

    def _create_event(self, src_path: str, action: str) -> HotReloadEvent:
        """创建热加载事件"""
        import time
        return HotReloadEvent(
            resource_type=self.watch_config.resource_type,
            resource_name=Path(src_path).stem,
            action=action,
            file_path=src_path,
            timestamp=time.time()
        )

    def on_modified(self, event):
        if event.is_directory:
            return
        if not self._matches_pattern(event.src_path):
            return
        if not self._should_process(event.src_path):
            return

        logger.info(f"🔥 检测到文件修改: {event.src_path}")
        hot_event = self._create_event(event.src_path, "modified")
        self.callback(hot_event)

    def on_created(self, event):
        if event.is_directory:
            return
        if not self._matches_pattern(event.src_path):
            return
        if not self._should_process(event.src_path):
            return

        logger.info(f"🆕 检测到新文件: {event.src_path}")
        hot_event = self._create_event(event.src_path, "created")
        self.callback(hot_event)

    def on_deleted(self, event):
        if event.is_directory:
            return
        if not self._matches_pattern(event.src_path):
            return

        logger.info(f"🗑️ 检测到文件删除: {event.src_path}")
        hot_event = self._create_event(event.src_path, "deleted")
        self.callback(hot_event)


class UISchemaHotReloader:
    """UI Schema 热加载器"""

    def __init__(self, config_path: str = "app/config/ui"):
        self.config_path = Path(config_path)
        self._cache: Dict[str, Any] = {}

    def reload_schemas(self, event: HotReloadEvent) -> bool:
        """重新加载 UI Schema"""
        try:
            file_path = Path(event.file_path)

            if event.action == "deleted":
                # 删除缓存
                if event.resource_name in self._cache:
                    del self._cache[event.resource_name]
                logger.info(f"🗑️ 移除 UI Schema: {event.resource_name}")
                return True

            # 读取 JSON 配置
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 更新注册表
            self._update_registry(data, event.resource_name)

            # 更新缓存
            self._cache[event.resource_name] = data

            logger.info(f"✅ UI Schema 热加载成功: {event.resource_name}")
            return True

        except Exception as e:
            logger.error(f"❌ UI Schema 热加载失败: {e}")
            return False

    def _update_registry(self, data: Dict[str, Any], source: str):
        """更新 UI Schema 注册表"""
        try:
            from app.agent.ui_schema_registry import (
                ui_schema_registry,
                UIComponentSchema,
                FieldType,
                FieldPattern
            )

            components = data.get("components", [])

            for comp in components:
                # 转换字段类型
                field_types = {}
                for field_name, type_str in comp.get("field_types", {}).items():
                    try:
                        field_types[field_name] = FieldType(type_str)
                    except ValueError:
                        field_types[field_name] = FieldType.ANY

                # 转换字段模式
                field_patterns = {}
                for field_name, pattern_dict in comp.get("field_patterns", {}).items():
                    field_patterns[field_name] = FieldPattern(
                        min_value=pattern_dict.get("min"),
                        max_value=pattern_dict.get("max"),
                        enum_values=pattern_dict.get("enum"),
                        min_items=pattern_dict.get("min_items"),
                        item_fields=pattern_dict.get("item_fields")
                    )

                # 创建 Schema
                schema = UIComponentSchema(
                    component_type=comp.get("type"),
                    name=comp.get("name"),
                    description=comp.get("description", ""),
                    required_fields=comp.get("required_fields", []),
                    optional_fields=comp.get("optional_fields", []),
                    field_types=field_types,
                    field_patterns=field_patterns,
                    field_aliases=comp.get("field_aliases", {}),
                    priority=comp.get("priority", 50),
                    category=comp.get("category", "general")
                )

                # 注册
                ui_schema_registry.register(schema)
                logger.debug(f"  📦 注册组件: {comp.get('type')}")

        except Exception as e:
            logger.error(f"更新 UI Schema 注册表失败: {e}")
            raise

    def load_all(self):
        """加载所有 UI Schema 配置"""
        if not self.config_path.exists():
            logger.warning(f"UI Schema 配置目录不存在: {self.config_path}")
            return

        for json_file in self.config_path.glob("*.json"):
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                self._update_registry(data, json_file.stem)
                self._cache[json_file.stem] = data
                logger.info(f"📦 加载 UI Schema: {json_file.name}")

            except Exception as e:
                logger.error(f"加载 UI Schema 失败 {json_file}: {e}")


class AgentConfigHotReloader:
    """Agent 配置热加载器"""

    def __init__(self, config_path: str = "config/agents"):
        self.config_path = Path(config_path)
        self._cache: Dict[str, Any] = {}

    def reload_config(self, event: HotReloadEvent) -> bool:
        """重新加载 Agent 配置"""
        try:
            file_path = Path(event.file_path)

            if event.action == "deleted":
                if event.resource_name in self._cache:
                    del self._cache[event.resource_name]
                self._remove_from_registry(event.resource_name)
                logger.info(f"🗑️ 移除 Agent 配置: {event.resource_name}")
                return True

            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self._update_registry(data, event.resource_name)
            self._cache[event.resource_name] = data

            logger.info(f"✅ Agent 配置热加载成功: {event.resource_name}")
            return True

        except Exception as e:
            logger.error(f"❌ Agent 配置热加载失败: {e}")
            return False

    def _update_registry(self, data: Dict[str, Any], source: str):
        """更新 Agent 注册表"""
        try:
            from app.agent.external_agent_registry import external_agent_registry, ExternalAgentConfig, AgentType

            agents = data.get("agents", [data]) if "agents" in data else [data]

            for agent_data in agents:
                if not agent_data.get("name"):
                    continue

                # 解析 Agent 类型
                try:
                    agent_type = AgentType(agent_data.get("type", "custom"))
                except ValueError:
                    agent_type = AgentType.CUSTOM

                config = ExternalAgentConfig(
                    name=agent_data.get("name"),
                    type=agent_type,
                    endpoint=agent_data.get("endpoint", ""),
                    description=agent_data.get("description", ""),
                    capabilities=agent_data.get("capabilities", []),
                    ui_hint=agent_data.get("ui_hint"),
                    core_fields=agent_data.get("core_fields", []),
                    auth_required=agent_data.get("auth_required", False),
                    timeout=agent_data.get("timeout", 30)
                )

                external_agent_registry.register(config)
                logger.debug(f"  📦 注册 Agent: {config.name}")

        except ImportError:
            logger.warning("external_agent_registry 不可用")
        except Exception as e:
            logger.error(f"更新 Agent 注册表失败: {e}")

    def _remove_from_registry(self, agent_name: str):
        """从注册表移除 Agent"""
        try:
            from app.agent.external_agent_registry import external_agent_registry
            external_agent_registry.unregister(agent_name)
        except Exception as e:
            logger.debug(f"移除 Agent 失败: {e}")

    def load_all(self):
        """加载所有 Agent 配置"""
        if not self.config_path.exists():
            logger.warning(f"Agent 配置目录不存在: {self.config_path}")
            return

        for json_file in self.config_path.glob("*.json"):
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                self._update_registry(data, json_file.stem)
                self._cache[json_file.stem] = data
                logger.info(f"📦 加载 Agent 配置: {json_file.name}")

            except Exception as e:
                logger.error(f"加载 Agent 配置失败 {json_file}: {e}")


class SkillHotReloader:
    """Skill 热加载器"""

    def __init__(self, skill_path: str = ".claude/skills"):
        self.skill_path = Path(skill_path)
        self._loaded_modules: Set[str] = set()

    def reload_skill(self, event: HotReloadEvent) -> bool:
        """重新加载 Skill"""
        try:
            file_path = Path(event.file_path)

            # 获取 skill 目录名
            skill_dir = self._get_skill_directory(file_path)
            if not skill_dir:
                return False

            skill_name = skill_dir.name

            if event.action == "deleted":
                self._unload_skill(skill_name)
                logger.info(f"🗑️ 卸载 Skill: {skill_name}")
                return True

            # 重新加载
            self._unload_skill(skill_name)
            self._load_skill_directory(skill_dir)

            # Update Unified Registry
            self._update_unified_registry(skill_dir)

            # 刷新注册表
            self._refresh_skill_registry()

            logger.info(f"✅ Skill 热加载成功: {skill_name}")
            return True

        except Exception as e:
            logger.error(f"❌ Skill 热加载失败: {e}")
            return False

    def _update_unified_registry(self, skill_dir: Path):
        """Update UnifiedResourceRegistry for this skill"""
        try:
            from app.agent.resource_registry import UnifiedResourceRegistry, ResourceType, ResourceMetadata
            skill_path = skill_dir / "SKILL.md"
            if not skill_path.exists():
                return
                
            with open(skill_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
            parts = content.split('---', 2)
            if len(parts) >= 3:
                import yaml
                frontmatter = yaml.safe_load(parts[1])
                name = frontmatter.get("name")
                if name:
                    metadata = ResourceMetadata(
                        name=name,
                        description=frontmatter.get("description", ""),
                        enabled=frontmatter.get("enabled", True),
                        config={
                             "path": str(skill_path),
                             "frontmatter": frontmatter,
                             "content": parts[2]
                        }
                    )
                    UnifiedResourceRegistry.get_instance().register_resource(
                        ResourceType.SKILL, name, content, metadata
                    )
                    logger.info(f"Updated UnifiedRegistry for skill: {name}")
        except Exception as e:
            logger.error(f"Failed to update unified registry for skill {skill_dir.name}: {e}")

    def _get_skill_directory(self, file_path: Path) -> Optional[Path]:
        """获取包含文件的技能目录"""
        current = file_path.parent
        while current != self.skill_path and current.parent != current:
            if current.parent == self.skill_path:
                return current
            current = current.parent
        return None

    def _unload_skill(self, skill_name: str):
        """卸载 Skill 模块"""
        modules_to_remove = [
            name for name in sys.modules.keys()
            if f".claude.skills.{skill_name}" in name or name.startswith(f"{skill_name}.")
        ]

        for module_name in modules_to_remove:
            del sys.modules[module_name]
            self._loaded_modules.discard(module_name)
            logger.debug(f"  🗑️ 卸载模块: {module_name}")

    def _load_skill_directory(self, skill_dir: Path):
        """加载 Skill 目录"""
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

                    self._loaded_modules.add(module_name)
                    logger.debug(f"  📦 加载模块: {module_name}")

            except Exception as e:
                logger.error(f"加载 Skill 模块失败 {py_file}: {e}")

    def _refresh_skill_registry(self):
        """刷新 Skill 注册表"""
        try:
            from .claude.skills.registry import registry
            if hasattr(registry, 'refresh_skills'):
                asyncio.create_task(registry.refresh_skills())
        except ImportError:
            pass

    def load_all(self):
        """加载所有 Skills"""
        if not self.skill_path.exists():
            logger.warning(f"Skill 目录不存在: {self.skill_path}")
            return

        for skill_dir in self.skill_path.iterdir():
            if skill_dir.is_dir() and not skill_dir.name.startswith('__'):
                self._load_skill_directory(skill_dir)
                logger.info(f"📦 加载 Skill: {skill_dir.name}")


class UnifiedHotReloadManager:
    """
    统一热加载管理器

    管理所有类型资源的热加载:
    - UI Schema
    - Skills
    - APIs
    - Agents
    - MCP Tools

    增强特性:
    - 原子事务重载
    - 配置验证
    - 自动回滚
    - 轮询备用监控
    """

    _instance: Optional["UnifiedHotReloadManager"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._manager_initialized = False
        return cls._instance

    def __init__(self, project_root: str = "."):
        if getattr(self, '_manager_initialized', False):
            return

        self.project_root = Path(project_root)
        self.observer: Optional[Observer] = None
        self.running = False

        # 配置加载
        self._config = self._load_config()

        # 子加载器
        self.ui_reloader = UISchemaHotReloader(
            str(self.project_root / "app" / "config" / "ui")
        )
        self.agent_reloader = AgentConfigHotReloader(
            str(self.project_root / "config" / "agents")
        )
        self.skill_reloader = SkillHotReloader(
            str(self.project_root / ".claude" / "skills")
        )

        # 事件回调
        self._event_listeners: List[Callable[[HotReloadEvent], None]] = []

        # 原子事务支持
        self._transactions: Dict[str, ReloadTransaction] = {}
        self._snapshots: Dict[str, ResourceSnapshot] = {}
        self._lock = threading.RLock()

        # 备份目录
        self._backup_dir = self.project_root / "app" / "data" / "backup" / "hot_reload"
        self._backup_dir.mkdir(parents=True, exist_ok=True)

        # 轮询监控器 (备用)
        self._polling_thread: Optional[threading.Thread] = None
        self._polling_running = False
        self._file_hashes: Dict[str, str] = {}

        # 监控配置
        self._watch_configs: List[WatchConfig] = [
            WatchConfig(
                path=str(self.project_root / "app" / "config" / "ui"),
                resource_type=ResourceType.UI_SCHEMA,
                pattern="*.json"
            ),
            WatchConfig(
                path=str(self.project_root / "config" / "agents"),
                resource_type=ResourceType.AGENT,
                pattern="*.json"
            ),
            WatchConfig(
                path=str(self.project_root / ".claude" / "skills"),
                resource_type=ResourceType.SKILL,
                pattern="*.py",
                recursive=True
            ),
            WatchConfig(
                path=str(self.project_root / ".claude" / "skills"),
                resource_type=ResourceType.SKILL,
                pattern="SKILL.md",
                recursive=True
            ),
            WatchConfig(
                path=str(self.project_root / "config" / "apis"),
                resource_type=ResourceType.API,
                pattern="*.json"
            ),
            WatchConfig(
                path=str(self.project_root / "agent_core" / "config" / "mcp"),
                resource_type=ResourceType.MCP,
                pattern="*.json"
            )
        ]

        self._manager_initialized = True

    def _load_config(self) -> Dict[str, Any]:
        """从配置文件加载热加载设置"""
        config_path = self.project_root / "config" / "system" / "settings.json"
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return data.get("hot_reload", {})
            except Exception as e:
                logger.error(f"Failed to load hot reload config: {e}")
        return {}

    @property
    def atomic_reload_enabled(self) -> bool:
        """是否启用原子重载"""
        return self._config.get("atomic_reload", True)

    @property
    def backup_before_reload(self) -> bool:
        """重载前是否备份"""
        return self._config.get("backup_before_reload", True)

    @property
    def debounce_ms(self) -> int:
        """防抖延迟(毫秒)"""
        return self._config.get("debounce_ms", 500)

    @property
    def poll_interval_seconds(self) -> float:
        """轮询间隔(秒)"""
        return self._config.get("poll_interval_seconds", 120.0)

    def add_event_listener(self, callback: Callable[[HotReloadEvent], None]):
        """添加事件监听器"""
        self._event_listeners.append(callback)

    def _handle_event(self, event: HotReloadEvent):
        """处理热加载事件"""
        if self.atomic_reload_enabled:
            self._atomic_handle_event(event)
        else:
            self._simple_handle_event(event)

    def _simple_handle_event(self, event: HotReloadEvent):
        """简单处理热加载事件（非原子）"""
        success = False

        # 根据资源类型分发
        if event.resource_type == ResourceType.UI_SCHEMA:
            success = self.ui_reloader.reload_schemas(event)
        elif event.resource_type == ResourceType.AGENT:
            success = self.agent_reloader.reload_config(event)
        elif event.resource_type == ResourceType.SKILL:
            success = self.skill_reloader.reload_skill(event)
        elif event.resource_type == ResourceType.API:
            success = self._reload_api_config(event)
        elif event.resource_type == ResourceType.MCP:
            success = self._reload_mcp_config(event)

        # 通知监听器
        for listener in self._event_listeners:
            try:
                listener(event)
            except Exception as e:
                logger.error(f"事件监听器错误: {e}")

    def _atomic_handle_event(self, event: HotReloadEvent):
        """原子事务处理热加载事件"""
        import uuid
        transaction_id = str(uuid.uuid4())[:8]
        resource_id = event.file_path

        transaction = ReloadTransaction(
            transaction_id=transaction_id,
            state=ReloadState.PREPARING,
            resources=[resource_id]
        )
        self._transactions[transaction_id] = transaction

        try:
            # 1. 创建快照
            transaction.state = ReloadState.PREPARING
            if self.backup_before_reload and event.action != "created":
                snapshot = self._create_snapshot(resource_id, event.resource_type)
                if snapshot:
                    transaction.snapshots[resource_id] = snapshot
                    self._backup_snapshot(snapshot)

            # 2. 验证新内容
            transaction.state = ReloadState.VALIDATING
            if event.action != "deleted":
                errors = self._validate_content(event.file_path, event.resource_type)
                if errors:
                    raise ValueError(f"Validation failed: {errors}")

            # 3. 应用变更
            transaction.state = ReloadState.APPLYING
            self._simple_handle_event(event)

            # 4. 完成
            transaction.state = ReloadState.COMPLETED
            transaction.end_time = datetime.now()

            logger.info(f"✅ 原子重载完成: {event.file_path}")

        except Exception as e:
            logger.error(f"❌ 原子重载失败: {e}")
            transaction.state = ReloadState.FAILED
            transaction.error = str(e)

            # 回滚
            if transaction.snapshots:
                self._rollback(transaction)

    def _create_snapshot(
        self,
        resource_id: str,
        resource_type: ResourceType
    ) -> Optional[ResourceSnapshot]:
        """创建资源快照"""
        file_path = Path(resource_id)
        if not file_path.exists():
            return None

        try:
            content = file_path.read_text(encoding='utf-8')
            if file_path.suffix == '.json':
                content = json.loads(content)

            content_bytes = json.dumps(content, ensure_ascii=False).encode('utf-8') \
                if isinstance(content, dict) else content.encode('utf-8')

            return ResourceSnapshot(
                resource_id=resource_id,
                resource_type=resource_type,
                content=content,
                content_hash=hashlib.md5(content_bytes).hexdigest(),
                timestamp=datetime.now()
            )
        except Exception as e:
            logger.error(f"创建快照失败: {e}")
            return None

    def _backup_snapshot(self, snapshot: ResourceSnapshot):
        """备份快照到磁盘"""
        backup_file = self._backup_dir / f"{snapshot.content_hash}.json"
        try:
            with open(backup_file, 'w', encoding='utf-8') as f:
                json.dump({
                    "resource_id": snapshot.resource_id,
                    "resource_type": snapshot.resource_type.value,
                    "content": snapshot.content,
                    "timestamp": snapshot.timestamp.isoformat()
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"备份快照失败: {e}")

    def _validate_content(
        self,
        file_path: str,
        resource_type: ResourceType
    ) -> List[str]:
        """验证配置内容"""
        errors = []
        path = Path(file_path)

        if not path.exists():
            return errors

        try:
            if path.suffix == '.json':
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                # 基本结构验证
                if resource_type in (ResourceType.CONFIG, ResourceType.UI_SCHEMA, ResourceType.MCP):
                    if "version" not in data and "components" not in data:
                        errors.append("Missing required fields")

                # JSON Schema 验证
                schema_path = self.project_root / "config" / "schemas" / f"{resource_type.value}.schema.json"
                if schema_path.exists():
                    try:
                        import jsonschema
                        with open(schema_path, 'r', encoding='utf-8') as f:
                            schema = json.load(f)
                        validator = jsonschema.Draft7Validator(schema)
                        for error in validator.iter_errors(data):
                            errors.append(str(error.message))
                    except ImportError:
                        pass

        except json.JSONDecodeError as e:
            errors.append(f"JSON parse error: {e}")
        except Exception as e:
            errors.append(str(e))

        return errors

    def _rollback(self, transaction: ReloadTransaction):
        """回滚事务"""
        transaction.state = ReloadState.ROLLING_BACK
        logger.warning(f"🔙 回滚事务 {transaction.transaction_id}")

        for resource_id, snapshot in transaction.snapshots.items():
            try:
                file_path = Path(snapshot.resource_id)
                if isinstance(snapshot.content, dict):
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump(snapshot.content, f, ensure_ascii=False, indent=2)
                elif isinstance(snapshot.content, str):
                    file_path.write_text(snapshot.content, encoding='utf-8')

                logger.info(f"✅ 已回滚: {resource_id}")
            except Exception as e:
                logger.error(f"回滚失败 {resource_id}: {e}")

        transaction.rollback_performed = True

    def _reload_mcp_config(self, event: HotReloadEvent) -> bool:
        """重新加载 MCP 配置"""
        try:
            file_path = Path(event.file_path)

            if event.action == "deleted":
                logger.info(f"🗑️ MCP 配置已删除: {event.resource_name}")
                return True

            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 通知 MCP 管理器重新加载
            try:
                from app.agent.mcp_integration import reload_mcp_servers
                reload_mcp_servers(data)
            except ImportError:
                logger.debug("MCP integration module not available")

            logger.info(f"✅ MCP 配置热加载成功: {event.resource_name}")
            return True

        except Exception as e:
            logger.error(f"❌ MCP 配置热加载失败: {e}")
            return False

    # 轮询监控支持 (备用方案)
    def start_polling_watcher(self):
        """启动轮询监控 (当 watchdog 不可用时)"""
        if self._polling_running:
            return

        self._polling_running = True
        self._init_file_hashes()

        self._polling_thread = threading.Thread(target=self._polling_loop, daemon=True)
        self._polling_thread.start()
        logger.info("🔄 启动轮询监控")

    def stop_polling_watcher(self):
        """停止轮询监控"""
        self._polling_running = False
        if self._polling_thread:
            self._polling_thread.join(timeout=5)
        logger.info("⏹️ 停止轮询监控")

    def _init_file_hashes(self):
        """初始化文件哈希"""
        for config in self._watch_configs:
            path = Path(config.path)
            if not path.exists():
                continue

            for file_path in self._glob_files(path, config.pattern, config.recursive):
                self._file_hashes[str(file_path)] = self._compute_file_hash(file_path)

    def _glob_files(self, base_path: Path, pattern: str, recursive: bool) -> List[Path]:
        """获取匹配的文件列表"""
        if recursive:
            return list(base_path.rglob(pattern))
        return list(base_path.glob(pattern))

    def _compute_file_hash(self, file_path: Path) -> str:
        """计算文件哈希"""
        try:
            return hashlib.md5(file_path.read_bytes()).hexdigest()
        except Exception:
            return ""

    def _polling_loop(self):
        """轮询循环"""
        while self._polling_running:
            try:
                for config in self._watch_configs:
                    path = Path(config.path)
                    if not path.exists():
                        continue

                    current_files: Dict[str, str] = {}
                    for file_path in self._glob_files(path, config.pattern, config.recursive):
                        current_files[str(file_path)] = self._compute_file_hash(file_path)

                    # 检查新增和修改
                    for file_path, new_hash in current_files.items():
                        old_hash = self._file_hashes.get(file_path)
                        if old_hash is None:
                            self._trigger_event(file_path, "created", config.resource_type)
                        elif old_hash != new_hash:
                            self._trigger_event(file_path, "modified", config.resource_type)

                    # 检查删除
                    for file_path in set(self._file_hashes.keys()) - set(current_files.keys()):
                        if file_path.startswith(str(path)):
                            self._trigger_event(file_path, "deleted", config.resource_type)

                    # 更新哈希
                    for file_path in current_files:
                        self._file_hashes[file_path] = current_files[file_path]

            except Exception as e:
                logger.error(f"轮询监控错误: {e}")

            time.sleep(self.poll_interval_seconds)

    def _trigger_event(self, file_path: str, action: str, resource_type: ResourceType):
        """触发热加载事件"""
        event = HotReloadEvent(
            resource_type=resource_type,
            resource_name=Path(file_path).stem,
            action=action,
            file_path=file_path,
            timestamp=time.time()
        )
        self._handle_event(event)

    def get_transactions(self) -> List[Dict[str, Any]]:
        """获取事务历史"""
        return [
            {
                "transaction_id": t.transaction_id,
                "state": t.state.value,
                "resources": t.resources,
                "start_time": t.start_time.isoformat(),
                "end_time": t.end_time.isoformat() if t.end_time else None,
                "error": t.error,
                "rollback_performed": t.rollback_performed
            }
            for t in self._transactions.values()
        ]

    def _reload_api_config(self, event: HotReloadEvent) -> bool:
        """重新加载 API 配置"""
        try:
            from app.agent.api_registry import api_registry

            file_path = Path(event.file_path)

            if event.action == "deleted":
                # 处理删除
                logger.info(f"🗑️ API 配置已删除: {event.resource_name}")
                return True

            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 更新 API 注册表
            apis = data.get("apis", [data]) if "apis" in data else [data]
            for api_data in apis:
                if api_data.get("name"):
                    api_registry.register_from_dict(api_data)
                    logger.debug(f"  📦 注册 API: {api_data.get('name')}")

            logger.info(f"✅ API 配置热加载成功: {event.resource_name}")
            return True

        except Exception as e:
            logger.error(f"❌ API 配置热加载失败: {e}")
            return False

    async def start(self):
        """启动热加载系统"""
        if self.running:
            return

        # 检查是否完全禁用热加载
        hot_reload_enabled = os.environ.get("HOT_RELOAD_ENABLED", "true").lower() == "true"
        if not hot_reload_enabled:
            logger.info("🔒 热加载已通过环境变量禁用 (HOT_RELOAD_ENABLED=false)")
            # 仍然执行首次加载，但跳过文件监控
            self.ui_reloader.load_all()
            self.agent_reloader.load_all()
            self.skill_reloader.load_all()
            self.running = True
            logger.info("✅ 首次加载完成，监控功能已禁用")
            return

        logger.info("🔥 启动统一热加载系统...")

        # 首次加载所有配置
        self.ui_reloader.load_all()
        self.agent_reloader.load_all()
        self.skill_reloader.load_all()

        if WATCHDOG_AVAILABLE:
            # 创建观察者
            self.observer = Observer()

            # 注册监控
            for config in self._watch_configs:
                path = Path(config.path)
                if not path.exists():
                    logger.warning(f"⚠️ 监控路径不存在: {config.path}")
                    continue

                handler = HotReloadEventHandler(config, self._handle_event)
                self.observer.schedule(handler, str(path), recursive=config.recursive)
                logger.info(f"👁️ 监控: {config.path} ({config.resource_type.value})")

            # 启动观察者
            self.observer.start()
            logger.info("✅ Watchdog 监控启动完成")
        else:
            # 使用轮询备用方案
            logger.warning("⚠️ watchdog 未安装，使用轮询监控备用方案")
            self.start_polling_watcher()

        self.running = True
        logger.info("✅ 统一热加载系统启动完成")

    async def stop(self):
        """停止热加载系统"""
        if not self.running:
            return

        logger.info("⏹️ 停止统一热加载系统...")

        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None

        if self._polling_running:
            self.stop_polling_watcher()

        self.running = False
        logger.info("✅ 统一热加载系统已停止")

    async def refresh_all(self):
        """强制刷新所有配置"""
        logger.info("🔄 强制刷新所有配置...")

        self.ui_reloader.load_all()
        self.agent_reloader.load_all()
        self.skill_reloader.load_all()

        logger.info("✅ 所有配置刷新完成")

    def get_status(self) -> Dict[str, Any]:
        """获取热加载状态"""
        return {
            "running": self.running,
            "watchdog_available": WATCHDOG_AVAILABLE,
            "polling_active": self._polling_running,
            "atomic_reload_enabled": self.atomic_reload_enabled,
            "backup_before_reload": self.backup_before_reload,
            "ui_schemas_cached": len(self.ui_reloader._cache),
            "agents_cached": len(self.agent_reloader._cache),
            "skills_loaded": len(self.skill_reloader._loaded_modules),
            "transactions_count": len(self._transactions),
            "watch_configs": [
                {
                    "path": c.path,
                    "type": c.resource_type.value,
                    "pattern": c.pattern
                }
                for c in self._watch_configs
            ]
        }


# 全局实例
unified_hot_reload_manager = UnifiedHotReloadManager()


async def start_unified_hot_reload():
    """启动统一热加载"""
    await unified_hot_reload_manager.start()


async def stop_unified_hot_reload():
    """停止统一热加载"""
    await unified_hot_reload_manager.stop()


def get_hot_reload_manager():
    """Get the singleton instance of UnifiedHotReloadManager"""
    return unified_hot_reload_manager
