-- 0008_criteria_belong_to_a_question.sql —— 考察点可以只属于**一道题**（决策 93）
--
-- 现场（第 1 轮实测）：私有题集里一个用户只有一个锚点（`私有题集（用户 {id}）`），
-- 而判分读的是"该题主知识点下的**全部**考察点" —— 于是 8 道私有题共享 20 条考察点，
-- **每道题都被拿别人的考察点判分**（`FIX-PLAN` #16）。生成它们的那条路
-- （`offline/profile_pipeline.py`）明明是**按题**造的考察点，库里却只能挂在知识点上。
--
-- 所以给 `criteria` 加一个可空的 `question_id`：
--   · NULL  = 知识点级考察点（公共骨架那一套，判分时按主知识点 + 关联知识点取）
--   · 非空  = **这道题自己的**考察点（`criteria_of_question` 优先读它）
--
-- ⚠️ 为什么不是"每题一个锚点"（`FIX_PLAN` 给的另一条路）：那会给每个用户的每道题
-- 都造一个 `knowledge_points` 行，而它们 `status='draft'`、永不人审 —— 知识地图那张
-- 表就被"不是知识点的东西"填满了（决策 91 的 owner 列只解决了"怎么删干净"，
-- 没解决"为什么它们在那里"）。
--
-- ⚠️ 顺带补上一直没有的两条唯一约束（同一个知识点/同一道题下 seq 不许重复）：
-- `_commit_point` 原来用 `len(existing) + 1`，seq 有空洞时会**重复**（静默）。
-- 用**部分索引**而不是表内 UNIQUE：SQLite 的唯一索引里 NULL 互不相等，写成
-- `UNIQUE(point_id, question_id, seq)` 会放行知识点级的重复行。
-- 线上库实测（只读）：574 条考察点、0 组重复 `(point_id, seq)`，所以这里建索引安全。
--
-- ⚠️ 纯 SQL、无分支、不许有 BEGIN / COMMIT（ADR-0011：事务边界由 runner 持有）。

ALTER TABLE criteria ADD COLUMN question_id INTEGER REFERENCES questions(id);

CREATE INDEX IF NOT EXISTS idx_criteria_question ON criteria(question_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_criteria_question_seq
    ON criteria(question_id, seq) WHERE question_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_criteria_point_seq
    ON criteria(point_id, seq) WHERE question_id IS NULL;
