# GitHub 许可白名单与来源溯源方案

> 状态：已确认（方案 B：对象列表 + 字符串兼容）｜ 日期：2026-08-16
> 目标：公开/生产只消费有明确许可且可再分发的 GitHub 仓库；每个 Source 带 license/author/repo_url 溯源。

## 1. 现状

- `Source` 只有 `type/url/title/cleaned_text/fetched_at/source_hash`，无许可/作者/仓库字段
- `github.py` clone/pull 后全量导入 markdown，不检查 LICENSE，不记录署名
- `config.yaml` 的 `sources.github_repos` 为字符串列表
- DB 迁移采用 `db.py` 中 `_migrate_*` + `PRAGMA table_info` 幂等范式

## 2. 数据模型

`Source` 新增 3 个可空列：

| 字段 | 类型 | 含义 |
|---|---|---|
| `license` | str? | SPDX 标识（MIT/Apache-2.0/...）；无许可留空 |
| `author` | str? | GitHub owner/组织名 |
| `repo_url` | str? | `https://github.com/{owner}/{repo}` |

迁移：`_migrate_source_provenance(engine)` 幂等 `ADD COLUMN`；历史行保持 NULL，不猜测、不删除。

## 3. 许可证解析

新模块 `app/crawler/license.py`：

- 扫描仓库根：`LICENSE` / `LICENSE.md` / `LICENCE` / `COPYING` / `UNLICENSE`（大小写不敏感）
- 解析优先级：
  1. `SPDX-License-Identifier: <id>`
  2. 标题关键词：MIT / Apache / BSD 3-Clause / BSD 2-Clause / ISC / Unlicense / CC0
- 解析不到返回 None

## 4. 配置（方案 B + 兼容简写）

```yaml
sources:
  nowcoder_enabled: false
  github_repos:
    - "owner/repo1"                    # 简写 = 无额外配置
    - repo: owner/repo2
      expected_license: MIT            # 可选：期望检测到的许可
    - repo: owner/no-license-repo
      manual_license: with-author-permission   # 可选：已人工授权
  github_require_license: true         # 默认 true：无许可跳过
  github_allowed_licenses:             # 默认宽松许可集合
    - MIT
    - Apache-2.0
    - BSD-2-Clause
    - BSD-3-Clause
    - ISC
    - Unlicense
    - CC0-1.0
```

`SourcesConfig` 通过 pydantic validator 将 `github_repos` 的字符串简写归一化为对象，保持旧配置可用。

## 5. 许可判定

每仓库顺序：

1. `manual_license` 已配置 → 直接放行，写入该值
2. 本地检测 LICENSE
3. 检测到且在 `allowed_licenses` → 放行
4. 检测到但不在白名单 → 跳过仓库并告警
5. 未检测到且 `require_license=true` → 跳过
6. `require_license=false`（本地个人兜底）→ 放行且 license 可为空

## 6. 导入流程

`github.py` 增加许可解析与溯源写入：

- `collect(repos, cache_dir, *, require_license, allowed_licenses)`
- 逐仓库解析 repo（str 简写或对象），得到 `author/repo_url/license`
- 许可判定不通过 → 跳过整仓库（保留单仓库隔离语义）
- `_import_file` 写入 `license/author/repo_url`

## 7. 存量处理（本阶段不做）

- 历史已入库 GitHub Source 的 license/author/repo_url 为 NULL
- 不做自动删除/隔离/审计
- 公开上线时由 owner 手动挑选题目/来源并做授权确认

## 8. 测试

- license 解析单测：MIT/Apache/BSD/无文件/识别不出
- 白名单：允许 MIT、拒绝 GPL、require=true 无许可跳过
- manual override：无 LICENSE 但在对象里配 manual_license → 入库
- 溯源字段：导入后 Source.license/author/repo_url 正确
- 原增量/删除/多仓库/大文件等行为不回退
- 迁移：旧 schema 加列后老数据 license=NULL
- config：字符串简写与对象两种形式解析

## 9. 暂不做

- UI 展示署名/来源
- GitHub API 在线查许可
- 历史源自动删除/隔离
