# M2 数据模型与存储模块

> 路径：`app/models.py` + `app/db.py` ｜ 规模：~180 行 ｜ 依赖：SQLModel
> 更新日期：2026-08-07（实现偏差修复）

## 1. 职责

定义 6 张表的 SQLModel 模型（字段按 docs/DESIGN.md 第 4 章），提供 SQLite 连接/会话/建表与通用 CRUD 辅助。

## 2. 接口

```python
def init_db(db_url: str) -> None          # 建表（create_all）
def get_session() -> Generator[Session]   # 依赖注入用（FastAPI 与流水线共用）
```

### 模型定义

| 表 | 关键字段 | 约束/说明 |
|---|---|---|
| `Source` | id, type, url, title, raw_text, cleaned_text, fetched_at, source_hash | `type ∈ nowcoder/github/manual/social/resume`；`source_hash UNIQUE` |
| `Question` | id, source_id, type, stem, tags, difficulty, good_criteria, bad_criteria, status, created_at | `type ∈ knowledge/design/project`；`status ∈ pending/today/done/skipped`；criteria 为 JSON |
| `Session` | id, question_id, kind, status, started_at, ended_at | `kind ∈ open/design/chain`；`status ∈ active/finished` |
| `Attempt` | id, session_id, round_no, is_followup, answer_text, feedback_text | `round_no` 从 0 起，第 0 轮为原始作答 |
| `Judgment` | id, session_id, scores, total_score, review, reference_answer, weak_tags, model, created_at | scores 为 JSON（四维），可含 status 标记 |
| `TaskLog` | id, task_name, status, fetched_count, generated_count, error, ran_at | 任务日志 |

### 通用 CRUD 辅助（收敛在 db.py，避免各模块重复写 SQL；均以 session 为第一参数）

- `list_today_questions(session)`：按 status=today 取当日题
- `pick_questions(session, limit, qtype=None)`：从 pending 池取题并标记 today；qtype 供 D8 按题型配额选题
- `latest_task_log(session, task_name)`
- `count_questions_created_since(session, date)`：供 D16 上限计数
- `commit(session)`：统一提交入口，UNIQUE 冲突 → `DuplicateSource`，其余 sqlite 异常 → `StorageError`

## 3. 关键决策

- 枚举字段用 SQLModel str-Enum（注解）+ DB 级 `CheckConstraint`：SQLModel 0.0.39 构造时不校验枚举值、且 `sa_column` 的 Enum 类型会被注解覆盖，故用 CHECK 约束实现"非法值入库即抛 `StorageError`"（ORM 与裸 SQL 均拦截）
- **重复导入幂等**：`Source.source_hash` UNIQUE 冲突捕获为 `DuplicateSource` 标记（定义在 `app/errors.py`，携带 source_hash 值，从 IntegrityError 的 SQL 列序与参数提取），由调用方决定跳过而非崩溃
- 级联删除：删 Session 级联删其 Attempt/Judgment（`Relationship(sa_relationship_kwargs={"cascade": "all, delete-orphan"})`）
- 通用查询辅助集中在 db.py，保证流水线与 Web 层行为一致

## 4. 错误隔离

- 所有经本层 API（init_db/commit/辅助函数）的 sqlite 异常包装为 `StorageError`；UNIQUE 冲突特殊化为 `DuplicateSource`
- 裸 `session.execute` 不在包装范围内（其异常直接上抛 sqlalchemy 类型）；应用代码统一经 commit/辅助函数入库
- 存储层不抛业务异常（业务语义由调用方处理）

## 5. 测试计划（`tests/test_models.py`）

| 类别 | 用例 |
|---|---|
| happy | 增删查改、级联删除（session→attempts/judgments）、JSON 字段往返、枚举值入库 |
| edge | 空表查询、批量写入、长文本字段（1MB round-trip，SQLite 不强制 VARCHAR 长度）、多个 active 会话共存 |
| fail | UNIQUE 冲突 → DuplicateSource（校验携带的 hash 值）、非法枚举值（ORM 与裸 SQL 两条路径均被 CHECK 拒绝）、字段超长（SQLite 上无意义，见 edge）、数据库文件只读 |

## 6. 技术选型

**SQLModel**（SQLAlchemy + Pydantic 之上）

- 优：pydantic 集成（API 层直接复用模型）、类型提示、建表简单、简历项目观感好
- 缺：抽象层较厚，复杂查询调试不如原生 SQL 直观
- 理由：个人工具规模下 SQLAlchemy 能力富余，但类型安全与开发速度收益明确；原生 sqlite3 手写映射代码反而更多

## 7. 实现提示

- 测试统一用内存 SQLite（`sqlite:///:memory:` 或 tmp 文件），不污染开发库
- `DuplicateSource` 是 `StorageError` 子类，携带 source_hash 字段（`app/errors.py`）
- JSON 字段用 `sa_column=Column(JSON)`，读写自动序列化
- SQLModel 0.0.39 注意：`session.exec()` 返回 Row 元组而非模型实例，统一用 `session.scalars()`；模型类 `Session` 与 `sqlmodel.Session` 撞名，db.py 内用别名 `DBSession`；SQLite 引擎需 `check_same_thread=False` 供 FastAPI 线程池复用
