"""面试领域的判定规则 —— **纯函数，不碰数据库、不调模型**。

把规则单独放一个文件，是因为它们是这条链上**最该被测透**的部分（追问、收尾、
分数合成），而"把它们藏在编排类的方法里"会让测试必须先造出一个带库带模型的
世界。纯函数让这些规则可以被逐条钉住：

| 规则 | 出处 |
|---|---|
| 分数合成 `.3/.3/.2/.2` | `docs/v1行为规格.md` §4.2 —— **继承** |
| 分值域 0-100，越界或非数值**钳到 0** | §4.3 —— 继承（容错优先于严格） |
| 连续两轮没有**新**命中 → 收尾 | 决策 28 / §3.4 的判据改写 |
| 轮数上限由外层强制 | §3.5 —— 继承（不能只信 LLM 自觉 finish） |
| 命中状态是**三值** | 决策 28 + `CONTEXT.md`「命中状态」 |
| 取"上一轮"必须在写本轮**之前** | §3.6 —— v1 的具体 bug，重写极易再犯 |
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 命中的三种状态。**"未涉及"不是可选装饰**：掌握度矩阵的分母是"被考过的考察点"，
#: 靠它把本轮没问到的排到分母之外（见 `migrations/0001_initial.sql` 的 hits 注释）。
HIT = "命中"
MISS = "未命中"
NOT_COVERED = "未涉及"
STATUSES = (HIT, MISS, NOT_COVERED)

#: 四维权重（§4.2，标为「继承」—— 可重标定，但先别动）。
WEIGHTS = {"accuracy": 0.3, "completeness": 0.3, "clarity": 0.2, "depth": 0.2}

DIMENSIONS = tuple(WEIGHTS)


@dataclass
class HitSnapshot:
    """一轮的累积快照：`criterion_id → 状态`。

    它是**累积**的（决策 28）：本轮要输出"到此刻为止"的全部考察点状态，而不是
    本轮增量。理由有三条（数据模型里写明）：追问决策要"此刻漏了什么"、掌握度要
    "最终答到了什么"、而"未命中"这种**缺席信息**在增量语义下无法表达。
    """

    statuses: dict[int, str] = field(default_factory=dict)

    def merge(self, updates: dict[int, str]) -> HitSnapshot:
        """把本轮判定并进快照，返回**新**快照（不改调用方手上的那份）。

        并的规则有方向性：已命中不会被后续的"未命中/未涉及"打回去（答到了就是
        答到了）；但"未涉及"可以被后续的命中/未命中覆盖（这一轮问了，就不再是
        "没涉及"）。
        """
        merged = dict(self.statuses)
        for cid, status in updates.items():
            if merged.get(cid) == HIT and status != HIT:
                continue
            merged[cid] = status
        return HitSnapshot(merged)

    def hit_ids(self) -> set[int]:
        return {cid for cid, s in self.statuses.items() if s == HIT}

    def miss_ids(self) -> set[int]:
        return {cid for cid, s in self.statuses.items() if s == MISS}

    def covered_ids(self) -> set[int]:
        """**被考过**的考察点（命中 + 未命中）—— 掌握度矩阵的分母。"""
        return {cid for cid, s in self.statuses.items() if s != NOT_COVERED}

    def to_json(self) -> dict[str, str]:
        """JSON 列里键必须是字符串（`criterion_id → 状态`）。"""
        return {str(cid): s for cid, s in sorted(self.statuses.items())}

    @classmethod
    def from_json(cls, raw: dict[str, str] | None) -> HitSnapshot:
        if not raw:
            return cls()
        return cls({int(k): v for k, v in raw.items()})


def normalize_scores(raw: object) -> dict[str, int]:
    """把模型吐的四维分钳进 0-100（§4.3：**越界或非数值一律钳到 0，不抛错**）。

    容错优先于严格 —— 判分失败一次不该让整场面试崩掉，而"钳到 0"让一次脏输出
    最多让这道题的分数难看，不会污染别处。
    """
    out: dict[str, int] = {}
    source = raw if isinstance(raw, dict) else {}
    for dim in DIMENSIONS:
        value = source.get(dim)
        try:
            n = int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            n = 0
        out[dim] = min(100, max(0, n))
    return out


def total_score(scores: dict[str, int]) -> int:
    """按 `.3/.3/.2/.2` 合成，**四舍五入取整**（§4.2）。"""
    value = sum(scores.get(dim, 0) * w for dim, w in WEIGHTS.items())
    return int(value + 0.5)


def count_new_hits(previous: HitSnapshot, current: HitSnapshot) -> int:
    """本轮**新**命中了几条。

    这是收尾判据的输入（§3.4 的判据改写）："在明显不会的点上不要耗轮数"这条
    成本保护必须保留，只是判据从"连续两次答差"换成"连续两轮没有新命中"。
    """
    return len(current.hit_ids() - previous.hit_ids())


def should_finish(
    *,
    round_no: int,
    max_rounds: int,
    previous: HitSnapshot,
    current: HitSnapshot,
    model_suggests_finish: bool = False,
    dry_rounds: int = 2,
) -> bool:
    """收尾判据。**否决权在代码手里**（ADR-0001）。

    顺序是有意的：轮数上限**先判** —— 它是否决权，不该被模型的建议绕开。
    然后判"连续 `dry_rounds` 轮没有新命中"（§3.4 的成本保护：在明显不会的点上
    不要耗轮数），最后才轮到模型的建议。

    `dry_rounds` 的门槛是必要的：第一轮结束时"上一轮快照"是空的，"没有新命中"
    这个判据在那一刻没有意义 —— 不留门槛会让每道题只问一轮。
    """
    if round_no >= max_rounds:
        return True
    if round_no >= dry_rounds and count_new_hits(previous, current) == 0:
        return True
    return model_suggests_finish
