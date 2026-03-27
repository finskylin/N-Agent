"""
Stock Agent Service - V2 SDK 架构
FastAPI 入口

集成功能：
- Skills 自动加载
- 统一资源注册表 (ResourceManager)
- UI 资源绑定 (UIBinder)
- 热加载管理器 (V2)
- 插件自动发现
- 健康监控系统
"""
import sys
import os
import asyncio
from pathlib import Path
from loguru import logger
from contextlib import asynccontextmanager

# Add project root and .claude to Python path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

claude_path = project_root / ".claude"
if str(claude_path) not in sys.path:
    sys.path.insert(0, str(claude_path))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.api import api_router
from app.api.auth import router as auth_router
from app.api.config_admin import router as config_admin_router
from app.api.knowledge import router as knowledge_router
from app.api.tile_proxy import router as tile_proxy_router
from app.api.tasks import router as tasks_router
from app.channels.feishu.webhook import router as feishu_router

# 配置日志
from app.utils.tracing import configure_logging
configure_logging()

# 全局实例
_sdk_cleanup_task = None
_cron_service = None

# 后台任务管理器（统一追踪所有 fire-and-forget 任务）
_background_task_manager = None
_sdk_cleanup_task = None  # SDK 孤儿进程清理定时任务


def get_cron_service():
    """返回全局 CronService 实例（用于 agent 层获取，启动前返回 None）"""
    return _cron_service


async def _cleanup_orphan_sdk_processes():
    """
    定时清理 claude_agent_sdk 泄漏的 bundled binary 子进程

    每个 SDK 子进程 ~340MB，如果 disconnect() 未正常调用（异常路径），
    子进程会成为孤儿进程持续占用内存。此任务每 3 分钟扫描一次，
    杀掉存活超过 10 分钟的 SDK 子进程。
    """
    import signal

    SCAN_INTERVAL = 180   # 每 3 分钟扫描一次
    MAX_AGE_SECONDS = 600  # 存活超过 10 分钟视为孤儿

    logger.info("[SDKCleanup] Orphan process cleanup task started")

    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL)

            # 扫描 /proc 下的进程（容器内可用）
            killed = 0
            import time as _time
            now = _time.time()

            proc_path = Path("/proc")
            if not proc_path.exists():
                # macOS 等非 Linux 环境，跳过
                continue

            for pid_dir in proc_path.iterdir():
                if not pid_dir.name.isdigit():
                    continue

                pid = int(pid_dir.name)
                if pid <= 1:
                    continue

                try:
                    # 读取进程命令行
                    cmdline_file = pid_dir / "cmdline"
                    if not cmdline_file.exists():
                        continue
                    cmdline = cmdline_file.read_bytes().decode("utf-8", errors="ignore")

                    # 匹配 claude_agent_sdk bundled binary 进程
                    if "claude_agent_sdk" not in cmdline and "claude-code" not in cmdline.lower():
                        continue

                    # 排除当前 Python 主进程自身
                    if "python" in cmdline.lower() or "uvicorn" in cmdline.lower():
                        continue

                    # 检查进程存活时间
                    stat_file = pid_dir / "stat"
                    if not stat_file.exists():
                        continue

                    # 从 /proc/[pid]/stat 获取启动时间（第 22 字段，单位 clock ticks）
                    stat_content = stat_file.read_text()
                    # 跳过 comm 字段（可能含空格），从最后一个 ')' 后开始解析
                    stat_after_comm = stat_content[stat_content.rfind(")") + 2:]
                    stat_fields = stat_after_comm.split()
                    if len(stat_fields) < 20:
                        continue

                    starttime_ticks = int(stat_fields[19])  # 第 22 字段，索引 19（从 0 开始，减去前 2 个字段）
                    clock_ticks = os.sysconf("SC_CLK_TCK")

                    # 获取系统启动时间
                    uptime_text = Path("/proc/uptime").read_text()
                    uptime_seconds = float(uptime_text.split()[0])

                    # 计算进程启动的绝对时间
                    boot_time = now - uptime_seconds
                    process_start = boot_time + (starttime_ticks / clock_ticks)
                    age_seconds = now - process_start

                    if age_seconds > MAX_AGE_SECONDS:
                        logger.warning(
                            f"[SDKCleanup] Killing orphan SDK process: "
                            f"pid={pid}, age={age_seconds:.0f}s ({age_seconds/60:.1f}min)"
                        )
                        try:
                            os.kill(pid, signal.SIGKILL)
                            killed += 1
                        except ProcessLookupError:
                            pass  # 进程已退出

                except (PermissionError, FileNotFoundError, ValueError, OSError):
                    continue

            if killed > 0:
                logger.info(f"[SDKCleanup] Cleaned up {killed} orphan SDK process(es)")

        except asyncio.CancelledError:
            logger.info("[SDKCleanup] Cleanup task cancelled")
            break
        except Exception as e:
            logger.warning(f"[SDKCleanup] Cleanup scan error: {e}")
            await asyncio.sleep(60)  # 出错后等 1 分钟再试


async def _clash_auto_select_proxy():
    """
    Clash 代理自动选节点：从新加坡节点中选延迟最低的可用节点。
    仅在配置了容器内 Clash（HTTP_PROXY=http://clash:7890）时生效。
    """
    proxy_url = os.environ.get("HTTP_PROXY", "")
    if "clash:7890" not in proxy_url:
        return  # 不是容器内 Clash，跳过

    clash_api = "http://clash:9090"
    clash_secret = "hitun"
    headers = {"Authorization": f"Bearer {clash_secret}"}
    proxy_group = "🔰 代理"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            # 获取所有节点
            resp = await client.get(f"{clash_api}/proxies", headers=headers)
            proxies_data = resp.json()
            group = proxies_data["proxies"].get(proxy_group, {})
            all_nodes = group.get("all", [])

            # 优先选新加坡节点（倍率1的最划算），依次测延迟
            candidates = [n for n in all_nodes if "新加坡" in n and "倍率" not in n]
            candidates += [n for n in all_nodes if "新加坡" in n and "倍率" in n]

            best_node = None
            best_delay = 9999
            for node in candidates[:8]:  # 最多测 8 个节点，避免启动太慢
                try:
                    encoded = httpx.URL(f"{clash_api}/proxies/{node}/delay").raw_path
                    delay_resp = await client.get(
                        f"{clash_api}/proxies/{node}/delay",
                        params={"url": "https://www.google.com", "timeout": 3000},
                        headers=headers,
                        timeout=8,
                    )
                    delay_data = delay_resp.json()
                    delay = delay_data.get("delay", 9999)
                    if delay > 0 and delay < best_delay:
                        best_delay = delay
                        best_node = node
                        logger.info(f"[ClashProxy] {node}: {delay}ms")
                except Exception:
                    pass  # 该节点不可用，跳过

            if best_node:
                # 切换到最优节点
                import urllib.parse
                encoded_group = urllib.parse.quote(proxy_group)
                await client.put(
                    f"{clash_api}/proxies/{encoded_group}",
                    headers=headers,
                    json={"name": best_node},
                )
                logger.info(f"[ClashProxy] Auto-selected: {best_node} ({best_delay}ms)")
            else:
                logger.warning("[ClashProxy] No available Singapore node, keeping current")
    except Exception as e:
        logger.warning(f"[ClashProxy] Auto-select failed: {e}")


async def init_systems():
    """初始化所有子系统"""
    global _background_task_manager

    # 0. Clash 代理自动选节点（容器内 Clash 模式）
    await _clash_auto_select_proxy()

    # 0. 初始化后台任务管理器（优先级最高）
    try:
        from app.utils.background_task_manager import BackgroundTaskManager
        _background_task_manager = BackgroundTaskManager("Global")
        logger.info("[BackgroundTaskManager] Initialized")
    except Exception as e:
        logger.error(f"Failed to initialize BackgroundTaskManager: {e}")

    # 数据库 ORM 表自动创建（SQLite/通用 SQLAlchemy）
    try:
        if getattr(settings, "database_url", ""):
            from app.db.database import engine as _db_engine
            from app.db.models_db import Base
            from app.agent.v4 import models as _v4_models  # noqa: F401

            async with _db_engine.begin() as conn:
                await conn.run_sync(lambda sync_conn: Base.metadata.create_all(bind=sync_conn, checkfirst=True))
            logger.info("[Database] ORM tables created/verified")
        else:
            logger.info("[Database] DATABASE_URL empty, ORM table creation skipped")
    except Exception as e:
        logger.warning(f"[Database] ORM table creation skipped: {e}")

    # 知识库目录结构初始化 + 建表 + 缓存预热
    try:
        import json as _kj
        _knowledge_config_path = project_root / "agent_core" / "config" / "knowledge" / "knowledge.json"
        if _knowledge_config_path.exists():
            _kconfig = _kj.loads(_knowledge_config_path.read_text(encoding="utf-8"))
            _knowledge_base = project_root / _kconfig.get("base_dir", "app/knowledge")
            for _cat in _kconfig.get("categories", []):
                _cat_dir = _knowledge_base / _cat["path"]
                _cat_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"[Knowledge] Directory structure ensured: {_knowledge_base}")

            _db_enabled = bool(getattr(settings, "database_url", ""))
            if _db_enabled:
                # Seed 系统预设分类到数据库
                try:
                    from app.services.knowledge_file_service import seed_system_categories
                    await seed_system_categories()
                    logger.info("[Knowledge] System categories seeded to database")
                except Exception as _se:
                    logger.warning(f"[Knowledge] Seed system categories failed: {_se}")
            else:
                logger.info("[Knowledge] DATABASE_URL empty, skipping database seed initialization")

            # 缓存预热 + 注入 PromptBuilder knowledge_provider
            try:
                from app.services.knowledge_file_service import KnowledgeMetadataCache
                _km_cache = KnowledgeMetadataCache.get_instance()
                _km_cache.get_directory_summaries()
                logger.info("[Knowledge] Metadata cache warmed up")
                # 解耦注入：agent_core/prompt_builder.py 不直接 import app 层
                from agent_core.prompt_builder import PromptBuilder
                PromptBuilder.set_knowledge_provider(_km_cache.get_directory_summaries)
            except Exception as _ce:
                logger.debug(f"[Knowledge] Cache warmup skipped: {_ce}")

            # 恢复卡住的 processing 文件
            try:
                from app.services.knowledge_file_service import recover_stuck_processing_files
                _recovered = await recover_stuck_processing_files(stuck_threshold_hours=1.0)
                if _recovered:
                    logger.info(f"[Knowledge] Recovered {_recovered} stuck processing file(s) on startup")
            except Exception as _re:
                logger.warning(f"[Knowledge] Stuck file recovery skipped: {_re}")
    except Exception as e:
        logger.warning(f"[Knowledge] Initialization skipped: {e}")

    # 8b-2. 注册知识源 Reader（供 agent_core 通过 KnowledgeSourceRegistry 读取）
    try:
        from agent_core.knowledge.source_registry import get_registry
        from app.services.knowledge_readers import LocalFileReader, MinioFileReader

        import json as _rkj
        _rk_config_path = project_root / "agent_core" / "config" / "knowledge" / "knowledge.json"
        _kreader_config = {}
        if _rk_config_path.exists():
            try:
                _kreader_config = _rkj.loads(_rk_config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        _kb_base = str(_kreader_config.get("base_dir", "app/knowledge"))

        _registry = get_registry()
        _registry.register(
            "local",
            LocalFileReader(base_dir=_kb_base),
            source_type="local",
            description="本地文件系统知识库",
        )
        logger.info("[KnowledgeRegistry] LocalFileReader registered")

        # MinIO（仅在配置了 endpoint 时注册）
        _minio_ep = getattr(settings, "minio_endpoint", "")
        if _minio_ep:
            _registry.register(
                "minio",
                MinioFileReader(),
                source_type="minio",
                description="MinIO 对象存储知识库",
            )
            logger.info("[KnowledgeRegistry] MinioFileReader registered")
    except Exception as e:
        logger.warning(f"[KnowledgeRegistry] Reader registration skipped: {e}")

    # 8c. 上传组件 bundle 到本地对象存储（供外网 HTML 引用）
    try:
        from app.services.file_generator import upload_file_to_local_storage, get_public_storage_url
        bundle_dir = project_root / "app" / "static" / "components"
        bundle_files = {
            "app/static/component-bundle.umd.js": ("component-bundle.umd.js", "application/javascript"),
            "app/static/component-bundle.css": ("component-bundle.css", "text/css"),
            "app/static/vue.global.prod.js": ("vue.global.prod.js", "application/javascript"),
            "app/static/main.css": ("main.css", "text/css"),
        }
        uploaded_any = False
        for storage_name, (local_name, content_type) in bundle_files.items():
            local_file = bundle_dir / local_name
            if local_file.exists():
                url = upload_file_to_local_storage(
                    file_data=local_file.read_bytes(),
                    user_id=0,
                    file_name=storage_name,
                    content_type=content_type,
                )
                if url:
                    uploaded_any = True
        if uploaded_any:
            storage_url = get_public_storage_url()
            bundle_base = f"{storage_url}/uploads/0/static"
            os.environ["COMPONENT_BUNDLE_BASE_URL"] = bundle_base
            logger.info(f"[Bundle] Uploaded to local storage, COMPONENT_BUNDLE_BASE_URL={bundle_base}")
        else:
            logger.warning("[Bundle] No bundle files found or upload failed, using local serving")
    except Exception as e:
        logger.warning(f"[Bundle] Upload failed, falling back to local: {e}")

    # 8d. 初始化应用内 Cron 调度器（仅 primary worker 运行，避免多 worker 重复触发）
    global _cron_service
    try:
        from app.channels.dingtalk.stream_client import _is_primary_worker as _dt_primary
        _should_run_cron = _dt_primary
        if not _should_run_cron:
            # 非钉钉 primary：用文件锁再检测一次（飞书模式或钉钉未启用时）
            import fcntl as _fcntl
            _cron_lock_path = project_root / "app" / "data" / "cron_scheduler.lock"
            _cron_lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                _cron_lock_f = open(_cron_lock_path, "w")
                _fcntl.flock(_cron_lock_f.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                _should_run_cron = True
                # 持有锁直到进程退出（不关闭文件）
            except (IOError, OSError):
                _should_run_cron = False

        if _should_run_cron:
            from app.api.chat_v4 import V4CronExecuteRequest, execute_cron_request
            from app.services.cron_service import init_cron_service

            async def _on_cron_job(job):
                # prediction 内置任务：直接调 Python 函数，不走 Agent 问答流程
                _cb = job.payload.callback or {}
                if _cb.get("type") == "prediction_verify":
                    try:
                        from app.services.prediction_task_runner import run_verify_cycle
                        result = await run_verify_cycle(
                            user_id=_cb.get("user_id", 1),
                            instance_id=_cb.get("instance_id", ""),
                        )
                        return f"prediction_verify done: {result}"
                    except Exception as _pe:
                        logger.warning(f"[CronService] prediction_verify failed: {_pe}")
                        return f"prediction_verify error: {_pe}"

                if _cb.get("type") == "strategy_learn":
                    try:
                        from app.services.prediction_task_runner import run_learn_cycle
                        result = await run_learn_cycle(
                            user_id=_cb.get("user_id", 1),
                            instance_id=_cb.get("instance_id", ""),
                        )
                        return f"strategy_learn done: {result}"
                    except Exception as _pe:
                        logger.warning(f"[CronService] strategy_learn failed: {_pe}")
                        return f"strategy_learn error: {_pe}"

                if _cb.get("type") == "prediction_incremental":
                    try:
                        from app.services.prediction_task_runner import run_incremental_cycle
                        result = await run_incremental_cycle(
                            user_id=_cb.get("user_id", 1),
                            instance_id=_cb.get("instance_id", ""),
                        )
                        return f"prediction_incremental done: {result}"
                    except Exception as _pe:
                        logger.warning(f"[CronService] prediction_incremental failed: {_pe}")
                        return f"prediction_incremental error: {_pe}"

                if _cb.get("type") == "skill_evolution":
                    try:
                        from app.agent.v4.native_agent import V4NativeAgent
                        _evolver = V4NativeAgent.get_skill_evolver()
                        if _evolver:
                            result = await _evolver.run_evolution_cycle(
                                user_id=_cb.get("user_id", 1),
                                instance_id=_cb.get("instance_id", ""),
                            )
                            return f"skill_evolution done: evolved={result}"
                        else:
                            return "skill_evolution skipped: evolver not initialized"
                    except Exception as _pe:
                        logger.warning(f"[CronService] skill_evolution failed: {_pe}")
                        return f"skill_evolution error: {_pe}"

                # 默认：走 Agent 问答流程
                request = V4CronExecuteRequest(
                    message=job.payload.message,
                    session_id=job.payload.session_id,
                    auto_approve_plan=job.payload.auto_approve_plan,
                    callback=job.payload.callback,
                    alert_threshold=getattr(job.payload, "alert_threshold", None),
                    silent_if_no_signal=getattr(job.payload, "silent_if_no_signal", False),
                )
                response = await execute_cron_request(request)
                return response.text or response.status or ""

            _cron_store_path = project_root / settings.cron_store_path
            _cron_service = await init_cron_service(
                store_path=_cron_store_path,
                on_job=_on_cron_job,
            )
            logger.info(f"[CronService] Initialized (primary worker pid={os.getpid()}), store={_cron_store_path}")

            # 注册预测验证 + 策略学习定时任务
            try:
                from app.services.prediction_task_runner import register_prediction_jobs
                from agent_core.config import V4Config
                from app.services.cron_service import CronSchedule, CronPayload
                _v4cfg = V4Config.from_env()
                _default_uid = getattr(settings, "v4_default_user_id", 1)
                _instance_id = _v4cfg.instance_id
                await register_prediction_jobs(
                    cron_service=_cron_service,
                    user_id=_default_uid,
                    instance_id=_instance_id,
                )
            except Exception as _pred_reg_err:
                logger.debug(f"[CronService] Prediction jobs registration skipped: {_pred_reg_err}")

            # 注册 SkillEvolver 定时任务（每天凌晨 3 点）
            try:
                _se_cron = os.getenv("SKILL_EVOLUTION_CRON", "0 3 * * *")
                _se_enabled = os.getenv("SKILL_EVOLUTION_ENABLED", "false").lower() in ("true", "1")
                if _se_enabled:
                    await _cron_service.add_job(
                        name=f"skill_evolution_{_default_uid}_{_instance_id}",
                        schedule=CronSchedule(kind="cron", cron_expr=_se_cron),
                        payload=CronPayload(
                            message="",
                            callback={
                                "type": "skill_evolution",
                                "user_id": _default_uid,
                                "instance_id": _instance_id,
                            },
                        ),
                    )
                    logger.info(f"[CronService] Registered skill_evolution job ({_se_cron})")
            except Exception as _se_err:
                logger.debug(f"[CronService] SkillEvolver job registration skipped: {_se_err}")
        else:
            logger.info(f"[CronService] Skipped (non-primary worker pid={os.getpid()})")
    except Exception as e:
        logger.warning(f"[CronService] Initialization failed: {e}")

    # 9a. 初始化 Langfuse 可观测平台（可选，失败不影响服务）
    try:
        from app.utils.langfuse_client import langfuse
        if langfuse.is_available:
            logger.info("[Langfuse] Observability platform connected")
        else:
            logger.info("[Langfuse] Not available, tracing disabled (service continues normally)")
    except Exception as e:
        logger.warning(f"[Langfuse] Init skipped: {e}")

    # 9b. 启动 SDK 孤儿进程清理定时任务
    global _sdk_cleanup_task
    try:
        from app.utils.background_task_manager import create_background_task
        _sdk_cleanup_task = create_background_task(
            _cleanup_orphan_sdk_processes(),
            task_name="sdk_orphan_cleanup"
        )
        logger.info("[SDKCleanup] Orphan process cleanup task scheduled (interval=3min, max_age=10min)")
    except Exception as e:
        logger.warning(f"[SDKCleanup] Failed to start cleanup task: {e}")

    # 9b-2. 启动 TaskMonitor（后台任务生命周期监控 + Agent 回调）
    try:
        from app.utils.background_task_manager import create_background_task
        from app.services.task_monitor import run_task_monitor
        create_background_task(run_task_monitor(), task_name="task_monitor")
        logger.info("[TaskMonitor] Background task monitor started")
    except Exception as e:
        logger.warning(f"[TaskMonitor] Failed to start: {e}")

    # 9c. 初始化钉钉 Stream 机器人（独立通道，可选功能，不影响现有 HTTP API）
    # 关键：钉钉连接失败不能影响主服务启动
    try:
        from app.channels.dingtalk.stream_client import start_dingtalk_stream
        # 整体初始化超时保护（防止卡住服务启动）
        init_timeout = int(os.environ.get("DINGTALK_INIT_TIMEOUT", 5))
        started = await asyncio.wait_for(
            start_dingtalk_stream(),
            timeout=init_timeout
        )
        if started:
            logger.info("[DingTalk] Stream bot started successfully (non-blocking)")
        else:
            logger.info("[DingTalk] Stream bot not started (disabled or not configured)")
    except asyncio.TimeoutError:
        logger.warning(f"[DingTalk] Initialization timeout, skipping (service continues normally)")
    except Exception as e:
        logger.warning(f"[DingTalk] Failed to start stream bot: {e} (service continues normally)")

    # 9c-2. 初始化飞书长连接机器人（可选，需配置 FEISHU_APP_ID / FEISHU_APP_SECRET）
    try:
        from app.channels.feishu.stream_client import start_feishu_stream
        feishu_init_timeout = int(os.environ.get("FEISHU_INIT_TIMEOUT", 5))
        feishu_started = await asyncio.wait_for(
            start_feishu_stream(),
            timeout=feishu_init_timeout,
        )
        if feishu_started:
            logger.info("[Feishu] Long-connection bot started successfully (non-blocking)")
        else:
            logger.info("[Feishu] Long-connection bot not started (disabled or not configured)")
    except asyncio.TimeoutError:
        logger.warning("[Feishu] Initialization timeout, skipping (service continues normally)")
    except Exception as e:
        logger.warning(f"[Feishu] Failed to start long-connection bot: {e} (service continues normally)")

    # 9d. 初始化 Channel Manager（插件化通道架构）
    try:
        from app.channels.manager import init_channel_manager
        from app.channels.dingtalk import DingTalkPlugin
        from app.agent.v4.native_agent import V4NativeAgent

        # 创建 Agent 工厂
        def _agent_factory():
            return V4NativeAgent()

        # 初始化 Channel Manager（注册到全局单例，供 bot_handler 等通过 get_channel_manager() 获取）
        _channel_manager = init_channel_manager(agent_factory=_agent_factory)

        # 注册钉钉插件（复用现有 stream_client，robot_code = dingtalk_client_id）
        from app.config import settings as _settings
        dingtalk_config = {
            "robot_code": _settings.dingtalk_client_id or "",
        }
        _channel_manager.register(DingTalkPlugin(dingtalk_config))

        # 注册飞书插件（可选，需配置 feishu_app_id / feishu_app_secret）
        if _settings.feishu_enabled and _settings.feishu_app_id and _settings.feishu_app_secret:
            try:
                from app.channels.feishu import FeishuPlugin
                from app.channels.feishu.bot_handler import _ensure_bot_handler
                _ensure_bot_handler()  # 初始化全局 handler
                feishu_config = {
                    "app_id": _settings.feishu_app_id,
                    "app_secret": _settings.feishu_app_secret,
                }
                _channel_manager.register(FeishuPlugin(feishu_config))
                logger.info("[ChannelManager] Feishu plugin registered")
            except Exception as fe:
                logger.warning(f"[ChannelManager] Failed to register Feishu plugin: {fe}")

        await _channel_manager.start_all()
        logger.info("[ChannelManager] DingTalk plugin registered and started")

    except Exception as e:
        logger.warning(f"[ChannelManager] Failed to initialize: {e} (service continues normally)")


async def shutdown_systems():
    """关闭所有子系统"""
    global _sdk_cleanup_task, _background_task_manager, _cron_service

    # 停止后台任务管理器（优先级最高，确保所有后台任务被取消）
    if _background_task_manager:
        try:
            await _background_task_manager.shutdown(timeout=30.0)
            logger.info("[BackgroundTaskManager] All background tasks stopped")
        except Exception as e:
            logger.error(f"[BackgroundTaskManager] Error during shutdown: {e}")

    # 停止 SDK 孤儿进程清理任务
    if _sdk_cleanup_task and not _sdk_cleanup_task.done():
        _sdk_cleanup_task.cancel()
        try:
            await _sdk_cleanup_task
        except asyncio.CancelledError:
            pass
        logger.info("[SDKCleanup] Cleanup task stopped")

    # 停止钉钉 Stream 客户端
    try:
        from app.channels.dingtalk.stream_client import stop_dingtalk_stream
        await stop_dingtalk_stream()
    except Exception as e:
        logger.warning(f"[DingTalk] Error stopping stream client: {e}")

    # 停止飞书长连接客户端
    try:
        from app.channels.feishu.stream_client import stop_feishu_stream
        await stop_feishu_stream()
    except Exception as e:
        logger.warning(f"[Feishu] Error stopping stream client: {e}")

    # 停止 Channel Manager
    try:
        from app.channels import get_channel_manager
        _channel_manager = get_channel_manager()
        if _channel_manager:
            await _channel_manager.stop_all()
            logger.info("[ChannelManager] All channels stopped")
    except Exception as e:
        logger.warning(f"[ChannelManager] Error stopping channels: {e}")

    # 停止浏览器服务（清理 Playwright 进程）
    try:
        from app.services.browser import browser_service
        await browser_service.close()
        logger.info("[Browser] Browser service stopped")
    except Exception as e:
        logger.warning(f"[Browser] Error stopping browser service: {e}")

    # 停止应用内 Cron 调度器
    try:
        from app.services.cron_service import shutdown_cron_service
        await shutdown_cron_service()
        _cron_service = None
        logger.info("[CronService] Stopped")
    except Exception as e:
        logger.warning(f"[CronService] Error stopping scheduler: {e}")

    # Langfuse: 刷新缓冲区（确保 observability 数据不丢失）
    try:
        from app.utils.langfuse_client import langfuse
        langfuse.flush()
        logger.info("[Langfuse] Observability data flushed")
    except Exception as e:
        logger.warning(f"[Langfuse] Error flushing: {e}")

    # 关闭 LLM HTTP 连接池
    try:
        from agent_core.agentloop.llm_provider import shutdown_http_pools
        await shutdown_http_pools()
        logger.info("[LLMProvider] HTTP connection pools closed")
    except Exception as e:
        logger.warning(f"[LLMProvider] Error closing HTTP pools: {e}")

    # 关闭数据库连接池
    try:
        from app.db.database import engine
        if engine is not None:
            await engine.dispose()
            logger.info("[Database] Connection pool disposed")
    except Exception as e:
        logger.warning(f"[Database] Error disposing engine: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    logger.info("Starting up Stock Agent Service...")
    await init_systems()
    yield
    logger.info("Shutting down Stock Agent Service...")
    await shutdown_systems()


# 创建 FastAPI 应用
app = FastAPI(
    title=settings.app_name,
    description="智能股票分析 Agent 服务",
    version=settings.app_version,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(api_router)
app.include_router(auth_router, prefix="/api/v1")
app.include_router(config_admin_router)
app.include_router(knowledge_router)
app.include_router(tile_proxy_router)
app.include_router(tasks_router)
app.include_router(feishu_router)  # 飞书 Webhook 事件接收

# 挂载组件 bundle 静态文件目录
# 前端 `npm run build:components` 构建产物通过 Docker volume 挂载到此目录
# 供 VueComponentRenderer 生成的 HTML 页面引用
_components_static_dir = project_root / "app" / "static" / "components"
_components_static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static/components", StaticFiles(directory=str(_components_static_dir)), name="component-bundle")


@app.get("/")
async def root():
    """健康检查"""
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "status": "running"
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "healthy"}


@app.get("/health/detailed")
async def health_detailed():
    """健康检查 - 详细"""
    return {
        "status": "healthy",
        "version": "2.0.0",
        "components": {}
    }


@app.get("/api/files/download")
async def download_file(path: str):
    """
    下载生成的文件
    Args:
        path: 文件相对路径或文件名 (e.g., 'output.pptx')
    """
    from fastapi.responses import FileResponse
    from fastapi import HTTPException
    from app.services.file_generator import resolve_local_path_from_download_value

    file_path = resolve_local_path_from_download_value(path)
    if not file_path:
        raise HTTPException(status_code=403, detail="Access denied")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    # 根据文件扩展名设置正确的 MIME 类型
    # HTML 文件需要在浏览器中直接打开而非下载
    mime_map = {
        ".html": "text/html; charset=utf-8",
        ".htm": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".pdf": "application/pdf",
    }
    suffix = file_path.suffix.lower()
    media_type = mime_map.get(suffix, "application/octet-stream")

    # 非下载类型（HTML/图片等）不设置 Content-Disposition attachment
    if suffix in (".html", ".htm", ".png", ".jpg", ".jpeg", ".svg", ".pdf"):
        return FileResponse(
            path=file_path,
            media_type=media_type,
        )

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type=media_type,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug
    )
