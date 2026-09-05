# UGC 用户提交与题目反馈方案

> 状态：已确认并实现（后端）｜ 日期：2026-08-16

## 一、目标

1. 注册用户可提交面经/简历/直入题，作为公开题库供给侧；
2. 提交内容不直接公开：系统自动出题 → 题目进入现有 owner 审核门禁（reviewed_at=NULL）→ 通过才放行；
3. 登录用户可对公开题目发起“题目质量反馈”（非版权举报），含 duplicate 结构化指认。

## 二、流程

```
用户提交（consent=true） → submission(pending)
   → 后台自动生成题目（facejing/resume/direct）
   → 题目进入审核队列（reviewed_at=NULL）
   → owner 审核通过 → 公共题库

用户对公开题点“题目不好” → question_feedback(open)
   → owner resolve/dismiss（可联动改题/删题）
```

## 三、分类

- wrong：题目/答案有错
- unclear：表述不清
- duplicate：认为与某题重复（需勾选 1~3 道）
- not_interview：不适合当面试题
- other：其他

不设 offensive 分类。

## 四、duplicate 候选

- 自动候选：余弦相似度 `>= 0.78`（含边界）的公开题，毫秒级（3009 题实测 ~0.4ms）；
- 同时支持题库自由搜索；
- 最多勾选 3 道；
- 结果仅作管理员参考，不自动删题；
- 候选能力同时用于用户反馈页与管理端处理页。

## 五、数据模型

- `submissions`：user_id/kind/content/consent/status(pending|processing|completed|failed|removed)/source_id/error
- `question_feedback`：question_id/user_id/category/duplicate_question_ids(JSON)/comment/status(open|resolved|dismissed)
- 同用户同题最多一条 open 反馈（partial unique index）

## 六、接口

- POST `/api/ugc/submissions`、GET `/api/ugc/submissions/me`、GET `/api/ugc/submissions/{id}`
- GET `/api/admin/ugc/submissions`、POST `/api/admin/ugc/submissions/{id}/retry`、POST `/api/admin/ugc/submissions/{id}/remove`
- POST `/api/feedback`、GET `/api/feedback/my`
- GET `/api/admin/feedback`、POST `/api/admin/feedback/{id}/resolve|dismiss`
- GET `/api/questions/{question_id}/similar`（duplicate 候选）

## 七、配置

```yaml
ugc:
  enabled: true
  max_per_user_per_day: 10
  max_content_bytes: 20971520
  require_consent: true
feedback:
  enabled: true
  duplicate_candidate_min_sim: 0.78
  duplicate_max_select: 3
```

## 八、说明

- 普通用户只能看自己的提交/反馈；
- owner 管理所有提交/反馈，题目审核沿用现有 `/api/review` + `/api/admin/questions/*`；
- remove 端点先做“标记 removed”溯源，派生题目由 owner 用现有删题入口处理；
- 前端 UI 待后续接入。
