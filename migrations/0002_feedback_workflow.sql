-- 0002_feedback_workflow.sql —— 恢复反馈的"工单"形态（§未决 12 / v1 的契约）
--
-- 背景：v1 的 `question_feedback` 有三态 `status` + 一条部分唯一索引
-- `uq_feedback_open`（同一人对同一题只许一条未处理反馈），配 resolve/dismiss
-- 两个动作端点。v2 的 0001 里两样都没有，而 `docs/v1行为规格.md` 也没把它标成
-- 「继承」或「废弃」—— 属于**规格漏项**。这一笔把它补回来。
--
-- 为什么它是「遗留契约」而不是新功能：没有 status 的话，用户报的问题会沉进表里，
-- 管理员既看不到"哪些还没处理"，也无法标记"已处理" —— 反馈这条闭环只有入口没有出口。
--
-- ⚠️ 纯 SQL、无分支、不许有 BEGIN / COMMIT（ADR-0011：事务边界由 runner 持有）。
--    两条语句都是幂等声明式的：`CREATE UNIQUE INDEX IF NOT EXISTS` 是幂等的；
--    `ADD COLUMN` 只在**从 0001 升上来**时跑一次（runner 按文件记账，不会重跑）。

ALTER TABLE question_feedback ADD COLUMN status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'resolved', 'dismissed'));

-- 部分唯一索引：只约束"未处理"的那些行。
-- 为什么是部分索引而不是全表唯一：用户对同一道题**可以**在旧反馈被处理之后再报一次
-- （问题没修好、或者又发现了新问题），而同一时刻只该有一条待处理。
CREATE UNIQUE INDEX IF NOT EXISTS uq_feedback_open
    ON question_feedback(question_id, user_id)
    WHERE status = 'open';
