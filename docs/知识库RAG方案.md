# 知识库 RAG 方案（2026-08-10）

> 状态：**已确认、实施中** ｜ 目的：保证判分/追问/复习卷准确性（LLM 回答前检索相关知识注入）

## 决策汇总

| 讨论项 | 决策 |
|---|---|
| 知识来源 | 用户提供的八股文文档（md/txt，放 `data/knowledge/`，gitignore） |
| 存储 | **FAISS 内存索引层 + SQLite 数据层**（不引 Milvus；20 用户/10 并发规模 FAISS search 只读线程安全） |
| 索引重建 | **version 号自动重建**（导入即生效，不重启） |
| 注入场景 | 判分（judge_v7，知识标准参照与用户高分参考并存分区）+ 追问（chain_v4）+ 复习卷（materials） |
| 切块 | 500-800 字/块，段落优先，代码块 ``` 边界完整保留 |
| 注入量 | top-3~5 块，拼接截断 4000 字 |
| 同步 | knowledge_chunks 存 interview.db → 现有「导出备份 zip → 服务器导入」天然携带（零新代码） |

## 数据模型（db.py 幂等迁移）

```
knowledge_chunks: id / title / content / source_hash(唯一,幂等) / embedding BLOB / created_at
knowledge_meta:   id=1 / version INTEGER
```

## 模块

- `scripts/import_knowledge.py`：扫描 data/knowledge → 切块 → bge-m3 批量向量化 → 入库 → version+1（幂等可重跑）
- `retrieval.py` `KnowledgeIndex`：进程内单例、IndexFlatIP（bge-m3 已 L2 归一化，内积=余弦）、version 检测自动重建（2 万块 1-2s）、降级链 faiss→numpy→不注入、search 回查 SQLite
- 三处注入：judge_v7 新增「标准参照」区 / CHAIN_PROMPT V4 追问依据 / 复习卷 materials；prompt 版本变更同步 fixture

## 测试与验证

- import 切块（含代码块）/幂等/version 递增；KnowledgeIndex version 重建/FAISS 冒烟/降级；judge_v7/chain_v4/复习卷注入；并发检索基准（10 线程×100 次，P95<200ms）
- 人工：导入八股文 → 答题看判分标准参照 → 追问有依据 → 复习卷含知识
