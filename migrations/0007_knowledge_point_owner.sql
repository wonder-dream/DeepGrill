-- 0007_knowledge_point_owner.sql —— 知识点要能回答"这是谁的"（决策 91）
--
-- 现场（第 1 轮实测）：用户注销之后，`私有题集（用户 {id}）` 那个锚点**还在**，
-- 挂在它下面的考察点也还在 —— 而那条考察点的文本是从用户简历里派生的，
-- 锚点名字里还带着 user id。`delete_account()` 从头到尾没碰 `knowledge_points`。
--
-- 为什么加列而不是"按名字删"：名字是 `offline/profile_pipeline.py` 拼出来的字符串，
-- 按它匹配等于把一处格式约定复制成两处 —— 哪天名字的格式改了，删除会**静默失效**
-- （锚点留下、简历派生的文本留下），而失败没有任何提示。给知识点一个 owner 列之后，
-- "谁的锚点"是库里的事实，不是字符串约定。
--
-- 为什么不是"用户私有题集专用的另一张表"：锚点就是 `knowledge_points` 的一行
-- （`status='draft'`、`origin='manual'`），它要被 `questions.primary_point_id`
-- 与 `criteria` 引用 —— 换一张表就得把这两处的外键一起改，而收益只是"少一列"。
--
-- ⚠️ 纯 SQL、无分支、不许有 BEGIN / COMMIT（ADR-0011：事务边界由 runner 持有）。

ALTER TABLE knowledge_points ADD COLUMN owner_user_id INTEGER REFERENCES users(id);

-- 注销时要"按 owner 找出全部私有锚点"：没有这个索引就是全表扫。
-- （`knowledge_points` 在 2C2G 上不会大，但这条索引与查询形状一一对应，零成本。）
CREATE INDEX IF NOT EXISTS idx_kp_owner ON knowledge_points(owner_user_id);
