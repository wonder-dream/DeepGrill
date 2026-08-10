"""共享标签词表（Phase 2 前置，DESIGN §9）。

生成侧题目标签（tags）与判分侧薄弱点（weak_tags）共用本词表：
prompt 约束选择 + 代码层过滤（generate._validate_questions / judge._parse_judgment）。
单层主题词表：薄弱点=薄弱主题，可支撑 Phase 2 按 weak_tags 检索同类题复习。

标签按大类组织（TAG_CATEGORIES 为唯一数据源，TAG_VOCABULARY 由它展开），
前端筛选按分类联动（GET /api/tags 提供分类结构）。

2026-08-10 重构：后端基础拆分为语言分类（Java/Python/Go/前端）+ 领域分类
（数据库与中间件/分布式与高并发/基础设施）；删除上位词/题型词/近义冗余 10 个
（大模型/Agent框架/混合检索/强化学习/锁/IO/项目深挖/场景题/架构设计/高并发），
扩充语言专属标签 16 个（Java+5/Python+5/Go+6）。
"""

TAG_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Agent 生态", (
        "Agent",
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
    )),
    ("训练与对齐", (
        "预训练",
        "RLHF",
        "数据工程",
        "模型评估",
    )),
    ("Java", (
        "Java",
        "Spring",
        "SpringBoot",
        "MyBatis",
        "JVM",
        "JUC",
        "线程池",
        "类加载",
    )),
    ("Python", (
        "Python",
        "GIL",
        "asyncio",
        "FastAPI",
        "Django",
        "Flask",
        "装饰器",
        "生成器",
        "元类",
    )),
    ("Go", (
        "Go",
        "Goroutine",
        "Channel",
        "GMP",
        "GC",
        "Context",
        "Gin",
    )),
    ("C/C++", (
        "C++",
        "智能指针",
        "内存管理",
        "STL",
        "模板",
        "右值引用",
        "编译链接",
    )),
    ("前端", (
        "前端",
    )),
    ("数据库与中间件", (
        "MySQL",
        "Redis",
        "消息队列",
        "缓存",
        "数据库",
    )),
    ("分布式与高并发", (
        "微服务",
        "分布式",
        "并发",
    )),
    ("基础设施", (
        "操作系统",
        "Linux",
        "Docker",
        "K8s",
        "网络",
        "Git",
        "安全",
    )),
    ("通用", (
        "系统设计",
        "算法",
        "数据结构",
        "设计模式",
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
