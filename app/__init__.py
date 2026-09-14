"""DeepGrill v2 应用包。

包内按领域切分（ADR-0005 定概念、ADR-0010 定物理布局）：

    app/main.py      组装根：建 app、挂路由
    app/web/         接口层：按页面切文件，只调领域暴露的数据函数，不含业务规则
    app/<领域>/       interview · report · bank · knowledge · profile · account
    app/offline/     编排层：跨领域动作的固定去处 ＋ worker
    app/llm/ db/ config/ errors/    基础设施

包外（不在 `packages` 里，这是决策不是遗漏）：

    migrations/     纯 SQL 迁移 ＋ runner（ADR-0011）
    prompts/        纯数据，相对仓库根 open()（ADR-0007 / ADR-0010）
    tools/          一次性脚本，跑完即删
    scripts/        元工具（check_docs.py 等），被 pre-commit 依赖
"""
