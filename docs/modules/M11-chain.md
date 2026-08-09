# M11 追问链模块

> 路径：`app/judge/chain.py` ｜ 规模：~250 行 ｜ 依赖：M3（LLM）、M10（判分）、M2（存储）
> 更新日期：2026-08-08（深挖协议 V2：五层阶梯 + 回答质量判定 + 层级落库）

## 1. 职责

深挖式多轮面试对话状态机（Deep-Probe Chain）：五层深度阶梯逐层追问到"不会为止"、回答质量驱动策略、20 轮上限强制结束（D15）、层级数据落库供判分校准（D18 断点恢复保留）。

## 2. 接口

```python
class ChainSession:
    def __init__(self, session_id: int, question: Question): ...

    def next_round(self, answer: str) -> RoundResult
        """提交回答 → (interviewer_message, finished)；内部落 attempts 并驱动 LLM"""

    def finish(self, reference: str | None = None) -> Judgment
        """结束追问链 → 调用 M10 整轮判分（带 max_level + 可选参考）"""

def resume(session_id: int, question: Question) -> ChainSession
    """从 attempts 表重建上下文，轮数正确接续（含 max_level 恢复）"""
```

`RoundResult = {interviewer_message: str, finished: bool}`

## 3. 关键决策

- **深挖协议（V2，2026-08-08）**：五层深度阶梯 L1 概念 → L2 原理 → L3 权衡 → L4 边界/反例 → L5 横向联系；每轮追问 ≥ 上一层
- **回答质量判定**：每轮 LLM 输出 `quality`（correct/partial/wrong/unsure）+ `level`（1-5），驱动策略——correct 继续加深（L5 连续 2 轮 correct 才证明充分）；wrong/unsure **连续 2 次**判定探到底收尾（单次差评再给机会）；partial 同层挖缺口
- **finish 仅两种**：连续 2 次差评探到底 / L5 证明充分；"答对就结束"不存在；20 轮上限兜底
- **层级落库**：attempts.level（每轮追问层级）；finish 时 max_level 传入 M10 判分（judge v4 按"最终能答到的层级"校准 depth 维度）
- **状态持久化**：每轮落 attempts（round_no、is_followup、answer、feedback、level），恢复时全量重建上下文（含 max_level）
- **单轮失败降级**：LLM 失败重试 → 仍失败则本轮降级为固定提示（"请继续"），不中断会话
- **空回答/单字回答**：由 Web 层校验拒绝（400），状态机本身不防御
- **判分衔接**：finish 后调 M10；判分失败落 failed 状态，可重试
- 非法调用（finished 后再 next_round）抛 `ChainStateError`

## 4. 错误隔离

- `ChainStateError`：状态机非法调用（如 finished 后继续）
- LLM 失败不中断会话（降级提示）；判分失败不抛错（failed 落库）
- 恢复时 attempts 缺失 → 视为新会话或抛错由 Web 层转 404

## 5. 测试计划（`tests/test_chain.py`）

| 类别 | 用例 |
|---|---|
| happy | 答得好 → 逐层深挖到 L5 连续 correct 才 finish（层级递增落库断言）；连续 2 次差评 → 探到底收尾；单次差评不误杀 |
| edge | 恢复后轮数正确接续（含 max_level 恢复）；finish 与判分衔接（prompt 含追问深度 L{n}）；每轮反馈+层级写入 attempts |
| fail | LLM 每轮失败 → 降级提示继续；判分失败仍落库 failed；finished 后 next_round 抛 ChainStateError |

## 6. 技术选型

**纯状态机 + attempts 持久化**，无框架（不引入 LangGraph 等）

- 优：轮数/状态逻辑简单可控、零依赖、简历上可清晰讲解
- 缺：无现成对话管理（本场景不需要工具调用/并行分支）
- 理由：LangGraph 图编排对"单链 20 轮"过度设计，代码量反而更大；差评计数由 prompt 看对话历史判断，代码不维护状态机

## 7. 实现提示

- 追问决策 prompt（`CHAIN_PROMPT_V3`）输出 `{action, followup, quality, level}`；level 非法（非 1-5 整数）降级为 None 不落层级
- 难度分级追问（2026-08-09）：`target_level_for(difficulty)`（min(5, difficulty+1)）注入目标深度、`max_rounds_for(difficulty, config_max)`（min(config_max, 3+difficulty*3)）收紧轮次；见 docs/深挖追问方案.md §1.5
- quality 决策完全在 prompt 层（LLM 看对话历史判断"最近两轮"），代码只做结构校验
- 轮数计数基于 attempts 表 `max(round_no)+1`（恢复安全）
- FakeLLM 测试路径矩阵：全 finish / 全 continue / 混合 / 每轮异常
- 20 轮上限常量 `CHAIN_MAX_ROUNDS` 从 config 注入（默认 20，测试可设小值加速）
