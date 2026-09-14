"""首页（`/`）—— 一个文件 = 一个 URL（ADR-0010）。

它是**内容推荐中心**（决策 3）：开始模拟面试（主按钮）+ 今日推荐题 + 我的私有
题集 + 继续未完成会话。**不做学习路径状态机** —— 用户随时可能只想刷两道题。

它的职责只有两件：**取数、交给模板**。判断与计算一律不在这里 ——
`web/` 是唯一 import 一切、因而唯一无法被隔离测试的模块（CONTEXT.md），
所以它一旦持有业务规则，那条规则就再也测不到。于是这一页做的是**拼装**：

```
掌握度薄弱点   knowledge.mastery_matrix(...).weak_points()     ← knowledge 领域算
推荐题         bank.recommend(weak_point_ids=..., exclude_ids=...) ← bank 领域挑
答过哪些题     interview.answered_question_ids(...)            ← interview 领域答
未完成会话     interview.active_interviews(...)                ← interview 领域给
私有题 / 收藏  bank / favorites 的计数
```

⚠️ **「今日推荐题」是即时计算，不是每日锁定的题单**：它没有自己的表，也不跨天
固化（基线里点明了 `user_picks` / `questions.selected_at` 都已删掉）。换成别的推荐
算法时它不会留数据残骸 —— 这也是为什么这一段拼装代码可以放心改。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.account import service as account
from app.bank import favorites as favorites_service
from app.bank import repository as bank_repository
from app.bank import service as bank_service
from app.config import Settings
from app.db.models import User
from app.deps import get_current_user, get_session, get_settings
from app.interview import service as interview
from app.knowledge import service as knowledge
from app.web.templating import render

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
CurrentUserDep = Annotated[User | None, Depends(get_current_user)]


def _count(session: Session, table: str) -> int | None:
    """数一张表的行数；表不存在时返回 None。

    库可能是空的、甚至没跑过迁移，而**首页不该因此 500** —— 它是用户看到的第一个
    页面。所以这里把"没迁移"当成一种**能显示的状态**，而不是异常：页面上直接告诉
    用户该跑哪条命令（ADR-0010：诊断信息要指向那一条命令）。
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

    context: dict[str, object] = {
        "database_status": status,
        "question_count": question_count or 0,
        "point_count": point_count or 0,
        # 额度是首页必须显示的一件事：它决定"主按钮按下去会发生什么"（决策 13）。
        # 匿名访客没有额度这回事，所以是 None。
        "quota": account.quota_state(session, user.id) if user else None,
    }

    if user is not None:
        context.update(_for_member(session, user))

    return render(request, "home.html", context)


def _for_member(session: Session, user: User) -> dict[str, object]:
    """登录用户才有的那半页。**拼装，不判断**（规则都在各自的领域里）。"""
    matrix = knowledge.mastery_matrix(session, user.id)
    weak = matrix.weak_points(limit=5)
    answered = interview.answered_question_ids(session, user.id)

    recommended = bank_service.recommend(
        session,
        user.id,
        weak_point_ids=[c.point_id for c in weak],
        exclude_ids=answered,
    )
    # 兜底也要**说清楚自己是兜底**：还没有测出薄弱点时（新用户、或全答对了），
    # 摆一个看起来很像推荐的列表会让人以为那是有依据的。
    fallback = False
    if not recommended and not weak:
        recommended = bank_service.latest(session, user.id)
        fallback = bool(recommended)

    active = []
    for interview_row in interview.active_interviews(session, user.id):
        ts = interview.next_active_session(session, interview_row.id)
        if ts is not None:
            active.append({"interview": interview_row, "session_id": ts.id})

    return {
        "weak_points": weak,
        "recommended": recommended,
        "recommend_fallback": fallback,
        "active": active,
        "private_count": len(bank_repository.owned_ids(session, user.id)),
        "favorite_count": favorites_service.count(session, user_id=user.id),
    }


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """存活探针。它**不查库** —— 用途是"进程还在吗"，与"库通不通"分开。"""
    return {"status": "ok"}
