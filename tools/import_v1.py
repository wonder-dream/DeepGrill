"""从 v1 的 `data/interview.db` 全量导入题目（**决策 49：唯一一次读 v1 库的操作**）。

导入之后 v2 库自足 —— 删掉 v1 目录，重建知识层仍然做得到；而且**初始装配与增量维护
都对着 v2 库跑**，不会分叉。

## 只读是硬约束

v1 那条目录是**规格与数据来源，不是代码来源**（AGENTS.md §五），而且"迁移与知识层构建
都依赖它保持原样"。所以这里用 `mode=ro` 打开，并且加一道**测试守着的护栏**：
`_assert_source_is_untouched()` 在导入前后比对 `data_version`（SQLite 的写计数），
读操作不会让它变。

## 搬什么、不搬什么

| v1 字段 | 去向 | 为什么 |
|---|---|---|
| `stem` / `difficulty` | `questions.stem` / `.difficulty` | 直接复用（`difficulty` 数值按 §11 重标定，但字段先用） |
| `type`（knowledge/design） | `questions.kind` | 同名映射（决策 20 正好是这两类） |
| `good_criteria` / `bad_criteria` | 原样进 v2 库 | **离线素材**（聚类的输入）。运行时判分只读 `criteria` 表（决策 49） |
| `suggested_*` / `reviewed_at` / `selected_at` | **不搬** | 「今日题」与人工前置审核都已废弃（决策 5、数据模型） |
| `embedding` | **不搬** | 嵌入走 API 且不进题目表；旧向量与新模型不在同一空间 |
| 标签（`tag_categories`/`tags`/`question_tags`） | **不搬**（只导出映射） | 标签是主题维度，知识点是能力维度（ADR-0002）。映射留作聚类辅助信号 |

## 不搬的那三类"简历噪声"（§12.3 逐题点名）

```
[62]   "请介绍您参与的RAG项目背景及主要功能。"      → 剔除（对任何人都不可答）
[69]   "请介绍您参与的优惠券项目背景及主要功能。"    → 剔除（同上）
[2136] "请描述你负责的Agent系统架构…"              → **改写为通用题**
```

前两道是 v1 把简历生成的项目题误存进公共题库的产物；第三道是"边缘"——它问的是
设计能力，只是用了第一人称，所以改写而不是剔除。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.bank import repository as bank_repository
from app.db.models import Question

logger = logging.getLogger(__name__)

#: v1 库的位置。它是**只读参考**，默认值指向与本仓库并列的那份 checkout。
DEFAULT_V1_PATH = Path(r"D:\document\InterviewAssistant\data\interview.db")

#: 剔除的题（对任何人都不可答：它们是简历生成的项目题，被误存进了公共题库）
DROP_IDS = frozenset({62, 69})

#: 改写为通用题的题：原文用第一人称引用"你负责的系统"，改成假设式就成立
REWRITE: dict[int, str] = {
    2136: "假设你要设计一个 Agent 系统，你会如何划分模块与决定架构？请说明取舍依据。",
}


class SourceNotReadOnly(RuntimeError):
    """源库必须是只读的 —— 但它被打开了可写。"""


@dataclass
class ImportResult:
    """导入结果。**它要能被展示**：搬了多少、跳过多少、为什么。"""

    scanned: int = 0
    inserted: int = 0
    skipped_existing: int = 0
    dropped: list[int] = field(default_factory=list)
    rewritten: list[int] = field(default_factory=list)
    bad_rows: list[tuple[int, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"扫过 {self.scanned} 道：新导入 {self.inserted}，已存在跳过 {self.skipped_existing}，"
            f"剔除 {len(self.dropped)}，改写 {len(self.rewritten)}，异常 {len(self.bad_rows)}"
        )


def open_v1_readonly(path: Path) -> sqlite3.Connection:
    """**只读**打开 v1 库。

    `mode=ro` 是硬要求：v1 那条目录要保持原样（迁移与知识层构建都依赖它）。
    只读打开还会让"手滑写回去"在 SQLite 层面直接失败（`DELETE` 会抛
    `attempt to write a readonly database`），而不是靠我们记得别写 ——
    有测试钉住这一点。

    ⚠️ 别把 `PRAGMA query_only` 当成这道保护：`mode=ro` 是文件级的只读，
    而 `query_only` 是另一套（连接级）开关，在 `mode=ro` 下它读出来是 0。
    第一版在这里检查了 `query_only` 并因此打印了一句**误导性的警告** ——
    保护其实是好的，是我查错了地方。
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"找不到 v1 题库：{path}\n"
            f"它是只读的数据来源（AGENTS.md §五），路径可用 --v1 指定。"
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def read_v1_questions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """把 v1 的题读成普通字典（**只读 v2 需要的列**）。"""
    rows = conn.execute(
        "SELECT id, type, stem, difficulty, good_criteria, bad_criteria "
        "FROM questions ORDER BY id"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": int(row["id"]),
                "type": str(row["type"] or ""),
                "stem": str(row["stem"] or "").strip(),
                "difficulty": row["difficulty"],
                "good_criteria": row["good_criteria"],
                "bad_criteria": row["bad_criteria"],
            }
        )
    return out


def _normalise_criteria(raw: object) -> str | None:
    """criteria 原样搬（它是 JSON 数组文本）。

    v1 实测 3009/3009 全部有值且都是规范 JSON 数组（§12.1），所以这里**不做转换** ——
    只把"不是 JSON 数组"的挑出来记进 `bad_rows`，不静默丢掉（§3.1）。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    return json.dumps(parsed, ensure_ascii=False)


def import_questions(session: Session, rows: list[dict[str, Any]]) -> ImportResult:
    """把读出来的行写进 v2。

    **幂等靠题干**：v1 实测**零重复题干**（3009 道全不相同），而 v2 的 `questions` 没有
    "来源 id"列可用（不该为一次性导入加列）。所以"题干已存在就跳过"是可靠的 ——
    重复跑一次导入不会翻倍。
    """
    result = ImportResult()
    for row in rows:
        result.scanned += 1
        v1_id = row["id"]

        if v1_id in DROP_IDS:
            result.dropped.append(v1_id)
            continue

        stem = REWRITE.get(v1_id, row["stem"])
        if v1_id in REWRITE:
            result.rewritten.append(v1_id)

        if not stem:
            result.bad_rows.append((v1_id, "题干为空"))
            continue

        kind = row["type"] if row["type"] in ("knowledge", "design") else "knowledge"
        difficulty = row["difficulty"]
        if not isinstance(difficulty, int) or not 1 <= difficulty <= 5:
            # v1 实测全是 1-5，但**不信输入**：越界就钳到合法区间并记账
            try:
                difficulty = min(5, max(1, int(difficulty)))
            except (TypeError, ValueError):
                result.bad_rows.append((v1_id, f"难度不合法：{row['difficulty']!r}"))
                continue

        good = _normalise_criteria(row["good_criteria"])
        bad = _normalise_criteria(row["bad_criteria"])
        if good is None:
            result.bad_rows.append((v1_id, "good_criteria 不是 JSON 数组"))
            continue

        if bank_repository.stem_exists(session, stem):
            result.skipped_existing += 1
            continue

        session.add(
            Question(
                kind=kind,
                stem=stem,
                difficulty=difficulty,
                # primary_point_id 留空：**挂载由知识层管道做**（决策 46 的增量纪律），
                # 一次性导入不猜挂载点
                good_criteria=good,
                bad_criteria=bad,
                origin="seed",
                visibility="public",
                answer_tier="long_tail",   # 冷门题只给评分标准，参考答案按需生成（决策 12）
            )
        )
        session.flush()
        result.inserted += 1
    return result


def export_tag_mapping(conn: sqlite3.Connection, out_path: Path) -> int:
    """导出「题目 id → v1 原标签」映射（**聚类辅助信号与回查依据**）。

    v1 的标签体系**不进 v2 的产品结构**（主题维度 ≠ 能力维度），但它对聚类有用：
    同一批标签下的题往往在考相近的东西。所以导出成文件而不是库表 ——
    它是一次性素材，天然会过期（`data/` 是运行期产物目录）。
    """
    rows = conn.execute(
        """
        SELECT q.id AS question_id, t.name AS tag
        FROM questions q
        JOIN question_tags qt ON qt.question_id = q.id
        JOIN tags t ON t.id = qt.tag_id
        ORDER BY q.id
        """
    ).fetchall()
    mapping: dict[str, list[str]] = {}
    for row in rows:
        mapping.setdefault(str(row["question_id"]), []).append(str(row["tag"]))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return len(mapping)


def _assert_source_is_untouched(before: int, after: int, path: Path) -> None:
    """护栏：导入之后源库的写计数必须没变。

    `PRAGMA data_version` 在**别的连接**改了库时才会变 —— 所以这里用同一个连接
    前后各读一次：只读连接上它应当恒定。这条断言的意义是让"手滑写回 v1"响亮地
    失败，而不是靠我们记得别写（AGENTS.md §五：不要修改它）。
    """
    if before != after:
        raise SourceNotReadOnly(
            f"{path} 的 data_version 变了（{before} → {after}）—— 源库被改写了，这不允许"
        )


def run(
    *,
    session: Session,
    v1_path: Path = DEFAULT_V1_PATH,
    tag_mapping_path: Path | None = None,
) -> ImportResult:
    """完整导入流程：只读打开 → 读 → 写 v2 → 导出标签映射。"""
    conn = open_v1_readonly(v1_path)
    try:
        version_before = int(conn.execute("PRAGMA data_version").fetchone()[0])
        rows = read_v1_questions(conn)
        result = import_questions(session, rows)
        if tag_mapping_path is not None:
            exported = export_tag_mapping(conn, tag_mapping_path)
            logger.info("导出标签映射 %d 道题 → %s", exported, tag_mapping_path)
        version_after = int(conn.execute("PRAGMA data_version").fetchone()[0])
        _assert_source_is_untouched(version_before, version_after, v1_path)
    finally:
        conn.close()
    return result


def main(argv: list[str] | None = None) -> int:
    """命令行入口：`python -m tools.import_v1 [--v1 路径] [--dry-run]`。

    它是**一次性动作**（决策 49），所以住在 `tools/`（做完可以整体删除）而不是
    `app/`。`--dry-run` 只读不写 —— 第一次跑之前先看一遍它打算搬什么，成本很低。
    """
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from app.config import Settings
    from app.db import create_db_engine, create_session_factory

    parser = argparse.ArgumentParser(prog="python -m tools.import_v1", description="v1 题库全量导入")
    parser.add_argument("--v1", type=Path, default=DEFAULT_V1_PATH, help="v1 库路径（只读）")
    parser.add_argument("--dry-run", action="store_true", help="只统计、不写库")
    parser.add_argument("--tag-mapping", type=Path, default=None,
                        help="把「题目 id → v1 原标签」导出到这个文件")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()

    if args.dry_run:
        conn = open_v1_readonly(args.v1)
        try:
            rows = read_v1_questions(conn)
        finally:
            conn.close()
        kept = [r for r in rows if r["id"] not in DROP_IDS]
        print(f"（dry-run）会扫过 {len(rows)} 道，剔除 {len(DROP_IDS)}，"
              f"改写 {len(REWRITE)}，拟导入 {len(kept) - len(REWRITE)} 道")
        print("不改任何库。去掉 --dry-run 就会真的导入。")
        return 0

    engine = create_db_engine(settings.resolved_database_path())
    with create_session_factory(engine)() as session:
        result = run(session=session, v1_path=args.v1, tag_mapping_path=args.tag_mapping)
        session.commit()
    print(result.summary())
    if result.dropped:
        print(f"  剔除的题 id：{result.dropped}")
    if result.rewritten:
        print(f"  改写的题 id：{result.rewritten}")
    if result.bad_rows:
        print(f"  异常行（未导入）：{result.bad_rows[:5]}")
    print("\n下一步：知识层构建管道（python -m app.cli propose）—— 导入的题此时还没有知识点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
