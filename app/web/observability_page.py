"""观测页（`/admin/observability`）—— 决策 23 的「最小观测」。

决策 23 写明"**观测是三项里最重要的一项**"：没有它，前面所有"降级不静默"的设计
就只是"记下来了"，而没人看得到。

## 为什么它住在 `web/` 而不在 `offline/`

它要**同时看**多个领域的表（题库 / 知识层 / 账号 / 离线任务），而领域之间互不
import（ADR-0005）。所以它是跨领域的装配，属于页面层；指标怎么算在
`app/web/observability.py`，那是一个**只读的查询层**，不含业务规则。

（第一版把它放在 `offline/` —— 那等于让编排层去 import 它并不编排的领域
（`bank` / `knowledge` / `account`）。规则上勉强说得通但方向不对：`offline` 的职责
是"跑跨领域动作"，不是"给页面取数"。）

## 它只读、不调模型

观测页会被反复打开（排查时尤其），所以必须便宜。任何"顺手算一个需要模型的指标"
都会让它在**模型挂掉时不可用** —— 而它恰恰是你用来判断"是不是模型挂了"的那个页面。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import User
from app.deps import get_current_user, get_session, get_settings
from app.errors import Forbidden
from app.web import observability
from app.web.templating import render

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]
CurrentUserDep = Annotated[User | None, Depends(get_current_user)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _require_owner(user: User | None) -> User:
    if user is None:
        raise Forbidden("请先登录")
    if user.role != "owner":
        raise Forbidden("只有 owner 能看观测页")
    return user


@router.get("/admin/observability")
def observability_page(
    request: Request, session: SessionDep, user: CurrentUserDep, settings: SettingsDep
) -> object:
    owner = _require_owner(user)
    return render(
        request,
        "admin_observability.html",
        {
            "user": owner,
            "o": observability.collect(session, db_path=settings.resolved_database_path()),
            "human_bytes": observability.human_bytes,
        },
    )
