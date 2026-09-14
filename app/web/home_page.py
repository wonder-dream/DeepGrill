"""首页（`/`）—— 一个文件 = 一个 URL（ADR-0010）。

它的职责只有两件：**取数、交给模板**。判断与计算一律不在这里 ——
`web/` 是唯一 import 一切、因而唯一无法被隔离测试的模块（CONTEXT.md），
所以它一旦持有业务规则，那条规则就再也测不到。

MVP 阶段本页只做"能跑起来"的证明：库连得上吗、有多少题、多少知识点。
真正的推荐中心（今日推荐 / 私有题集 / 继续未完成会话）等各自的领域有了
再往这里加 —— 那时它是"多调几个领域的装配函数"，不是"往这个函数里再写一段"。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.account import service as account
from app.config import Settings
from app.db.models import User
from app.deps import get_current_user, get_session, get_settings
from app.web.templating import render

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
CurrentUserDep = Annotated[User | None, Depends(get_current_user)]


def _count(session: Session, table: str) -> int | None:
    """数一张表的行数；表不存在时返回 None。

    MVP 阶段库可能是空的、甚至没跑过迁移，而**首页不该因此 500** —— 它是用户
    看到的第一个页面。所以这里把"没迁移"当成一种**能显示的状态**，而不是异常：
    页面上直接告诉用户该跑哪条命令（ADR-0010：诊断信息要指向那一条命令）。
    """
    try:
        return int(session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
    except SQLAlchemyError:
        session.rollback()
        return None


@router.get("/")
def home(
    request: Request, session: SessionDep, settings: SettingsDep, user: CurrentUserDep
) -> object:
    question_count = _count(session, "questions")
    point_count = _count(session, "knowledge_points")

    if question_count is None:
        path = settings.resolved_database_path()
        status = f"未初始化（{path}）—— 先跑 python -m migrations.run"
    else:
        status = "已连接"

    return render(
        request,
        "home.html",
        {
            "database_status": status,
            "question_count": question_count or 0,
            "point_count": point_count or 0,
            # 额度是首页必须显示的一件事：它决定"主按钮按下去会发生什么"（决策 13）。
            # 匿名访客没有额度这回事，所以是 None。
            "quota": account.quota_state(session, user.id) if user else None,
        },
    )


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """存活探针。它**不查库** —— 用途是"进程还在吗"，与"库通不通"分开。"""
    return {"status": "ok"}
