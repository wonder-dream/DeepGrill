"""共享标签词表（岗位 × 语言维度，2026-08-11 重建）。

岗位（roles）：frontend/backend/ai_app/qa/ai_infra，分类 roles 为空 = 全岗位通用。
语言（lang）：仅 backend 岗位使用（java/python/go/C/C++），其余分类为 None。
题岗位集 = 该题全部标签所在分类 roles 的并集（通用分类不贡献岗位）。
用户 focus（单选岗位）+ focus_lang（仅 backend）只影响推荐排序，不阻断做题。

词表数据库化：代码定义种子（TAG_CATEGORIES），init_db 后调用 reload_tags()
从 DB（tag_categories/tag_items）重建；管理接口（增删分类/标签）后同样调用 reload_tags()。
"""

ROLE_FRONTEND = "frontend"
ROLE_BACKEND = "backend"
ROLE_AI_APP = "ai_app"
ROLE_QA = "qa"
ROLE_AI_INFRA = "ai_infra"

LANG_JAVA = "java"
LANG_PYTHON = "python"
LANG_GO = "go"
LANG_CPP = "C/C++"

# (分类名, lang, roles, 标签)
_SEED_CATEGORIES: tuple[tuple[str, str | None, tuple[str, ...], tuple[str, ...]], ...] = (
    ("LLM 核心", None, (ROLE_AI_APP, ROLE_AI_INFRA), (
        "Transformer",
        "Attention",
        "Tokenizer",
        "KV Cache",
        "推理优化",
        "微调",
        "LoRA",
        "量化",
        "Prompt工程",
        "上下文工程",
        "流式",
    )),
    ("Agent 生态", None, (ROLE_AI_APP,), (
        "Agent",
        "Multi-Agent",
        "ReAct",
        "Function Calling",
        "Tool Use",
        "MCP",
        "LangChain",
        "LangGraph",
    )),
    ("RAG 与检索", None, (ROLE_AI_APP,), (
        "RAG",
        "Embedding",
        "向量数据库",
        "检索",
        "重排序",
        "文档问答",
    )),
    ("训练与对齐", None, (ROLE_AI_INFRA,), (
        "预训练",
        "RLHF",
        "数据工程",
        "模型评估",
    )),
    ("AI 基础设施", None, (ROLE_AI_INFRA,), (
        "GPU/CUDA",
        "推理部署",
        "vLLM/Triton",
        "分布式训练",
        "并行策略",
        "DeepSpeed/Megatron",
        "MLOps",
        "推理性能优化",
    )),
    ("Java", LANG_JAVA, (ROLE_BACKEND,), (
        "Java",
        "Spring",
        "SpringBoot",
        "MyBatis",
        "JVM",
        "JUC",
        "线程池",
        "类加载",
    )),
    ("Python", LANG_PYTHON, (ROLE_BACKEND, ROLE_AI_APP), (
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
    ("Go", LANG_GO, (ROLE_BACKEND,), (
        "Go",
        "Goroutine",
        "Channel",
        "GMP",
        "GC",
        "Context",
        "Gin",
    )),
    ("C/C++", LANG_CPP, (ROLE_BACKEND,), (
        "C++",
        "智能指针",
        "内存管理",
        "STL",
        "模板",
        "右值引用",
        "编译链接",
    )),
    ("数据库", None, (ROLE_BACKEND, ROLE_AI_APP, ROLE_QA), (
        "MySQL",
        "Redis",
        "索引",
        "事务",
        "分库分表",
        "消息队列",
    )),
    ("分布式与高并发", None, (ROLE_BACKEND, ROLE_AI_APP), (
        "微服务",
        "分布式",
        "一致性",
        "分布式事务",
        "分布式锁",
        "限流",
        "熔断降级",
        "负载均衡",
    )),
    ("操作系统与网络", None, (), (
        "操作系统",
        "进程与线程",
        "虚拟内存",
        "网络",
        "TCP/IP",
        "HTTP",
        "Linux",
    )),
    ("基础设施", None, (ROLE_BACKEND, ROLE_AI_INFRA, ROLE_QA), (
        "Docker",
        "K8s",
        "Git",
        "CI/CD",
        "监控",
        "部署",
    )),
    ("前端", None, (ROLE_FRONTEND,), (
        "JavaScript",
        "TypeScript",
        "CSS",
        "浏览器原理",
        "Vue",
        "React",
        "Node.js",
        "前端工程化",
        "前端性能优化",
        "网络安全",
    )),
    ("测试开发", None, (ROLE_QA,), (
        "测试理论基础",
        "接口测试",
        "自动化测试",
        "性能测试",
        "测试流程与质量保障",
    )),
    ("通用", None, (), (
        "系统设计",
        "算法",
        "数据结构",
        "设计模式",
    )),
)

# 运行期常量（可变容器：reload_tags() 原地更新，保持模块级旧引用自动同步）
TAG_CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    (name, tags) for name, _lang, _roles, tags in _SEED_CATEGORIES
]
TAG_VOCABULARY: list[str] = [
    tag for _name, tags in TAG_CATEGORIES for tag in tags
]
CATEGORY_LANG: dict[str, str | None] = {
    name: lang for name, lang, _roles, _tags in _SEED_CATEGORIES
}
CATEGORY_ROLES: dict[str, tuple[str, ...]] = {
    name: roles for name, _lang, roles, _tags in _SEED_CATEGORIES
}

MAX_TAGS = 5  # 生成侧单题标签上限
MAX_WEAK_TAGS = 3  # 判分侧薄弱点标签上限


def reload_tags() -> None:
    """从 DB（tag_categories/tags）重建词表常量；DB 无数据则写入种子。

    init_db 后与管理接口（增删分类/标签）后调用。
    """
    global TAG_CATEGORIES, TAG_VOCABULARY, CATEGORY_LANG, CATEGORY_ROLES
    from sqlalchemy import select

    from .db import get_session
    from .models import Tag, TagCategory

    with get_session() as session:
        cats = list(session.scalars(select(TagCategory).order_by(TagCategory.id)))
        items = list(session.scalars(select(Tag)))
        if not cats:
            # 首次：写入种子（代码默认）
            from .db import commit

            for name, lang, roles, tags in _SEED_CATEGORIES:
                cat = TagCategory(name=name, is_custom=0, roles=list(roles), lang=lang)
                session.add(cat)
                commit(session)
                session.refresh(cat)
                for t in tags:
                    session.add(Tag(category_id=cat.id, name=t, is_custom=0))
            commit(session)
            cats = list(session.scalars(select(TagCategory).order_by(TagCategory.id)))
            items = list(session.scalars(select(Tag)))
    by_cat: dict[int, list[str]] = {}
    lang_by_cat: dict[int, str | None] = {}
    roles_by_cat: dict[int, tuple[str, ...]] = {}
    for it in items:
        by_cat.setdefault(it.category_id, []).append(it.name)
    for c in cats:
        lang_by_cat[c.id] = c.lang
        roles_by_cat[c.id] = tuple(c.roles or ())
    TAG_CATEGORIES[:] = [
        (c.name, tuple(by_cat.get(c.id, ())))
        for c in cats
    ]
    TAG_VOCABULARY[:] = [
        tag for _name, tags in TAG_CATEGORIES for tag in tags
    ]
    CATEGORY_LANG.clear()
    CATEGORY_LANG.update({c.name: lang_by_cat.get(c.id) for c in cats})
    CATEGORY_ROLES.clear()
    CATEGORY_ROLES.update({c.name: roles_by_cat.get(c.id, ()) for c in cats})


def tag_vocab_text() -> str:
    """词表渲染为 prompt 片段。"""
    return "\n".join(f"- {t}" for t in TAG_VOCABULARY)
