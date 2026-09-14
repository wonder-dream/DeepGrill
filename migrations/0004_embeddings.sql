-- 0004_embeddings.sql —— 嵌入缓存（ADR-0008 的"嵌入走 API" / 决策 44 的聚类）
--
-- 为什么要缓存：全量装配要嵌入几千条文本，而**同一道题每次跑管道都会再嵌一次**
-- （重跑是常态：心跳回退、改阈值再试、分批失败重来）。缓存让"重跑"只花钱在真的
-- 变了的东西上，也让聚类**可复现**（同一批输入 → 同一批向量）。
--
-- 为什么键是 (kind, ref_id) 而不是 question_id：
--   聚类不只需要题的向量，还需要**候选知识点**的向量（决策 44 的两阶段里，
--   第二阶段是"把候选名嵌入后粗筛"）。所以 ref_id 是文本级的主键：
--     · kind='question'  → ref_id = 题目 id
--     · kind='candidate' → ref_id = 文本哈希（候选还没有 id）
--
-- `source_hash`：被嵌入的那段文本的哈希。**它让"题干改了"自动失效** ——
--   与 `explanation_cache` 同一个手法（比"再存一列原文来比"少一处会漏的条件）。
--
-- `vector`：base64 的 float32 字节。JSON 数组存 1536 维要 ~30KB/行，
--   float32 是 6KB —— 几千行时这个差别是几十 MB（ADR-0008 的 2C2G）。
--
-- ⚠️ 它有**回收者**（AGENTS.md §3.2）：TTL + 容量上限，由离线任务
--    `purge_embeddings` 执行。题删了行也随之消失（真外键，只有 question 那一类）。
--
-- ⚠️ 纯 SQL、无分支、不许有 BEGIN / COMMIT（ADR-0011：事务边界由 runner 持有）。

CREATE TABLE embeddings (
    kind        TEXT    NOT NULL CHECK (kind IN ('question', 'candidate')),
    ref_id      TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    source_hash TEXT    NOT NULL,
    dim         INTEGER NOT NULL CHECK (dim > 0),
    vector      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (kind, ref_id)
);

-- 回收者按时间清最旧的：没有这个索引，purge 会全表扫。
CREATE INDEX IF NOT EXISTS idx_embeddings_created ON embeddings(created_at);
