-- 0003_explanation_cache.sql —— 讲解的按需生成与缓存（决策 67）
--
-- 背景：基线把「讲解 / 讲义」列为知识层的**第三类内容**，并写明它**按需生成 +
-- 缓存**（不预生成、不进审核队列）。前两类（骨架 / 知识点定义 / 参考答案）都已有
-- 归属：参考答案落在 `questions.reference_answer`（决策 12 的档位）。
--
-- 为什么不塞进 `questions.reference_answer`：那是**泛用题预生成**的参考答案，
-- 这是**长尾题按需**生成的讲解 —— 门禁、寿命、失效条件三样都不同。混在一列里
-- 会让"这列到底是谁写的、什么时候写的、改了题干还算不算数"变成没人答得出的问题。
--
-- `version` 是**复合键的一半**，由代码算出来（`app/knowledge/explanation.py`）：
--   提示词版本 + 题干哈希
-- 于是两件事自动成立：改了 prompt 旧缓存不复活（版本变了）；改了题干旧讲解不会被
-- 当成新题干的讲解（哈希变了）。比"加两列再在查询里比"少一处会漏的条件。
--
-- ⚠️ 这张表会随题目数增长，所以它有**回收者**（AGENTS.md §3.2）：TTL + 容量上限，
--    由离线任务 `purge_explanations` 执行（`app/knowledge/explanation.py` 的
--    `purge()`）。终端用户那次请求只负责写与读。
--
-- ⚠️ 纯 SQL、无分支、不许有 BEGIN / COMMIT（ADR-0011：事务边界由 runner 持有）。

CREATE TABLE explanation_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    version     TEXT    NOT NULL,
    body        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
-- question_id + version：同一道题在同一版本下只有一条讲解（并发请求不会写出两条）。
-- 用**独立索引**而不是表内 UNIQUE：迁移 runner 的 splitter 只按分号切语句，
-- 表内约束与独立索引在这里等价，而独立索引可以写成 IF NOT EXISTS（幂等声明式）。

CREATE UNIQUE INDEX IF NOT EXISTS uq_explanation_key
    ON explanation_cache(question_id, version);

-- 回收者要按时间清最旧的：没有这个索引，purge 会全表扫。
CREATE INDEX IF NOT EXISTS idx_explanation_created
    ON explanation_cache(created_at);
