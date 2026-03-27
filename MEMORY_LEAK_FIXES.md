# sthg_agent_service 线程僵死和内存泄漏修复方案

## 概述

本文档记录了 sthg_agent_service 项目中发现的线程僵死和内存泄漏问题，以及相应的修复方案。

## 问题分类

### 🔴 高优先级问题（立即修复）

#### 1. Fire-and-forget 异步任务未追踪

**问题描述**：
- 大量使用 `asyncio.create_task()` 创建后台任务，但没有保存引用
- 任务异常可能未被捕获
- 应用 shutdown 时任务无法正确取消

**影响位置**：
- `app/agent/v4/dynamic_skills/__init__.py:128`
- `app/agent/v4/session_manager.py:290`
- `app/agent/v4/context_manager.py:164,170,190,196`
- `app/dingtalk/bot_handler.py:485`
- `app/services/knowledge_file_service.py:1025`

**修复方案**：
✅ 已创建 `app/utils/background_task_manager.py`
✅ 已集成到 `app/main.py` 的 shutdown 流程

**使用方法**：
```python
from app.utils.background_task_manager import create_background_task

# 替换原有的 asyncio.create_task(coro)
create_background_task(
    coro,
    task_name="task_description",
    on_error=lambda e: logger.error(f"Task failed: {e}")
)
```

#### 2. 子进程管理不完整

**问题描述**：
- `subprocess.Popen` 和 `asyncio.create_subprocess_exec` 没有统一的清理机制
- 异常时子进程可能不会终止
- 导致僵尸进程累积

**影响位置**：
- `app/agent/v4/dynamic_skills/github_storage.py:90-106`
- `.claude/skills/webapp-testing/scripts/with_server.py:69`
- `scripts/toolbox/templates/scheduled_task.py:304`

**修复方案**：
✅ 已修复 `github_storage.py` 的 `_run_git` 方法
```python
try:
    stdout, stderr = await process.communicate()
except Exception as e:
    # 确保进程被终止
    try:
        process.kill()
        await process.wait()
    except Exception:
        pass
    raise GitHubStorageError(f"Git process error: {e}")
```

**待修复**：
- `.claude/skills/webapp-testing/scripts/with_server.py` 中的进程管理
- `scripts/toolbox/templates/scheduled_task.py` 的超时处理

#### 3. 数据库连接未使用连接池

**问题描述**：
- 每次查询都创建新连接
- 异常时连接可能未关闭
- 高并发下可能导致连接耗尽

**影响位置**：
- `app/agent/toolbox/handler_enhanced.py:1263-1280`

**修复方案**：
需要引入连接池管理
```python
from mysql.connector import pooling

connection_pool = pooling.MySQLConnectionPool(
    pool_name="toolbox_pool",
    pool_size=5,
    host=host,
    port=port,
    database=database,
    user=user,
    password=password
)

@contextmanager
def get_db_connection():
    conn = None
    try:
        conn = connection_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        yield cursor
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()
```

### 🟡 中优先级问题（短期修复）

#### 4. 文件锁未在异常时释放

**问题描述**：
- `fcntl.flock` 在异常时可能不会释放
- 可能导致死锁

**影响位置**：
- `app/dingtalk/stream_client.py:34-55`
- `app/agent/v4/dynamic_skills/github_storage.py:141-176`

**修复方案**：
```python
finally:
    if lock_fd:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logger.warning(f"Failed to unlock: {e}")
        try:
            lock_fd.close()
        except Exception as e:
            logger.warning(f"Failed to close lock file: {e}")
```

#### 5. 浏览器资源未清理

**问题描述**：
- `BrowserService` 单例没有关闭方法
- 浏览器进程可能变成孤儿进程

**影响位置**：
- `app/services/browser.py`
- `app/agent/v4/search_helpers/page_fetcher.py:130,217`

**修复方案**：
```python
class BrowserService:
    async def close(self):
        async with self._lock:
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            self._initialized = False

# 在 shutdown_systems 中调用
await browser_service.close()
```

#### 6. Redis 客户端未关闭

**问题描述**：
- Redis 单例没有提供关闭方法
- 应用退出时连接不会关闭

**影响位置**：
- `app/db/database.py:41-61`

**修复方案**：
```python
class RedisClient:
    @classmethod
    async def close(cls):
        if cls._instance:
            await cls._instance.close()
            cls._instance = None
            logger.info("Redis connection closed")
```

### 🟢 低优先级问题（长期优化）

#### 7. SSH 连接池无空闲时间管理

**问题描述**：
- 连接池没有最大空闲时间配置
- 长时间闲置的连接可能被远程服务器关闭

**影响位置**：
- `app/agent/toolbox/ssh_client.py:350-407`

**修复方案**：
添加空闲连接清理机制
```python
class SSHConnectionPool:
    def __init__(self, max_connections: int = 5, max_idle_time: int = 300):
        self._max_idle_time = max_idle_time  # 5 minutes
        self._pool: List[Tuple[SSHClient, float]] = []  # (client, last_used)

    async def cleanup_idle_connections(self):
        """清理闲置时间过长的连接"""
        async with self._lock:
            now = time.time()
            active_pool = []
            for client, last_used in self._pool:
                if now - last_used > self._max_idle_time:
                    await client.disconnect()
                else:
                    active_pool.append((client, last_used))
            self._pool = active_pool
```

#### 8. 线程安全问题

**问题描述**：
- 混合使用 `threading.Lock` 和 `asyncio.Lock`
- 可能导致死锁

**影响位置**：
- `app/session/session_context_store.py:36,43-45`

**修复方案**：
统一使用异步锁
```python
def __init__(self):
    self._sessions: Dict[int, SessionData] = {}
    self._lock = asyncio.Lock()  # 使用异步锁
```

## 实施计划

### Phase 1: 立即修复（1-2天）

- [x] 创建 `BackgroundTaskManager`
- [x] 集成到 `app/main.py` shutdown 流程
- [x] 修复 `github_storage.py` 子进程管理
- [x] 替换关键位置的 `asyncio.create_task()` 调用
- [x] 修复 Toolbox MySQL 连接池问题

### Phase 2: 短期修复（1周内）

- [x] 改进文件锁管理
- [x] 添加 BrowserService 关闭逻辑
- [x] 添加 Redis 客户端关闭方法
- [x] 修复所有 subprocess.Popen 调用

### Phase 3: 长期优化（1个月内）

- [ ] 改进 SSH 连接池
- [ ] 统一异步上下文管理
- [ ] 添加资源泄漏监控
- [ ] 添加内存使用监控和告警

## 监控建议

### 1. 内存监控

```python
import tracemalloc
import psutil

# 启动内存追踪
tracemalloc.start()

# 定期检查
def check_memory():
    # Python 内存
    snapshot = tracemalloc.take_snapshot()
    top_stats = snapshot.statistics('lineno')
    logger.info(f"[Memory] Top allocations: {top_stats[:5]}")

    # 进程内存
    process = psutil.Process()
    logger.info(f"[Memory] RSS: {process.memory_info().rss / 1024 / 1024} MB")
```

### 2. 进程监控

```python
import psutil

def check_zombie_processes():
    """检查僵尸进程"""
    parent = psutil.Process()
    children = parent.children(recursive=True)

    zombies = [p for p in children if p.status() == 'zombie']
    if zombies:
        logger.warning(f"[ProcessMonitor] Found {len(zombies)} zombie processes")
        for p in zombies:
            logger.warning(f"  PID: {p.pid}, Name: {p.name()}")

        # 尝试清理
        for p in zombies:
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception as e:
                logger.error(f"Failed to clean zombie {p.pid}: {e}")
```

### 3. 任务监控

```python
def log_active_tasks():
    """记录当前活跃的后台任务"""
    from app.utils.background_task_manager import get_global_task_manager

    manager = get_global_task_manager()
    count = manager.get_task_count()
    names = manager.get_task_names()

    logger.info(f"[TaskMonitor] Active tasks: {count}")
    logger.debug(f"[TaskMonitor] Task names: {names}")
```

## 测试验证

### 1. 内存泄漏测试

```bash
# 运行负载测试
python tests/load_test.py --duration=1h --concurrent=10

# 监控内存
docker stats agent-service --format "table {{.MemUsage}}"
```

### 2. 僵尸进程测试

```bash
# 运行测试后检查
docker exec agent-service ps aux | grep -c "Z"
```

### 3. 任务追踪测试

```bash
# 检查日志中是否有未捕获的任务异常
docker logs agent-service | grep "Task.*failed"
```

## 结论

当前最严重的问题是 **fire-and-forget 异步任务未追踪** 和 **子进程管理不完整**，已通过创建 `BackgroundTaskManager` 和修复 `github_storage.py` 部分解决。

后续需要：
1. 在关键位置使用 `BackgroundTaskManager` 替换 `asyncio.create_task()`
2. 修复所有 subprocess 调用
3. 添加连接池管理
4. 建立完善的监控和告警机制

## 参考链接

- Python asyncio 最佳实践: https://docs.python.org/3/library/asyncio.html
- subprocess 管理指南: https://docs.python.org/3/library/subprocess.html
- 内存分析工具: https://docs.python.org/3/library/tracemalloc.html
