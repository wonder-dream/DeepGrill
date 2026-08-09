# M10 判分模块

> 路径：`app/judge/judge.py` ｜ 规模：~220 行 ｜ 依赖：M3（LLM）、M2（存储）

## 1. 职责

对答题会话做四维评分 + 总分合成（D17）+ 评语/参考答案/薄弱点标签；prompt 按题型差异化。判分是核心质量点，prompt 版本化 + fixture 回归锁行为。

## 2. 接口

```python
PROMPT_VERSION = "judge_v5"   # 判分 prompt 版本常量

def judge(question: Question,
          transcript: list[dict],
          model: str,
          llm,
          *,
          session_id: int | None = None,
          reference: str | None = None,
          max_level: int | None = None) -> Judgment
    """transcript: [{role: user|interviewer, content}]，含追问链全部轮次
    reference: 库内同类高分回答片段（Phase 2 §9.3 参考检索，可选；None 不注入）
    max_level: 深挖追问探到的最大层级 L1-L5（可选），depth 维度按此校准
    """
```

输出 JSON（LLM 契约）：

```json
{
  "scores": {"accuracy": 0-100, "completeness": 0-100,
             "clarity": 0-100, "depth": 0-100},
  "review": "评语",
  "reference_answer": "参考答案",
  "weak_tags": ["RAG"]
}
```

> weak_tags 必须取自共享词表（`app/tags.py`，58 词），与题目标签同词表——薄弱点=薄弱主题，支撑 Phase 2 按 weak_tags 检索同类题复习；prompt 注入词表 + 代码层过滤非词表词（上限 3 个）。

## 3. 关键决策

- **输入组成**（D7）：题目 + 该题 good/bad criteria + 完整对话记录 + 标签/难度——judge 引用随题生成的判分标准，而非现场自由发挥
- **总分合成**（D17）：`total = accuracy×0.3 + completeness×0.3 + clarity×0.2 + depth×0.2`
- **题型差异化 prompt**：knowledge 重准确性、design 重完整性与深度、project 重真实性与反思深度（prompt 内分派）
- **分数钳制**：各维 0-100，越界钳制；缺失维度补 0
- **失败降级**：LLM 失败重试 1 次 → `Judgment(status="failed")` 存库，Web 显示"判分失败可重试"；不影响其他题与后续会话
- **prompt 版本化**：`PROMPT_VERSION` 变更需同步更新 fixture，防 prompt 漂移（v2：weak_tags 收敛共享词表；v3：注入库内同类高分回答参考段，空检索降级不注入；v4：注入深挖追问层级 max_level，depth 按"最终能答到的层级"校准；v5：难度细化 1-5 + 按难度校准判分期望（1-2 答全要点即高分、3 须权衡、4-5 须体现选型与取舍），难度档位定义见 `app/difficulty.py`）

## 4. 错误隔离

- `JudgeError`（retryable）：LLM 调用失败
- 判分失败永远落库（failed 状态），不抛错中断流程
- transcript 超长截断（如 50k 字符）再入 prompt，防上下文溢出

## 5. 测试计划（`tests/test_judge.py`）

| 类别 | 用例 |
|---|---|
| happy | 完整 JSON 判分落库；四维边界分 0/100；criteria 引用生效（prompt 内容断言含 criteria 文本）；total 与四维加权一致（断言公式） |
| edge | 弱标签列表；参考答案为空；追问链超长 transcript 截断；不同题型 prompt 内容差异断言 |
| fail | JSON 缺字段补默认；分数越界钳制；LLM 异常重试后降级落 failed 状态；输出与 schema 完全不符 |
| 回归 | PROMPT_VERSION 变更需显式更新 fixture（测试引用版本常量） |

## 6. 技术选型

**prompt 模板版本化 + M3**（不引入 DeepEval 等评估框架）

- 优：判分是核心质量点，版本化 + fixture 回归锁行为；参考 interview-pilot-ai 的 criteria 注入思路（D7）
- 缺：判分质量上限受 judge_model 能力约束（可通过 config 升级模型）
- 理由：个人工具规模下评估框架过度设计；prompt 版本化 + 回归测试成本最低且有效

## 7. 实现提示

- prompt 结构：system（面试官角色 + 题型判分标准 + JSON schema）+ user（题目/criteria/transcript 组装）
- 评分公式独立函数 `compute_total(scores) -> float`，单独可测
- `Judgment.status` 字段（ok/failed）参与落库与 Web 展示
- fixture 预录响应覆盖：满分/低分/缺字段/越界/非法 JSON 五类
