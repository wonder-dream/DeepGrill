"""面试报告页（`/report/{id}`）—— 一个文件 = 一个 URL（ADR-0010）。

它**只读已落库的报告**（`interviews.report_body` / `report_summary`），不重算 ——
这是 ADR-0003 的直接后果：报告在 finish 时算好并落库，所以每次打开看到的是同一份。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db.models import Interview, User
from app.deps import get_current_user, get_session
from app.errors import NotFound
from app.report import service as report_service
from app.web.templating import render

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]
CurrentUserDep = Annotated[User | None, Depends(get_current_user)]


@router.get("/report/{interview_id}")
def report_page(
    request: Request, interview_id: int, session: SessionDep, user: CurrentUserDep
) -> object:
    if user is None:
        return RedirectResponse("/login", status_code=302)

    interview = session.get(Interview, interview_id)
    # 不属于自己的报告一律 404 —— 与"不存在"同一种回应，不泄露存在性
    if interview is None or interview.user_id != user.id:
        raise NotFound("这份报告不存在")

    report = report_service.get_report(session, interview)
    return render(request, "report.html", {"report": report, "interview": interview})
