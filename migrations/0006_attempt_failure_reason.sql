-- 0006_attempt_failure_reason.sql —— 判分失败的原因要能查（AGENTS.md §3.1）
--
-- 现场（第 1 轮实测）：一轮判定失败时只有一句 `logger.warning`，而库里那一行与
-- "模型正常、候选人一条考察点都没答到"**长得完全一样** —— `hits` 全是"未涉及"、
-- `feedback_text` 是那句固定提示。§3.1 要"① 用户可见的状态 ② 可查询的失败记录"
-- 两者：① 有了（页面上会提示、SSE 帧里带 `llm_failed`），② 只有 stdout。
--
-- 于是事后想回答"这台机器今天有几次模型调用是失败的、失败在哪一类"时，
-- 只能翻日志。加一列就把这个问题变成一次 SQL。
--
-- 为什么不是新表：失败**属于这一轮**（一行 attempts 就是一轮），另起一张表要
-- join 才能回答"这一轮为什么没判出来"，而它没有任何自己的生命周期。
--
-- ⚠️ 纯 SQL、无分支、不许有 BEGIN / COMMIT（ADR-0011：事务边界由 runner 持有）。
-- 加列是幂等安全的（ALTER TABLE ADD COLUMN 在 SQLite 里不会重建表）。

ALTER TABLE attempts ADD COLUMN llm_error TEXT;
