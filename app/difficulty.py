"""难度分级（1-5）档位定义：生成 prompt / 判分 prompt / 存量迁移脚本共用。"""

DIFFICULTY_NAMES = {1: "入门", 2: "基础", 3: "进阶", 4: "深度", 5: "专家"}

DIFFICULTY_SCALE_TEXT = """难度按 1-5 级标注（必须输出 1-5 的整数），分级标准如下：

1 级 · 入门：单一基础概念，定义或原理一句话可答，背过即会
   （例：HashMap 是什么？HTTP 状态码有哪些？）
2 级 · 基础：常见高频题，需理解原理并展开 2-3 个要点，有追问空间
   （例：进程和线程的区别？讲讲 HashMap 底层原理）
3 级 · 进阶：原理 + 场景结合，需讲清边界条件与权衡，涉及多知识点串讲
   （例：Redis 缓存一致性怎么保证？MySQL 为什么用 B+ 树索引？）
4 级 · 深度：综合系统设计/多组件协同/方案选型，需实战经验支撑
   （例：设计一个短链接系统？分布式事务怎么选型？）
5 级 · 专家：源码级/极致性能/容错细节，没有真实踩坑难以答好
   （例：ConcurrentHashMap 扩容细节？设计高并发秒杀系统并说清全链路取舍？）

判定维度：广度（单点知识点 → 多系统协同）、深度（表面记忆 → 源码/机制级）、
权衡（无 → 方案选型 → 容量/一致性/性能量化取舍）、经验依赖（背题可答 → 需要真实踩坑）。"""


def target_level_for(difficulty: int) -> int:
    """追问目标深度（L1-L5）：难度 1-4 分别到 L2-L5，难度 5 全链到 L5。"""
    return min(5, difficulty + 1)


PROBE_TIERS = {1: "light", 2: "light", 3: "medium", 4: "deep", 5: "deep"}


def probe_tier_for(difficulty: int) -> str:
    """追问档位：1-2 浅挖（回答完整清晰即收尾，最多轻拓展一问）/
    3 中挖（追到权衡/边界 L4 即收）/
    4-5 深挖（逐层深挖到目标深度 L5，达标即收）。"""
    return PROBE_TIERS.get(difficulty, "deep")


def max_rounds_for(difficulty: int, config_max: int = 20) -> int:
    """追问轮数上限按档位收紧（低难度题浅挖少问）：light 4 / medium 8 / deep 12-15。"""
    cap = {1: 4, 2: 4, 3: 8, 4: 12, 5: 15}.get(difficulty, 15)
    return min(config_max, cap)
