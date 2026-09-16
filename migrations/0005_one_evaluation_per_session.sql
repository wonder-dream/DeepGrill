-- 0005_one_evaluation_per_session.sql —— 一个题会话只有一条最终评分（决策 89）
--
-- 现场证据：`finish_interview()` 是**幂等**的（重复调用重算并覆盖 `interviews` 上的
-- 报告），但题会话级的 `evaluations` 不是 —— 它每次收尾都 `INSERT` 一行。于是
-- "收尾两次"（旧标签页重放一轮之后页面再收尾一次 / 并发两个收尾）会留下两行，
-- 而读路径 `profile/service.py` 的 `GET /me/export` 用的是 `scalar_one_or_none()`：
-- 两行 ⇒ `MultipleResultsFound` ⇒ **那个用户的导出永久 500**。
--
-- 治因：唯一索引（一个题会话一行）。治表：同一笔改动把读路径改成"取一行"。
-- 与 `uq_explanation_key` 同理用**独立索引**而不是表内 UNIQUE —— runner 的 splitter
-- 只按分号切语句，独立索引可以写成 `IF NOT EXISTS`（幂等声明式，ADR-0011）。
--
-- ⚠️ 加索引之前必须**先去重**：真库上真出现过两行的情形（上面那条路径），而
-- `CREATE UNIQUE INDEX` 撞到既有重复会整文件回滚、不记账。
--
-- 去重保留哪一行：**优先 `status='ok'`，其次最早的**。理由是这两行的来源不同 ——
-- `failed` 那一行是"模型当时没接上"（分数全 0、评语是失败文案），后来成功了那次
-- 才是真正的判定；把好的一行删掉等于把那次判分白花了。两行都 ok（或都 failed）
-- 时保留最早的那一行：先出的报告是用户已经看过的那一份。
--
-- ⚠️ 纯 SQL、无分支、不许有 BEGIN / COMMIT（ADR-0011：事务边界由 runner 持有）。

DELETE FROM evaluations
WHERE id NOT IN (
    SELECT COALESCE(MIN(CASE WHEN status = 'ok' THEN id END), MIN(id))
    FROM evaluations
    GROUP BY session_id
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_evaluation_session
    ON evaluations(session_id);
