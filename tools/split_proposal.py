"""把一份大提案拆成几批（每批一个领域），供人审分次过。

## 为什么不用嵌入分组（第一版就是这么做的，失败了）

先按"名字 + 定义"的嵌入做了最远点 + 最近种子分组，结果**一批吃掉 111/171 个点**：
这些名字全是同一片语义邻域里的短中文短语（互相余弦 P50 就有 0.74），最近种子
分不开主题 —— 与"聚类阈值 0.86 悬在分布之外"是同一个现象的两面（短文本的余弦整体偏高）。

## 所以改用**词法规则**

主题边界在这批名字里是**关键词**级的（RAG / 智能体 / 大模型 / 后端系统），而人审时
每批的领域名还能改 —— 规则分错一两个点的代价很小，而它的好处是**可解释、可复现、
零调用**。规则按顺序匹配，第一个命中即算。

用法：
    python tools/split_proposal.py                      # 看分组（只打印）
    python tools/split_proposal.py --write              # 写 data/review/batch-*.json
    python tools/split_proposal.py --activate 2         # 把第 2 批换成当前待审提案
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROPOSAL = ROOT / "data" / "knowledge_proposal.json"
OUT_DIR = ROOT / "data" / "review"

#: (领域名, 关键词) —— **按顺序匹配，第一个命中即算**。
#:
#: 关键词不是拍的：先数了一遍语料（`tools/probe_axes.py`），按"有多少道题命中"定轴 ——
#: Agent 35% / 大模型 11% / 系统设计 11% / RAG 10% / Java·JVM 3% / 数据库 3% /
#: 算法 2% / 前端 2% / Go 1% / **Python 0.3%（10 道题，成不了一个领域）**。
#: 顺序把**具体的栈词放前面**（栈词比主题词更特有），最后才是兜底。
RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Java / JVM 后端", ("java", "jvm", "gc", "spring", "mybatis", "jdk", "字节码", "类加载",
                        "垃圾回收")),
    # ⚠️ 用户明确要求：Python / Go **不能并进 JVM**，要单独成"后端语言"这一块
    ("后端语言（Python / Go）", ("python", "golang", " go ", "goroutine", "django", "flask",
                              "asyncio", "装饰器", "语言机制", "面向对象")),
    ("数据库与存储", ("mysql", "redis", "索引", "事务", "分库分表", "sql", "postgres",
                    "mongo", "存储引擎", "缓存")),
    ("检索与 RAG", ("rag", "检索", "召回", "向量", "分块", "rerank", "重排", "嵌入",
                  "知识图谱")),
    # ⚠️ 用户明确要求：**底层模型的训练与微调**要和"大模型应用"分开 —— 这一档放前面，
    #    因为它比"应用"更具体（先命中的赢）
    ("大模型算法（训练与微调）", ("预训练", "微调", "对齐", "rlhf", "注意力", "transformer",
                              "量化", "蒸馏", "损失", "梯度", "归一化", "激活", "数值稳定",
                              "优化器", "预训练数据", "模型架构", "自监督", "sft", "lora")),
    ("大模型应用工程", ("大模型", "llm", "模型", "评测", "评估", "推理", "提示", "prompt",
                    "少样本", "配置", "回退", "工具层", "幻觉", "输出", "服务", "成本",
                    "token", "解码", "词表", "多模态")),
    ("智能体与编排", ("agent", "智能体", "工具调用", "多智能体", "编排", "工作流", "记忆",
                    "规划", "上下文")),
    ("系统设计与架构", ("架构", "分布式", "高并发", "限流", "网关", "稳定性", "可用性",
                      "微服务", "幂等", "熔断", "降级", "性能", "网络", "协议")),
    ("算法与数据结构", ("算法", "红黑树", "动态规划", "排序", "a*", "dfs", "bfs",
                      "dijkstra", "图论", "数据结构")),
    ("前端", ("javascript", "vue", "react", "css", "html", "浏览器")),
    # ⚠️ 用户看过剩下 43 个兜底点之后定的两条：**AI 工程实践与工具链单独成块**，
    #    其余（无人系统 / 具身 / 视频 / 端侧 + 少量散点）全部并进「领域应用」。
    ("AI 工程实践与工具链", ("ai辅助", "claude code", "harness", "技能", "编码规则", "沙箱",
                          "sandbox", "工程实践", "失败资产", "创建者验证", "全生命周期",
                          "编程系统")),
]


def classify(name: str) -> str:
    for domain, keywords in RULES:
        if any(keyword.lower() in name.lower() for keyword in keywords):
            return domain
    # 兜底：用户明确要求"剩下的就合并成一个领域应用"（不再叫「其他（待定）」）
    return "领域应用"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/split_proposal.py")
    parser.add_argument("--write", action="store_true", help="写文件（默认只看）")
    parser.add_argument("--activate", type=int, default=0, help="把第 N 批换成当前待审提案")
    args = parser.parse_args(argv)

    if args.activate:
        source = OUT_DIR / f"batch-{args.activate}.json"
        if not source.exists():
            print(f"没有 {source}")
            return 2
        if PROPOSAL.exists() and not (PROPOSAL.parent / "knowledge_proposal.applied.json").exists():
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PROPOSAL, OUT_DIR / "replaced.json")
            print(f"（原来的待审提案另存了一份：{OUT_DIR / 'replaced.json'}）")
        payload = json.loads(source.read_text(encoding="utf-8"))
        PROPOSAL.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已换成第 {args.activate} 批：{len(payload.get('candidates') or [])} 条候选、"
              f"领域名「{payload.get('domain')}」——现在打开 /admin/review 审这一批")
        return 0

    payload = json.loads(PROPOSAL.read_text(encoding="utf-8"))
    candidates = payload.get("candidates") or []
    buckets: dict[str, list[dict]] = {}
    for cand in candidates:
        buckets.setdefault(classify(str(cand.get("name") or "")), []).append(cand)

    ordered = sorted(buckets.items(), key=lambda item: -len(item[1]))
    for domain, group in ordered:
        names = [str(c.get("name")) for c in group]
        cover = sum(len(c.get("question_ids") or []) for c in group)
        print(f"\n【{domain}】{len(group)} 个点、覆盖 {cover} 道题")
        print(f"  {'、'.join(names[:10])}{' …' if len(names) > 10 else ''}")

    if not args.write:
        print("\n（只看不写。要写文件加 --write）")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for order, (domain, group) in enumerate(ordered, start=1):
        target = OUT_DIR / f"batch-{order}.json"
        target.write_text(
            json.dumps({"domain": domain, "candidates": group}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  → {target}")
    print("\n审第 N 批：python tools/split_proposal.py --activate N")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
