-- 0001_initial.sql —— DeepGrill v2 的完整 schema
--
-- 这是 v2 的表结构权威。`docs/v2数据模型.md` 记的是"为什么这么设计"与与 v1 的对照，
-- 字段级事实以本文件为准（那份文档自己声明「实现后由代码与迁移脚本替代」）。
--
-- 执行：python -m migrations.run
-- 语义：见 docs/adr/0011-migrations-are-plain-sql.md —— **纯 SQL，没有分支**。
--      本文件里不许出现 BEGIN / COMMIT（事务边界由 runner 持有）。
--
-- ⚠️ owner 账号的初始密码哈希是占位值（占位 owner 那一段），
--    **首次登录前必须替换** —— 否则任何人都能用那个口令拿到 owner 权限。

-- ---------------------------------------------------------------------------
-- 记账表
--
-- ⚠️ 它**不靠这个文件创建** —— runner 开头会用 `CREATE TABLE IF NOT EXISTS`
--    自己保证它存在（ADR-0011）。这一段的作用是让 schema 自文档：读这一个
--    文件就能看到全部表。
--    两处都幂等，所以谁先谁后都成立；原先"靠 0001 建它"的写法在 runner 与
--    迁移文件之间留了一条隐式契约，实现时当场崩了。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 1. 账号侧
-- ---------------------------------------------------------------------------
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT    NOT NULL UNIQUE,
    username      TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'user',
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
-- 没有 deleted_at：注销走硬删（决策 21）—— 删身份与原文，只把判定数据聚合进
-- question_point_stats。软删除字段因此不需要。

CREATE TABLE invite_codes (
    code       TEXT PRIMARY KEY,
    created_by INTEGER REFERENCES users(id),
    expires_at TEXT,
    used_by    INTEGER REFERENCES users(id),
    used_at    TEXT
);
-- 取代 v1 的 MAX_USERS 全局计数 + email_code 整套：名额由"发了多少码"决定。
-- 作废一张码 = 删除该行。空 used_by 即未使用。

CREATE TABLE user_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE quota_ledger (
    user_id     INTEGER NOT NULL REFERENCES users(id),
    kind        TEXT    NOT NULL DEFAULT 'day',
    day         TEXT    NOT NULL,
    units_used  INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, kind, day)
);
-- 每日一行，不做全量流水：用户只需要知道"今天还剩多少"，你需要知道"钱花在哪了"。
-- kind: day / month —— 月度归档行与每日行**同表但不同粒度**（决策 23）。
--   第一版只靠"用一个特殊 day 值，如 '2026-09'"来区分，于是同一列里混两种格式、
--   而"这个月已用多少"只能靠字符串形状猜。加一列把它变成显式事实（表是空的）。

-- ---------------------------------------------------------------------------
-- 2. 知识侧（ADR-0002 的图）
-- ---------------------------------------------------------------------------
CREATE TABLE domains (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES domains(id),
    name      TEXT    NOT NULL
);
-- 森林而不是一棵树（决策 52）：顶层领域 parent_id IS NULL，没有虚构的"全部知识"根。
-- 知识点可被多条路径共享，所以它准确说是 DAG ——「树」在本项目一律按有向无环图理解。
-- 没有 kind 字段：岗位不在这张表里（决策 51）。
-- 层级走 parent_id，依赖走 knowledge_point_edges，两种关系分开存。

CREATE TABLE roles (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT    NOT NULL UNIQUE
);

CREATE TABLE role_points (
    role_id  INTEGER NOT NULL REFERENCES roles(id),
    point_id INTEGER NOT NULL REFERENCES knowledge_points(id),
    PRIMARY KEY (role_id, point_id)
);
-- 岗位是独立于知识结构的另一条轴（决策 51）。它同时补上了 v1 的一个真空：
-- 此前数据模型里没有任何"岗位 ↔ 知识点"的关联，"按岗位抽题"无从实现。

CREATE TABLE knowledge_points (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id      INTEGER NOT NULL REFERENCES domains(id),
    name           TEXT    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'draft',
    origin         TEXT    NOT NULL DEFAULT 'proposed',
    exclusions     TEXT,
    question_count INTEGER NOT NULL DEFAULT 0
);
-- domain_id: 属于哪个知识领域 —— **「知识领域 → 知识点」这层关系在库里的唯一表达**。
--   第一版漏了这一列（手抄 24 张表时漏掉的一行），后果是 domains 没有任何表指向它，
--   三层结构在库里只剩两层，查不出"某领域下有哪些知识点"。
--   它是 tools/_compare_schema.py 抓出来的 —— 那份对照脚本因此留在仓库里。
-- status: draft / confirmed —— **人审过才是 confirmed**
-- origin: proposed（LLM 提议）/ manual（人写）
-- exclusions: 不考察什么（防挂载漂移）
-- question_count: 用于审核分层（≥20 / 10-19 / 5-9 / 3-4 / ≤2）
-- 一条知识点下的考察点通常 2-5 条；写不出"什么算答到、什么算没答到"就是切错了。

CREATE TABLE criteria (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id INTEGER NOT NULL REFERENCES knowledge_points(id),
    seq      INTEGER NOT NULL,
    [text]   TEXT    NOT NULL,
    shared   INTEGER NOT NULL DEFAULT 0
);
-- 考察点独立成表，不塞进 knowledge_points 的 JSON 字段：判断两条考察点是否
-- "不该复制"需要稳定 id 跨知识点比较。它同时是 attempts.hits 与
-- question_point_stats 的引用对象，也是追问的路线（第 N 条未命中，下一问朝它去）。
-- ⚠️ attempts.hits 不在 evaluations 上 —— 决策 28 把它移到了 attempts（累积快照）。
-- shared: 通用表达类考察点（"先说结论再说理由"这类的 1 与 0），允许被多个知识点
--   合法引用。ADR-0002 判据② 靠它区分两种情形 —— 一条考察点被复制到多个知识点下
--   是"这两个知识点该合并"，而一条共用考察点出现在多处是"通用表达要求，不该合并"。
--   没有这一列，判据② 只能产出误报（第一版就漏了它）。

CREATE TABLE knowledge_point_edges (
    from_point_id INTEGER NOT NULL REFERENCES knowledge_points(id),
    to_point_id   INTEGER NOT NULL REFERENCES knowledge_points(id),
    kind          TEXT    NOT NULL,
    PRIMARY KEY (from_point_id, to_point_id, kind)
);
-- kind: prerequisite —— 定义在知识领域内，但允许跨领域的边（依赖是知识本身的性质）。

-- ---------------------------------------------------------------------------
-- 3. 题库侧
-- ---------------------------------------------------------------------------
CREATE TABLE questions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT    NOT NULL,
    stem              TEXT    NOT NULL,
    difficulty        INTEGER NOT NULL,
    primary_point_id  INTEGER REFERENCES knowledge_points(id),
    good_criteria     TEXT,
    bad_criteria      TEXT,
    answer_tier       TEXT,
    reference_answer  TEXT,
    origin            TEXT    NOT NULL DEFAULT 'generated',
    owner_user_id     INTEGER REFERENCES users(id),
    visibility        TEXT    NOT NULL DEFAULT 'public',
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);
-- kind: knowledge / design（决策 20）；项目深挖题只存在于私有题集
-- difficulty: 1-5（继承 v1 的定义与标定）
-- primary_point_id: 必填由业务层保证（SQLite 加列时为兼容 v1 导入留了空），
--   它只管挂载与自修复，**不参与掌握度计算**（决策 24）
-- good_criteria / bad_criteria: **离线素材**，不是产品数据 —— 供知识层构建管道
--   当聚类素材。硬规则：运行时逻辑（判分/追问/报告/掌握度）只准读 criteria 表
-- answer_tier: 泛用档（预生成参考答案）/ 长尾档（只有评分标准）（决策 12）
-- owner_user_id: 非空 = 私有题集的题；空 = 公共题库。
--   ⚠️ 本表同时装公共题库与所有人的私有题集，**任何查询都必须过滤 owner_user_id**，
--   且必须经过 bank/repository.py 的统一入口（AGENTS.md §3.5）。漏一次就是数据泄露。
-- visibility: public / private / pending（晋升待门禁）/ hidden（质量信号差）

CREATE TABLE question_points (
    question_id INTEGER NOT NULL REFERENCES questions(id),
    point_id    INTEGER NOT NULL REFERENCES knowledge_points(id),
    source      TEXT    NOT NULL DEFAULT 'authored',
    PRIMARY KEY (question_id, point_id)
);
-- 一道综合题还牵动哪些知识点（决策 24）。用于按知识点组卷与覆盖检查，
-- **不用于掌握度归属** —— 那走 hits。缺了它，综合题牵动的知识点会被长期低估。

CREATE TABLE question_flags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    kind        TEXT    NOT NULL,
    detail      TEXT,
    status      TEXT    NOT NULL DEFAULT 'open'
);
-- kind: conflict（与同知识点其他题口径不一致）/ suspect_mount（长期答不到本知识点的
--   考察点）/ suggest_move
-- 这张表就是"题库哪里不干净"的仪表板，让挂载错误**可观测**（ADR-0002 的自修复落地处）。

-- ---------------------------------------------------------------------------
-- 4. 私有侧（决策 8 / 决策 9）
-- ---------------------------------------------------------------------------
CREATE TABLE candidate_profiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    structured  TEXT    NOT NULL,
    source_note TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
-- 简历解析产物：structured 是 JSON（项目 / 职责 / 技术栈 / 量化结果）。
-- **不保存简历原文**。私有题集不是一张新表 —— 就是 questions 里 owner_user_id 非空的行。

-- ---------------------------------------------------------------------------
-- 5. 作答侧（决策 19：两层会话结构）
-- ---------------------------------------------------------------------------
CREATE TABLE interviews (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    mode           TEXT    NOT NULL,
    plan           TEXT,
    status         TEXT    NOT NULL DEFAULT 'active',
    quota_charged  INTEGER NOT NULL DEFAULT 0,
    report_body    TEXT,
    report_summary TEXT,
    started_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    ended_at       TEXT
);
-- mode: interview（完整面试）/ drill（单题追问）/ browse（题库刷题）
-- plan: 编排配置 + 调整日志（初始题单 / 每题追问轮数上限 / 是否要报告，
--   以及面试过程中编排层的每次跳过与插入）。调整必须留痕，否则报告无法解释
--   "为什么这场只问了 5 道而计划是 6 道"。
-- quota_charged: 创建时扣的点数（0 / 1 / 6）
-- report_body / report_summary: finish 时算好落库，**不现生成** ——
--   否则每次打开措辞都会变，与"报告不重新生成"矛盾（ADR-0003）。

CREATE TABLE report_items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id       INTEGER NOT NULL REFERENCES interviews(id),
    session_id         INTEGER REFERENCES sessions(id),
    seq                INTEGER NOT NULL,
    snap_stem          TEXT,
    snap_question_kind TEXT,
    snap_difficulty    INTEGER,
    snap_point_name    TEXT,
    snap_criteria      TEXT
);
-- 面试报告每道题一行，**含判分依据的快照**（决策 24 风险④ / ADR-0003）。
-- 快照的是「依据」—— 题干、考察点、criteria、知识点名：它们会被编辑，
--   而历史报告必须显示**当时**的样子。
-- 不快照的是「结论」—— 分数、评语、hits 一律读 evaluations：它们是历史事实。
-- 本表没有"每题一段总结"字段（决策 27 删除）：逐题评语就是 evaluations.review。

CREATE TABLE sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL REFERENCES interviews(id),
    question_id INTEGER NOT NULL REFERENCES questions(id),
    seq         INTEGER NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'active',
    max_rounds  INTEGER NOT NULL,
    UNIQUE (interview_id, seq)
);
-- 第二层（题会话）。max_rounds 是**编排参数，不再由难度推导**（ADR-0001）——
-- 否决权在代码手里，到顶时强制收尾。

CREATE TABLE attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES sessions(id),
    round_no      INTEGER NOT NULL,
    is_followup   INTEGER NOT NULL DEFAULT 0,
    input_mode    TEXT    NOT NULL DEFAULT 'text',
    stt_text      TEXT,
    answer_text   TEXT,
    feedback_text TEXT,
    hits          TEXT,
    UNIQUE (session_id, round_no)
);
-- input_mode: voice / text（ADR-0009）—— 面试页默认语音、可随时切回打字
-- stt_text: 语音识别的**原始输出**（含口语停顿与措辞），仅 voice 时有值。
--   与 answer_text 分开存是为了让「表达清晰度」有原始素材 —— 转写直接进判分、
--   不设确认环节，两列在多数轮次里内容相同，用户顺手改过才不同。
--   ⚠️ 不保存原始录音。
-- answer_text 设长度上限（由业务层保证）
-- hits: 本轮对当前题**全部考察点的命中状态**（JSON: criterion_id → 命中状态），
--   是**累积快照**而非增量（决策 28）。它逐轮都要产出 —— 追问本身靠它决定下一问。
--   它必须能表达"题目问了但用户完全没答"（记为未命中），而不是只在答到时才有记录：
--   "考了但没答"与"没考过"对候选人是两件事（0% 与空格）。
--   ⚠️ 命中状态是**三值**：命中 / 未命中 / 未涉及（CONTEXT.md「命中状态」）。
--   "未涉及"不是可选装饰 —— 掌握度矩阵的分母是"被考过的考察点"，靠它把
--   本轮没问到的考察点从分母里排掉。

CREATE TABLE evaluations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    scores      TEXT,
    total_score REAL,
    review      TEXT,
    status      TEXT    NOT NULL DEFAULT 'ok'
);
-- 取代 v1 的 judgments。**题会话级的最终评分**（不是逐轮评分）。
-- scores: accuracy / completeness / clarity / depth 四维 JSON，
--   total_score 按 .3/.3/.2/.2 合成。
--   ⚠️ clarity 是四维里唯一含义随输入模态变化的维度（ADR-0009）：打字测组织与排版，
--   语音测表达流畅。判分 prompt 必须按 attempts.input_mode 给不同提示。
-- status: ok / failed —— **失败也要落库，不静默**（继承 v1 的 4.4）。
-- hits 不在本表（决策 28 移出）：它随轮次累积在 attempts 上。

-- ---------------------------------------------------------------------------
-- 6. 治理侧
-- ---------------------------------------------------------------------------
CREATE TABLE user_favorites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    question_id INTEGER NOT NULL REFERENCES questions(id),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, question_id)
);
-- 收藏夹（决策 63）：候选人在题库里收藏题目，是首页的一个入口。
-- UNIQUE (user_id, question_id) 让"收藏"这个动作**天然幂等**（重复点不会出两行），
--   与 v1 的实现一致（docs/v1现状-20260913.md 的第 12 张表）。
-- ⚠️ 题被隐藏 / 删除后这些行会变成悬空引用：查询必须 join 到 questions 并按
--   可见性过滤（AGENTS.md §3.5 的同一条纪律），否则收藏夹会露出不该看的题。
-- 收藏**不消耗额度点** —— 它只是存一个指针，没有任何 LLM 调用。

CREATE TABLE question_feedback (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id           INTEGER NOT NULL REFERENCES questions(id),
    user_id               INTEGER REFERENCES users(id),
    kind                  TEXT    NOT NULL,
    detail                TEXT,
    duplicate_question_ids TEXT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now'))
);
-- 沿用 v1 五类：wrong / unclear / duplicate / not_interview / other。
-- duplicate 改为指向**知识点**（"这题和我见过的那个知识点重复"）。

CREATE TABLE task_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,
    payload    TEXT,
    result     TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
-- 沿用（离线任务报告：生成 / 质检 / 挂载 / 编译）

CREATE TABLE jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,
    payload      TEXT,
    status       TEXT    NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    worker_id    TEXT,
    heartbeat_at TEXT,
    progress     REAL,
    message      TEXT,
    error        TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    started_at   TEXT,
    finished_at  TEXT
);
-- 离线任务的持久化队列（ADR-0006）。投递与业务写入同一事务 —— 这正是选它而不选
-- Redis 队列的核心理由。
-- ⚠️ 必须有回收者：已完成的记录按 TTL 清理（AGENTS.md §3.2 在库表上的对应）。
-- ⚠️ 任务函数必须幂等：超时回退与手动重跑都会导致同一任务跑两遍。
-- worker_id / heartbeat_at: 原子认领与心跳；running 且心跳超时 → 放回 pending 重跑。

CREATE TABLE question_point_stats (
    question_id  INTEGER NOT NULL REFERENCES questions(id),
    point_id     INTEGER NOT NULL REFERENCES knowledge_points(id),
    criterion_id INTEGER NOT NULL REFERENCES criteria(id),
    hit_count    INTEGER NOT NULL DEFAULT 0,
    miss_count   INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (question_id, point_id, criterion_id)
);
-- 聚合质量信号。无 user_id、无会话 id，只有计数。服务三件事：挂载自修复的信号、
-- 难度标定、题库质量分析。
-- ⚠️ 键是 criterion_id，**不是位置**（第一版写作 criterion_index，是个缺陷）：
--   attempts.hits 是 criterion_id → 命中状态 的映射，聚合表若按位置索引，
--   两处无法互相映射 —— 注销时的「累加进本表」那一步就没有可用的对照键；
--   而且考察点被删或重排之后，位置键会**静默指向另一条考察点**（决策 28）。
-- 注销用户时：先把该用户的 attempts.hits 累加进本表，再删掉逐条记录。
-- ⚠️ **不要称它为"匿名数据"**：当前规模（20 人以内）下，用剩余计数做减法即可
--   反推出被删除用户答了什么。它在用户数约 50 以上后自然失效。在此之前，
--   注销流程的合规依据是「用户要求删除」，**不是**「数据已匿名化」。

-- ---------------------------------------------------------------------------
-- 7. 索引
-- ---------------------------------------------------------------------------
CREATE INDEX idx_questions_visibility   ON questions(visibility);
CREATE INDEX idx_questions_owner        ON questions(owner_user_id);
CREATE INDEX idx_questions_primary_pt   ON questions(primary_point_id);
CREATE INDEX idx_kp_domain              ON knowledge_points(domain_id);
CREATE INDEX idx_attempts_session       ON attempts(session_id);
CREATE INDEX idx_sessions_interview     ON sessions(interview_id);
CREATE INDEX idx_interviews_user        ON interviews(user_id, status);
CREATE INDEX idx_report_items_interview ON report_items(interview_id);
CREATE INDEX idx_jobs_claim             ON jobs(status, heartbeat_at);
CREATE INDEX idx_qp_edges_from          ON knowledge_point_edges(from_point_id);
-- 反查：按知识点取题（组卷 / 覆盖检查）。没有它，question_points 只能按题查。
CREATE INDEX idx_question_points_point  ON question_points(point_id);
CREATE INDEX idx_criteria_point         ON criteria(point_id);
-- 挂载自修复的信号是"这个知识点的这条考察点大家普遍答不到" —— 入口是 point_id。
CREATE INDEX idx_qp_stats_point         ON question_point_stats(point_id);
-- 收藏的 UNIQUE(user_id, question_id) 已覆盖"按用户列收藏"；
-- 这条覆盖反向：题被删 / 被隐藏时要能查出谁收藏了它。
CREATE INDEX idx_favorites_question     ON user_favorites(question_id);

-- ---------------------------------------------------------------------------
-- 8. 占位 owner 账号（满足 v1 导入与首启的需要）
--
-- ⚠️ 口令哈希是**占位值**，首次登录前必须替换。这不是提醒，是一处安全缺口：
--    在替换之前，知道这个占位口令的人可以拿到 owner 权限。
-- ---------------------------------------------------------------------------
INSERT INTO users (email, username, password_hash, role)
VALUES ('owner@local', 'owner', 'PLACEHOLDER__REPLACE_BEFORE_FIRST_LOGIN', 'owner');
