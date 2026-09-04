# M12 每日流水线模块

> 路径：`app/pipeline/daily.py` ｜ 规模：~200 行 ｜ 依赖：全部上层模块

## 1. 职责

编排"采集→清洗→生成→去重→入库→选题"每日流程；执行 D16（36 题生成上限）与 D8（当日待做配额）；每阶段写 TaskLog。这是唯一串联所有模块的地方，也是错误隔离的最后一道边界。

## 2. 接口

```python
def run_daily(config: AppConfig, sources: list[SourceProvider], llm, embedder) -> DailyReport
    """sources/llm/embedder 注入（依赖注入，测试可替换 Fake）；embedder 供 M9 语义去重"""

DailyReport = {
  "sources": {源名: {"fetched": int, "failed": int}},
  "new_questions": int,     # 去重后新入库题数（≤36）
  "today_questions": [...], # 当日待做题
  "errors": [...],          # 各阶段错误摘要
}
```

采集源协议（`SourceProvider`）：`{name: str, collect() -> list[Source]}`——M5/M6/M7 均实现，社交源占位。

## 3. 关键决策

- **逐源、逐阶段隔离**：单源失败仅该源计数为 0；单阶段失败记录后停止后续阶段，但**已入库内容保留**；任何异常不向上传播
- **生成上限计数**（D16）：新入库**去重后**不重复问题累计 ≤36，达到即停止生成
- **待做配额**（D8）：从新题池按 knowledge 3-5 / design 1-2 / project 0-1 选当日题；不足配额取实际可选数；优先级 knowledge > design > project
- **幂等**：重复运行不产生重复题（UNIQUE + 去重 + 状态标记）
- 通知由 Web 层触发（模块返回报告，不发通知本身）

## 4. 错误隔离

- 阶段包裹：`采集(逐源 try) → 生成(逐面经 try) → 去重(降级) → 选题(纯计算) → TaskLog(写库)`
- 任何模块异常在流水线边界被捕获 → 记录到报告与 TaskLog，`run_daily` 本身不抛错
- 调度器（M13）视角：run_daily 永远正常返回
- **进程内互斥**（2026-08-08 修复）：`run_daily` 入口非阻塞 try-acquire 锁，重叠触发（手动「立即更新」连点/与定时任务撞车）返回 SKIPPED_REPORT 不跑——防并发双跑击穿 D16 上限

## 5. 测试计划（`tests/test_daily.py`）

全部用注入的 FakeSource + FakeLLM + 内存 SQLite：

| 类别 | 用例 |
|---|---|
| happy | 全源正常闭环；各计数正确；当日题目按配额选出 |
| edge | 无新源（全增量零新题）；超 36 上限截断（生成 40 → 入库 36）；配额不足取实际值；重复源幂等 |
| fail | 某源全挂其余正常（计数正确 + 报告含错误）；生成阶段挂（已入库保留、停止后续）；入库 UNIQUE 冲突幂等；run_daily 永不抛错 |

## 6. 技术选型

**纯编排代码**，无框架（不用 Airflow/Celery）

- 优：流水线是核心资产，保持薄、行为可穷举测试
- 缺：无跨阶段重试编排（单阶段重试由模块内自管；跨阶段重试靠手动触发）
- 理由：Airflow/Celery 对个人工具是过度设计；APScheduler 只负责"何时跑"，"怎么跑"由本模块负责

## 7. 实现提示

- 采集源注册表：`default_sources(config) -> list[SourceProvider]`（importer 固定；github 有仓库才注册；nowcoder 仅 `sources.nowcoder_enabled=True` 才注册，默认关闭用于生产/公开，本地个人学习可显式开启），测试注入替换
- 阶段函数独立：`_collect / _generate / _dedup / _pick_today`，各自可单测
- 报告结构稳定后即为 M13 与 Web 手动触发的返回体
