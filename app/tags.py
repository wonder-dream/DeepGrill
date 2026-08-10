"""共享标签词表（Phase 2 前置，DESIGN §9）。

生成侧题目标签（tags）与判分侧薄弱点（weak_tags）共用本词表：
prompt 约束选择 + 代码层过滤（generate._validate_questions / judge._parse_judgment）。
单层主题词表：薄弱点=薄弱主题，可支撑 Phase 2 按 weak_tags 检索同类题复习。

标签按大类组织（TAG_CATEGORIES 为唯一数据源，TAG_VOCABULARY 由它展开），
前端筛选按分类联动（GET /api/tags 提供分类结构）。
"""

TAG_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Agent 生态", (
        "Agent",
        "Agent框架",
        "Multi-Agent",
        "ReAct",
        "Function Calling",
        "Tool Use",
        "MCP",
        "LangChain",
        "LangGraph",
    )),
    ("LLM 核心", (
        "Transformer",
        "Attention",
        "Tokenizer",
        "大模型",
        "Prompt工程",
        "上下文工程",
        "微调",
        "LoRA",
        "量化",
        "KV Cache",
        "推理优化",
        "流式",
    )),
    ("RAG 与检索", (
        "RAG",
        "Embedding",
        "向量数据库",
        "检索",
        "重排序",
        "文档问答",
        "混合检索",
    )),
    ("训练与对齐", (
        "预训练",
        "RLHF",
        "强化学习",
        "数据工程",
        "模型评估",
    )),
    ("后端基础", (
        "Java",
        "Spring",
        "MySQL",
        "Redis",
        "消息队列",
        "微服务",
        "分布式",
        "高并发",
        "JVM",
        "网络",
        "缓存",
        "并发",
        "锁",
        "IO",
        "Python",
        "GIL",
        "asyncio",
        "FastAPI",
        "Go",
        "操作系统",
        "Linux",
        "Docker",
        "K8s",
        "Git",
        "数据库",
        "安全",
    )),
    ("通用", (
        "系统设计",
        "架构设计",
        "算法",
        "数据结构",
        "设计模式",
        "项目深挖",
        "场景题",
        "前端",
        "测试",
    )),
)

TAG_VOCABULARY: tuple[str, ...] = tuple(
    tag for _name, tags in TAG_CATEGORIES for tag in tags
)

MAX_TAGS = 5  # 生成侧单题标签上限
MAX_WEAK_TAGS = 3  # 判分侧薄弱点标签上限


def tag_vocab_text() -> str:
    """词表渲染为 prompt 片段。"""
    return "\n".join(f"- {t}" for t in TAG_VOCABULARY)
