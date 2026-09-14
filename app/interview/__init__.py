"""面试编排领域（CONTEXT.md 的**编排**）：问哪些题、追问到什么程度、什么时候结束。

**它是一条 workflow（带循环的状态机），不是自主 Agent**（ADR-0001）。分界线是
**否决权在代码手里**：模型可以建议 `should_finish`，但 `max_rounds` 到顶时由
`should_finish()` 强制收尾。

**状态显式落库**（决策 19 的两层结构）：`interviews`（一场）→ `sessions`（一道题）
→ `attempts`（一轮）。所以进程重启、用户中途退出再回来，状态都在库里 ——
这是"带循环 + 挂起恢复"不能用静态 DAG 实现的原因（ADR-0001 的 ⚠️）。

本领域**只写** `interviews` / `sessions` / `attempts` / `evaluations` 这几张表；
题目与考察点通过 `bank` 领域拿（领域之间互不 import —— ADR-0005）。
"""
