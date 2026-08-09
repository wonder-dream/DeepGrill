# M8 题目生成模块

> 路径：`app/pipeline/generate.py` ｜ 规模：~250 行 ｜ 依赖：M3（LLM）、M2（存储）

## 1. 职责

面经 → knowledge/design 题 + good/bad criteria + 标签/难度；简历 → project 深挖题；统一落 Question 表。

## 2. 接口

```python
def generate_from_source(source: Source,
                         existing_questions: list[Question]) -> list[Question]
    """面经源 → knowledge/design 题；existing 供 prompt 引用防重复"""

def generate_project_questions(resume_source: Source, limit: int) -> list[Question]
    """简历源 → project 深挖题，受 limit 节流"""
```

LLM 输出契约（`json_schema` 结构化输出）：

```json
[{
  "type": "knowledge|design",
  "stem": "题干",
  "tags": ["RAG", "检索"],
  "difficulty": 1,
  "good_criteria": ["高分标准..."],
  "bad_criteria": ["扣分特征..."]
}]
```

> tags 必须取自共享词表（`app/tags.py`，58 词，6 大类），prompt 注入词表 + 代码层过滤非词表词（上限 5 个）。词表用途见 docs/标签词表方案.md。
> difficulty 为 1-5 整数，prompt 注入分级标准（`app/difficulty.py` `DIFFICULTY_SCALE_TEXT`），代码钳制 1-5（2026-08-09 起由 1-3 细化）。

## 3. 关键决策

- **题型分流在 prompt 层**：LLM 决定 knowledge/design 与内容，代码只做结构校验与落库
- **good/bad criteria 随题生成**（D7）：判分时注入 judge，引用随题标准而非现场自由发挥
- **project 生成器**：从简历源提取项目清单 → 每项目一条深挖题（"讲一下 XX 项目的技术难点和取舍"），按 `daily.project_limit` 节流；未导入简历则不生成
- **失败容忍**：单篇面经失败仅记 task_logs 跳过，不中断批量；产出空列表合法（面经无有效问题）
- **输出校验**：缺 criteria 补默认（good=["完整、准确、结构清晰"]，bad=["答非所问"]）；非法 type 丢弃该条；difficulty 钳制 1-5；tags 过滤词表外词（V2 起）

## 4. 错误隔离

- `GenerationError`（LLM 调用失败/输出结构整体非法）；单篇失败由 M12 捕获跳过
- 条目级容错：一条非法不影响同批其他条目
- 落库失败（UNIQUE 等）由 M2 层处理，本模块不重复防御

## 5. 测试计划（`tests/test_generate.py`）

| 类别 | 用例 |
|---|---|
| happy | 面经含 3 问 → 3 题含 criteria（FakeLLM 预录）；简历 → project 题；existing_questions 注入 prompt 断言 |
| edge | 面经全是寒暄无问题 → 空结果；题目超长截断；criteria 缺字段补默认；非法 type 条目被丢弃 |
| fail | LLM 输出非 JSON → GenerationError 且该面经跳过；LLM 异常重试后失败；与库内重复的衔接（M9 拒收） |
| 一致性 | good/bad criteria 与 stem 强绑定（同一条目内不串位） |

## 6. 技术选型

**prompt 模板 + M3 结构化输出**，无独立第三方

- 优：生成逻辑核心在 prompt 设计，代码层保持薄；prompt 版本化常量便于回归
- 缺：prompt 质量波动是主要风险（由 fixture 回归测试锁定）
- 理由：题目生成不涉及复杂计算或框架，引入 LangChain 等反而稀释可控性

## 7. 实现提示

- prompt 模板放模块常量 `GENERATE_PROMPT_V2`，含类型定义、criteria 要求、防重复指示（引用 existing 题目标题）、共享词表约束
- 生成结果先经校验函数 `_validate_questions(parsed) -> list[Question]` 再落库
- 测试的 FakeLLM 预录响应覆盖：3 题正常 / 0 题寒暄 / 非法 type / 非 JSON 四种
