# 数据库重建方案（绿地重建，2026-08-11）✅ 已执行完毕

## 执行结果（2026-08-11 完成）
- 15 表新 schema 落地（含 user_favorites、question_tags；tag_items→tags；删 review_suggestions/status/raw_text/social）
- 全 FK ON DELETE CASCADE（删题/删标签/删用户一条 DELETE，DB 级联已验证）
- 每用户池选题 + 薄弱标签个性化推荐（db.pick_questions/_rank_recommend）；pipeline 不再选题（懒加载接管）
- 收藏功能（API + 侧边栏 + 卡片 ★）；审核进度入库（reviewed_at + suggested_*）
- owner 不变量：注册无 owner 补位 + 唯一 owner 禁删（删除接口 409 由 require_owner 层保障）
- 旧库备份 data/backup/interview.db.bak_pre_rebuild_20260811（32MB 完整）
- 知识库重建 2503 块（version=1）；测试 355 全绿；端到端 smoke 通过
- 服务运行中（127.0.0.1:8000），题库 0 题待重建（bootstrap_generate / 上传 / 爬取）

## 背景与目标
用户决策：全库 14 张表重新设计；现有数据（2915 题/2499 知识块/账号/作答）**全部丢弃**；重建后知识库从原始资料重新导入。
动机（1+2）：① 解决踩坑——tags JSON 数组导致删除/检索/刷新问题、FK 级联缺失导致删题 500；② 精简重构——去冗余、为后续功能打底。

## 设计原则
- 所有 FK 显式 `ondelete="CASCADE"`（SQLModel Field + relationship `passive_deletes=True`），删题/删用户/删分类一条 DELETE 完事
- 标签规范化：题目-标签多对多关联表（替代 JSON 数组），根治 `json_each` 删除与 LIKE 检索
- 审核建议并入题目表（替代独立 review_suggestions 表，生命周期与题一致）
- 结构合理的域**保持不变**：sources（管线核心，raw_text 保留）、questions.status 选题池机制（pending/today + selected_at）、user_picks 每用户选题记录（双轨不冲突）、embedding BLOB 列、knowledge 域、task_logs

## 新 Schema（14 表 → 14 表）
### 保留原样（8 张）
- `users`（id/username UNIQUE/password_hash/role/created_at）
- `user_tokens`（user_id FK→users **CASCADE**/token_hash UNIQUE/expires_at/created_at）
- `sources`（type/url/title/raw_text/cleaned_text/fetched_at/source_hash UNIQUE）— 爬取生成管线核心
- `knowledge_chunks` / `knowledge_meta`（RAG 域）
- `task_logs`（运维）
- `attempts` / `judgments`（session_id FK→sessions **CASCADE**，关系级联保留）
- `user_picks`（user_id、question_id FK **CASCADE**）

### 调整（3 张）
- `questions`：删 `tags` JSON 列；删 `source_id` 约束不变；**新增** `suggested_category`、`suggested_tags`(JSON)、`suggested_difficulty`、`suggested_at`（审核建议并入，替代 review_suggestions）；其余（type/stem/difficulty/good_criteria/bad_criteria/status/selected_at/embedding/created_at）不变
- `tag_categories`：不变（id/name UNIQUE/is_custom），**FK 级联**到 tags
- `tag_items` → 改名 **`tags`**（id/category_id FK→tag_categories **CASCADE**/name UNIQUE/is_custom）
- `sessions`：question_id/user_id FK **CASCADE**（kind/status/started_at/ended_at 不变）

### 新增（1 张）
- `question_tags`（question_id FK→questions **CASCADE** + tag_id FK→tags **CASCADE**，复合主键 (question_id, tag_id)）— 多对多

### 删除（1 张）
- `review_suggestions` → 并入 questions.suggested_*

## 关键机制
1. **删标签**：`DELETE FROM tags WHERE id=?` → question_tags 级联清关联，一条语句，无 json_each 扫描
2. **删分类**：`DELETE FROM tag_categories WHERE id=?` → tags 级联删 + question_tags 级联清
3. **删题**：`DELETE FROM questions WHERE id=?` → sessions/attempts/judgments/user_picks/question_tags 全部级联
4. **按标签检索**：`/api/bank?category=` 改 JOIN question_tags（替代 `_tags_exists` LIKE）
5. **按标签统计**（tag-cloud）：`SELECT tag_id, COUNT(*) FROM question_tags GROUP BY tag_id`（替代 Python 计数）
6. **词表校验**：generate/judge 的 TAG_VOCABULARY 内存过滤不变；写入时 INSERT question_tags
7. **审核建议**：快照写入 questions.suggested_*；审核完成清零；删除题目无残留

## 代码改动清单
| 文件 | 改动 |
|---|---|
| `app/models.py` | 重写：新 schema、全 FK ondelete CASCADE、新增 question_tags、ReviewSuggestion 移除、tags 表定义 |
| `app/tags.py` | reload_tags() 种子写入 tags 表；TAG_CATEGORIES/TAG_VOCABULARY 内存常量机制保留 |
| `app/web/routes.py` | `_tags_exists`/`_tags_contains` 改 JOIN；`/api/tags`、tag-cloud 改关联表；admin 删分类/删标签/删题逻辑大幅简化（级联）；`/api/review/suggestions` 改读 questions 列；PUT 保存题拆分 question_tags 写入；`/api/bank` 标签筛选 |
| `app/pipeline/generate.py` | 生成题后 tags 列表 → question_tags 批量插入 |
| `app/judge/judge.py` | 只读词表校验，无需改（核对） |
| `app/db.py` | backfill_owner_data 等涉及 tags 处适配（核对 dedup 是否用 tags） |
| `app/web/static/reviewer.js` | 无 schema 依赖（suggestions 端点字段不变），少量核对 |
| `scripts/snapshot_tags.py` | **删除**（快照并入 questions 列） |
| `scripts/*.py`（trim_bank/retag_empty_tags/migrate_tags/bootstrap_generate/collect_nowcoder_days） | tags 相关写入适配 question_tags |
| `tests/*` | 大量适配：fixture 建题改关联、admin 删除断言（级联后无 FK 错误）、tags 路由断言；新增 question_tags 级联测试 |
| `docs/题库人工审核方案.md` | 更新表结构描述 |

## 数据动作（执行顺序）
1. 备份旧库：`data/interview.db` → `data/backup/interview.db.bak_pre_rebuild_20260811`（保险，可删）
2. 删除旧库文件（重启后 init_db 自动建新 schema + 种子 13 分类/81 标签）
3. 重建知识库：`uv run python scripts/import_knowledge.py`（资料在 `data/knowledge/`，bge-m3 本地重新 embedding，预计 30-60 分钟）
4. 用户注册：首用户自动为 owner

## 验证
- 全量 pytest（预期 357 旧测试经适配后全绿 + 新增级联测试）
- smoke：删题（带标签/会话/建议）一次成功；删标签/分类级联清题上关联；bank 分类筛选、tag-cloud 统计正确
- 浏览器：主界面注册登录 + 审核页端到端

## 风险与回退
- 知识库重嵌耗时 30-60 分钟（本地 bge-m3，无 API 成本）；失败可重跑（source_hash 幂等）
- 测试适配量大（357 个），先跑通核心路径再全量
- 回退：备份库 + git 历史（models.py 改动前 commit 点）
- 题库为空期间主界面功能不可用（无题可练），属预期
