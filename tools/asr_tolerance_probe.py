"""一次性：**转写容错到底有没有用** —— 同一段被写错术语的回答，跑「改前 / 改后」。

背景（决策 85）：面试官 prompt 此前**一个字都没提**候选人的话是从语音转写来的。
ADR-0009 把「下游模型本就能读通」当成不设确认环节的理由，而那只在**错得读不通**时
成立 —— 这里造的正是**读得通**的那种错：`内存屏障` → `内存平障`、`向量化` → `像量化`、
`rerank` → `瑞兰克`。句子照样通顺，所以怕的是模型照错字判成概念错误。

它只回答一件事：**把那句转写说明塞进 prompt，命中判定会不会翻过来。**

「改前」不是"把槽留空"——那还会留着"判定前先读"的小标题与规则 8，是**不干净的对照**。
它取自 git：`--rev`（默认 `HEAD~1`）那个版本的**真 prompt 文件**，一个字都没动过。

量是"几条考察点判成命中"，每个格子跑 `--runs` 次（模型是随机的，一次不算数）。

用法：
    python tools/asr_tolerance_probe.py                 # 3 个用例 × 各 3 次
    python tools/asr_tolerance_probe.py --runs 5 --rev HEAD~2
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROMPT_REL = "interviewer/score_round.md"  # `prompts.load()` 要的是 prompts/ 下的相对路径
PROMPT_GIT_PATH = f"prompts/{PROMPT_REL}"

#: 用例：题干与考察点用**正确写法**（题目本来就是文字，不过 STT），
#: 回答则是"术语被 STT 写错、但句子读得通"的形状。每条考察点都压在一个错字上。
CASES: list[tuple[str, str, list[str], str]] = [
    (
        "volatile（同音错字）",
        "说说 volatile 关键字：它保证了什么、底层怎么做到的、又保证不了什么？",
        [
            "可见性靠内存屏障实现（会刷新写缓冲、让别的核看到）",
            "底层是 lock 前缀指令，会导致其他核的 cache line 失效（MESI 协议）",
            "volatile 不保证原子性（i++ 仍不安全）",
        ],
        "volatile 保证可见性靠的是内存平障，底层是那个 lock 前缀指令，"
        "会让其他核的凯什行失效，走麦西协议；它不保证原子性，所以 i++ 这种还是得加锁。",
    ),
    (
        "RAG（英文术语被音译）",
        "你们的 RAG 检索链路是怎么搭的？召回质量怎么保证？",
        [
            "文档切块后用嵌入模型做向量化，存进向量库",
            "检索 Top-K 之后把内容拼进 prompt 作为上下文",
            "用重排序（rerank）或关键词+向量混合检索提升召回质量",
        ],
        "我们把文档切块之后用嵌入模型做像量化，存到向量库里；检索 top k 之后"
        "把内容拼进普隆普特当上下文；后面还接了一层瑞兰克。",
    ),
    (
        "索引（术语被换成同音词）",
        "MySQL 里哈希索引和 B+ 树索引有什么区别？什么时候用哪个？",
        [
            "哈希索引只支持等值查询，不支持范围查询与排序",
            "B+ 树索引支持范围查询与最左前缀匹配",
            "二级索引查非索引列要回表（或走覆盖索引避免）",
        ],
        "哈西索引只能等值查，范围查和排序都不行；B加树可以范围查，还能走最左前缀；"
        "用二级索引查非索引列要回表，或者用覆盖索引避免。",
    ),
    (
        "红黑树（错字是个真词：书）",
        "HashMap 在 JDK 8 里为什么要用红黑树？和链表比取舍在哪？",
        [
            "链表过长（默认阈值 8）时树化，查找从 O(n) 降到 O(log n)",
            "红黑树是近似平衡的二叉搜索树，插入删除的旋转次数比 AVL 树少",
            "树节点占内存更大，所以元素少时仍用链表（退化到 6 时链化）",
        ],
        "链表超过八个就转成红黑书，查得更快，从 on 变成 o log n；红黑书是近似平衡的"
        "二叉搜索树，旋转比 avl 少；但树的节点更占内存，所以太少的时候还是用链表。",
    ),
    (
        "Transformer（英文术语被写成中文词）",
        "Transformer 的自注意力是怎么算的？为什么它能并行而 RNN 不行？",
        [
            "用 Q/K/V 三个投影，点积算注意力权重，再对 V 加权求和",
            "除以 sqrt(d_k) 防止点积过大导致 softmax 梯度消失",
            "所有位置一次矩阵乘法算完，没有时间步依赖，所以能并行",
        ],
        "传输形式的自注意力是用 qkv 三个投影，点积算权重再对 v 加权求和；"
        "还要除以根号 dk，不然 softmax 梯度会消失；所有位置一次矩阵乘法就算完了，"
        "没有时间步依赖，所以能并行。",
    ),
    (
        "闭包（错字是**另一个真概念**：背包）",
        "说说 JavaScript 的闭包：它是什么、为什么会形成、有什么用途和风险？",
        [
            "闭包是函数与其定义时词法环境的组合，内部函数能访问外部变量",
            "只要闭包还被引用，外部函数的变量就不会被回收（内存驻留）",
            "常见用途是封装私有状态或柯里化；风险是循环里用 var 会共享同一个变量",
        ],
        "背包就是一个函数加上它定义时候的那个词法环境，里面的函数能拿到外面的变量；"
        "只要背包还被引用着，外面那个函数的变量就不会被回收；"
        "平时用来封装私有状态，或者做颗粒化；风险是循环里用 var 的话大家共享一个变量。",
    ),
]


def _old_prompt(rev: str):
    """取那个版本的真 prompt 文件 —— 对照必须是**改动前的原文**，不是"槽留空"。"""
    from app.llm import Prompt

    raw = subprocess.run(
        ["git", "show", f"{rev}:{PROMPT_GIT_PATH}"],
        capture_output=True, cwd=ROOT, check=True,
    ).stdout.decode("utf-8")
    if "asr_note" in raw:
        raise SystemExit(f"{rev} 里已经有 asr_note 了 —— 换个更早的 --rev")
    return Prompt(name=f"{rev}:{PROMPT_GIT_PATH}", text=raw)


def main(argv: list[str] | None = None) -> int:
    from app.config import Settings
    from app.deps import get_llm
    from app.interview.service import _asr_note
    from app.llm import LLMError, prompts, split_prose_and_json

    parser = argparse.ArgumentParser(prog="python tools/asr_tolerance_probe.py")
    parser.add_argument("--runs", type=int, default=3, help="每个格子跑几次")
    parser.add_argument("--rev", default="HEAD~1", help="改前那一版 prompt 的 git 版本")
    parser.add_argument("--case", default="", help="只跑用例名里含这个字串的（空 = 全跑）")
    args = parser.parse_args(argv)

    old_template = _old_prompt(args.rev)
    new_template = prompts.load(PROMPT_REL)
    variants = (("改前", old_template), ("改后", new_template))
    cases = [c for c in CASES if args.case in c[0]]

    llm = get_llm(Settings())
    try:
        for name, stem, criteria, answer in cases:
            print(f"\n=== {name} ===\n题干：{stem}")
            criteria_block = "\n".join(f"{i}. {t}" for i, t in enumerate(criteria, start=1))
            for label, template in variants:
                hits_total = 0
                for run in range(args.runs):
                    prompt = template.render(
                        stem=stem,
                        criteria=criteria_block,
                        history="（这是第一轮）",
                        answer=answer,
                        asr_note=_asr_note("voice"),
                    )
                    try:
                        raw = llm.chat([{"role": "user", "content": prompt}]).text
                        prose, data = split_prose_and_json(raw)
                    except LLMError as e:
                        print(f"  {label} 第 {run + 1} 次：失败 {type(e).__name__}: {str(e)[:120]}")
                        continue
                    payload = data if isinstance(data, dict) else {}
                    hits = [
                        (int(h["criterion_id"]), str(h.get("status")))
                        for h in (payload.get("hits") or [])
                        if isinstance(h, dict) and h.get("criterion_id") is not None
                    ]
                    hit_count = sum(1 for _, s in hits if s == "命中")
                    hits_total += hit_count
                    misses = [cid for cid, s in hits if s == "未命中"]
                    first_line = prose.strip().splitlines()[0][:100] if prose.strip() else "（空）"
                    print(
                        f"  {label} 第 {run + 1} 次：命中 {hit_count}/{len(criteria)}"
                        f"｜未命中 {misses or '—'}｜面试官：{first_line}"
                    )
                print(f"  → {label} 平均命中 {hits_total / args.runs:.2f}/{len(criteria)}")
    finally:
        llm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
