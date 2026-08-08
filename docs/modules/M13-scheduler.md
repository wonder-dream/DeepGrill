# M13 调度模块

> 路径：`app/scheduler.py` ｜ 规模：~80 行 ｜ 依赖：APScheduler
> 更新日期：2026-08-07（实现偏差记录）

## 1. 职责

APScheduler CronTrigger 每日定时触发 M12 流水线；提供启动/关闭/手动触发；与 FastAPI 同进程生命周期。

## 2. 接口

```python
def start_scheduler(config: AppConfig, job: Callable[[], None]) -> None
    """注册每日任务并启动（内存 jobstore）"""

def shutdown() -> None
def trigger_now() -> None
    """手动触发（Web "立即更新"按钮调用）"""

def parse_cron(expr: str) -> CronTrigger
    """解析 cron 表达式，非法抛 ConfigError（供测试直接验证）"""
```

## 3. 关键决策

- **单次运行异常不外泄**：M12 内部消化所有异常，调度器不因任务失败停止
- **任务互斥**：运行时内存锁，防止长任务跨过调度点重叠执行（重叠触发被锁拒绝）
- **手动触发**：与定时触发走同一路径，可复用互斥锁
- 时区用本地时间（`CronTrigger(hour=8)` 默认本地）

## 4. 错误隔离

- 非法 cron 表达式配置：`parse_cron` 抛 `ConfigError`，启动阶段 fail fast
- job 执行异常：由 M12 捕获；调度器自身异常记录日志但不终止
- 互斥锁拒绝重叠：不阻塞调用方，直接返回"运行中"

## 5. 测试计划（`tests/test_scheduler.py`）

| 类别 | 用例 |
|---|---|
| happy | 注册后按 cron 触发（注入假 job，时间推进断言调用次数）；手动触发 |
| edge | cron 表达式边界（00:00/23:59）；本地时区解析 |
| fail | 非法 cron 表达式抛 ConfigError；job 抛异常后调度器仍存活（后续触发正常）；重叠触发被锁拒绝 |

## 6. 技术选型

**APScheduler**

- 优：进程内、零系统配置、CronTrigger 表达式灵活、与 FastAPI 同进程生命周期
- 缺：多进程/多实例部署需额外分布式锁（个人单进程无影响）
- 理由：对比 Windows 计划任务（不可控、不便调试、难随服务启停）与 Celery（过重）

## 7. 实现提示

- 用 `BackgroundScheduler` + `MemoryJobStore`；`start()` 幂等（重复调用不重复注册）
- 互斥锁用 `threading.Lock`（非阻塞 try-acquire）
- 测试：APScheduler 有 `pytest` 支持（`scheduler.pause/start`），假 job 用可计数 callable
- 手动触发在 FastAPI startup/shutdown 事件中启停

## 8. 实现偏差记录

- `start_scheduler(config, job, *, trigger=None)`：新增可选 trigger 注入参数，供测试用 `IntervalTrigger` 替代真实 CronTrigger（真实定时触发测试需时间推进库，属过度设计）；默认仍用 `parse_cron(config.daily.schedule)`
- `trigger_now()` 为**同步**执行（Web 端用 FastAPI BackgroundTasks 包装，避免请求挂起）
- 测试覆盖：parse_cron 边界（00:00/23:59/非法）、手动触发、IntervalTrigger 真实触发、幂等注册、重叠拒绝、job 异常不杀调度器
