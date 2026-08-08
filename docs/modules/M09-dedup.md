# M9 去重模块

> 路径：`app/pipeline/dedup.py` ｜ 规模：~120 行 ｜ 依赖：app/embed.py（bge-m3）+ numpy
> 更新日期：2026-08-08（Phase 2 §9.1：LLM 语义判断 → embedding 余弦阈值）

## 1. 职责

新题 vs 库内旧题的重复判断：归一化精确重复快速路径 → bge-m3 embedding 余弦相似度阈值 → 返回去重后的新题列表。

## 2. 接口

```python
def dedup(new_questions: list[Question],
          existing_questions: list[Question],
          embedder) -> list[Question]
    """保留不重复的新题；embedder 注入（测试用 FakeEmbedder，生产 Embedder）"""

def normalize_stem(stem: str) -> str
    """纯函数：全角转半角、去空白标点、小写，供快速路径用"""
```

## 3. 关键决策

- **两级策略**：归一化精确重复（哈希集合，零成本，无需 embedding）→ 余弦全量比对（numpy 矩阵乘法，36×N 毫秒级）
- **阈值**：`SIM_THRESHOLD = 0.85`——真实模型标定：60 题/1770 对最大相似度 0.797，0.85 保证零误杀（标定详情见 docs/语义去重方案.md §6）
- **降级路径**：embedding 不可用（`EmbedError`）→ 降级仅哈希精确去重——**宁重复勿丢题**（防误删新题）
- **向量持久化**：Question.embedding BLOB 列；旧题 NULL 首次自动补算（ensure_embeddings），新题向量随 daily 落库，避免下次重算
- 同批多条与同一旧题重复：embedding 全量比对天然全部判重（无候选集概念）

## 4. 错误隔离

- embedder 加载/推理失败 → 降级哈希路径，不抛错
- 本模块不落库（补算旧题向量除外）、不抛业务异常；输入空列表直接返回空

## 5. 测试计划（`tests/test_dedup.py`）

| 类别 | 用例 |
|---|---|
| happy | 归一化精确重复判重（断言零 encode）；预设向量语义重复判重；不同主题放行；阈值边界 0.84 保留 / 0.86 判重 |
| edge | 空新旧列表；旧题已有 embedding 不重算（断言只 encode 新题）；旧题 NULL 自动补算 |
| fail | embedder 抛错 → 降级仅哈希；降级时语义近似放过（宁重复勿丢题）；批内多条全部判重 |
| 幂等 | 同一批跑两次结果一致 |

## 6. 技术选型

**bge-m3（sentence-transformers，GPU）+ numpy 暴力余弦 + SQLite BLOB**

- 优：多语 SOTA 质量、离线免费、GPU 上 209 题全量 20s、0 向量库依赖
- 缺：torch 依赖重（GPU 版需手动安装）、2.2GB 模型下载（一次性）
- 理由：题目量级（千级）numpy 暴力比对毫秒级，sqlite-vec/chroma 属过度设计，题量到万级再评估

## 7. 实现提示

- `ensure_embeddings`：仅对 embedding 为 NULL 的题 encode；有 id 的 merge 落库，无 id（批内新题）仅写内存
- 余弦 = 点积（encode 时已 L2 归一化）
- 降级路径是回归高发区，测试重点覆盖
