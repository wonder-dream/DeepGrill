"""知识层人审页（决策 47：**一次性最小审核页**）。

决策 47 定的形态很具体：列候选簇与成员，逐条「通过 / 否决 / 合并」，批量提交；
**不需要权限体系、分页、搜索**（几百条一次加载完）。这一页就按那个形态做，
只加一件事：它只给 owner 看（装配是全局动作，不是个人动作）。

## 提案从哪来

`python -m app.cli propose` 跑一次管道，把候选落成
`data/knowledge_proposal.json`（**跨进程**：人审可能隔天做，而且这是另一个请求）。
审核页读它、渲染表单；提交时按决定落库。

提案文件天然会过期（题变了就该重跑），所以**不进库当业务数据** —— 它是一次装配的
中间产物，和 `tools/` 里的脚本同一性质。
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import REPO_ROOT
from app.db.models import User
from app.deps import get_current_user, get_session
from app.errors import Forbidden
from app.offline import knowledge_pipeline as kp
from app.web.templating import render

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]
CurrentUserDep = Annotated[User | None, Depends(get_current_user)]

#: 提案文件的位置。`data/` 是运行期产物目录（不进库）。
PROPOSAL_PATH = REPO_ROOT / "data" / "knowledge_proposal.json"


def _require_owner(user: User | None) -> User:
    if user is None:
        raise Forbidden("请先登录")
    if user.role != "owner":
        raise Forbidden("这一步只有 owner 能做（它是全局装配，不是个人动作）")
    return user


def load_proposal() -> kp.ProposalResult | None:
    if not PROPOSAL_PATH.is_file():
        return None
    try:
        return kp.ProposalResult.from_json(json.loads(PROPOSAL_PATH.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        # 文件坏了就当成"没有提案" —— 重新跑一次 propose 即可，不必让页面 500
        return None


@router.get("/admin/review")
def review_page(request: Request, user: CurrentUserDep) -> object:
    owner = _require_owner(user)
    proposal = load_proposal()
    return render(
        request,
        "admin_review.html",
        {
            "user": owner,
            "proposal": proposal,
            "path": str(PROPOSAL_PATH.relative_to(REPO_ROOT)),
        },
    )


@router.post("/admin/review/apply")
async def apply_review(request: Request, session: SessionDep, user: CurrentUserDep) -> object:
    """批量提交人的决定。

    表单是**动态条数**的，所以这里收原始表单而不是逐个声明参数 ——
    FastAPI 的 `Form(...)` 需要固定参数名，而候选数量由提案文件决定。
    """
    owner = _require_owner(user)
    proposal = load_proposal()
    if proposal is None or not proposal.candidates:
        raise Forbidden("没有可审的提案 —— 先跑一次 python -m app.cli propose")

    form = await request.form()
    domain_name = str(form.get("domain_name") or "").strip() or "未命名领域"

    decisions: list[kp.Decision] = []
    for index, cand in enumerate(proposal.candidates):
        action = str(form.get(f"action-{index}") or "reject")
        if action == "approve":
            name = str(form.get(f"name-{index}") or cand.name).strip()
            decisions.append(kp.Decision(index, "approve", name=name))
        elif action == "merge":
            # 合并目标用下拉框，只有一个值（其它候选的下标）；空 = 不合并
            target = str(form.get(f"merge-{index}") or "")
            if target.isdigit():
                decisions.append(kp.Decision(index, "merge_into", merge_into=int(target)))
            else:
                decisions.append(kp.Decision(index, "reject"))
        else:
            decisions.append(kp.Decision(index, "reject"))

    result = kp.apply_review(
        session, candidates=proposal.candidates, decisions=decisions, domain_name=domain_name
    )
    session.commit()

    # 审完就把提案挪走：留着它会让"再审一次"重复建点（虽然 _commit_point 幂等，
    # 但状态会变得难以解释）。改名而不是删除 —— 保留这次的痕迹，便于复盘。
    try:
        PROPOSAL_PATH.replace(PROPOSAL_PATH.with_suffix(".applied.json"))
    except OSError:
        pass

    return render(
        request,
        "admin_review_done.html",
        {
            "user": owner,
            "result": result,
            "created": result.created_points,
            "skipped": result.skipped,
        },
    )
