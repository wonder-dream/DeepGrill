"""M14 Web 路由层：请求编排与校验，业务委托下层模块；LLM 调用放后台任务 + 前端轮询。

- AppError → 500 统一 JSON；未知异常 → 500 + 日志
- 并发作答同一会话 → 409（in-flight 集合）
- 判分失败：结果接口返回 status="failed"，可再次作答重试
"""
import base64
import hashlib
import logging
import os
import re
import secrets
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import cast, delete, exists, func, or_, select, String, text

from .. import db
from ..auth import (
    MAX_USERS,
    create_token,
    hash_password,
    revoke_token,
    user_count,
    user_from_token,
    validate_password,
    verify_password,
)
from ..config import AppConfig, secret_value
from ..difficulty import max_rounds_for, target_level_for
from ..embed import Embedder
from ..errors import AppError, DuplicateSource
from ..judge.chain import resume
from ..parsers import extract_text
from ..judge.judge import STATUS_FAILED, judge
from ..pipeline.generate import _clamp_difficulty
from ..ratelimit import allow as _rl_allow
from ..ratelimit import check as _rl_check
from ..ratelimit import fail as _rl_fail
from ..ratelimit import success as _rl_success
from ..models import (
    Attempt,
    Judgment,
    Question,
    QuestionType,
    Session,
    SessionKind,
    SessionStatus,
    Source,
    SourceType,
    User,
    UserFavorite,
    UserPick,
    Submission,
    SubmissionKind,
    SubmissionStatus,
    QuestionFeedback,
    FeedbackCategory,
    FeedbackStatus,
)
from ..tags import TAG_CATEGORIES, TAG_VOCABULARY
from .schemas import (AdminQuestionUpdate, AnswerBody, CreateSessionBody, FeedbackBody, SubmitBody, UploadBody)
from . import static_dir

logger = logging.getLogger(__name__)

_shared_embedder = None
_shared_embedder_lock = threading.Lock()


def _default_embedder_factory():
    """默认 embedder 工厂：进程内共享单例（lazy 加载）。

    并发答题/上传共用一份 bge-m3（~2.3GB），避免每次判分参考检索都
    新建实例导致 4C8G 内存打满、服务卡死（代理层表现为 502）。
    """

    def factory() -> Embedder:
        global _shared_embedder
        if _shared_embedder is None:
            with _shared_embedder_lock:  # 双检锁：并发首波请求只建一份
                if _shared_embedder is None:
                    _shared_embedder = Embedder()
        return _shared_embedder

    return factory


def require_user(authorization: str = Header(default="")) -> User:
    """认证依赖：Bearer token → 当前用户；无效 401。"""
    token = authorization.removeprefix("Bearer ").strip()
    user = user_from_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return user


def require_owner(user: User = Depends(require_user)) -> User:
    """管理员依赖：role=owner 才可用（上传/题库管理/流水线）。"""
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="仅管理员可用")
    return user


def _validate_focus(focus, focus_lang) -> tuple[str | None, str | None]:
    """校验岗位/语言方向：focus 为空则清空；focus_lang 仅 backend 有效，其他岗位忽略。"""
    from ..tags import LANG_CPP, LANG_GO, LANG_JAVA, LANG_PYTHON, ROLE_BACKEND

    if focus is None or str(focus).strip() == "":
        return None, None
    focus = str(focus).strip()
    valid_roles = {ROLE_BACKEND, "frontend", "ai_app", "qa", "ai_infra"}
    if focus not in valid_roles:
        raise HTTPException(status_code=400, detail=f"invalid focus: {focus}")
    if focus != ROLE_BACKEND:
        return focus, None  # 非后端岗位忽略语言方向
    if focus_lang is None or str(focus_lang).strip() == "":
        return focus, None
    focus_lang = str(focus_lang).strip()
    valid_langs = {LANG_JAVA, LANG_PYTHON, LANG_GO, LANG_CPP}
    if focus_lang not in valid_langs:
        raise HTTPException(status_code=400, detail=f"invalid focus_lang: {focus_lang}")
    return focus, focus_lang


def _ensure_today_picks(session, user_id: int) -> list[UserPick]:
    """今日选题懒加载：用户今天无 picks 时按 D8 配额个性化选题（每用户池 + 薄弱点加权，按天隔离）。"""
    from datetime import datetime as _dt

    today_start = _dt.now().replace(hour=0, minute=0, second=0, microsecond=0)
    existing = list(
        session.scalars(
            select(UserPick).where(
                UserPick.user_id == user_id,
                UserPick.picked_at >= today_start,
            )
        )
    )
    if existing:
        return existing
    for qtype, limit in (
        (QuestionType.knowledge, 3),
        (QuestionType.design, 2),
        (QuestionType.project, 1),
    ):
        db.pick_questions(session, user_id, limit, qtype)
    return list(
        session.scalars(
            select(UserPick).where(
                UserPick.user_id == user_id,
                UserPick.picked_at >= today_start,
            )
        )
    )

_inflight: dict[int, float] = {}  # session_id → 入队时间戳（判分中；超时自动清理防永久 409）
_inflight_lock = threading.Lock()
_INFLIGHT_TTL = 600  # 判分中标记有效期（秒）：LLM 判分一般 <60s，超时视为进程已死，允许删除/重试


def _inflight_snapshot() -> set[int]:
    """锁内快照：清理超时标记后返回在途 session_id 集合。"""
    with _inflight_lock:
        now = time.time()
        expired = [sid for sid, ts in _inflight.items() if now - ts > _INFLIGHT_TTL]
        for sid in expired:
            _inflight.pop(sid, None)
        return set(_inflight)


def _question_inflight(session, question_id: int) -> bool:
    """该题是否存在判分中的会话（删除保护：判分中删题会让用户答案静默丢失）。"""
    inflight = _inflight_snapshot()
    if not inflight:
        return False
    sid = session.scalar(
        select(Session.id)
        .where(Session.id.in_(inflight), Session.question_id == question_id)
        .limit(1)
    )
    return sid is not None

_register_lock = threading.Lock()  # 注册串行：防并发注册双 owner / 超名额竞态（B4）

_resume_candidates: dict[str, dict] = {}
_resume_lock = threading.Lock()

_upload_tasks: dict[str, dict] = {}
_upload_lock = threading.Lock()

_parse_semaphore = threading.Semaphore(1)  # 二进制解析串行（MinerU 进程数 GB 内存，防并发 OOM）

_review_paper_cache: dict[str, dict] = {}
_review_paper_lock = threading.Lock()
_review_paper_inflight: dict[str, threading.Event] = {}  # 同 key 生成中：并发请求等待复用，防双份 LLM 调用
_review_paper_ttl = 3600  # 复习卷缓存有效期（秒），过期后重新生成（讲义按题库变化适度新鲜）


def _review_paper_clear_cache() -> None:
    """管理端改题/删题/词表变更后清缓存（讲义含题目素材，避免推荐已删题/旧题干）。"""
    with _review_paper_lock:
        _review_paper_cache.clear()

def _sanitize_html(html: str) -> str:
    """讲义 HTML 白名单消毒（防存储型 XSS：LLM 输出/题库题干可携带脚本）。"""
    import bleach

    return bleach.clean(
        html,
        tags={
            "p", "br", "strong", "em", "b", "i", "code", "pre",
            "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
            "blockquote", "table", "thead", "tbody", "tr", "th", "td",
            "hr", "a", "span", "div",
        },
        attributes={"a": ["href", "title"]},
        protocols={"http", "https", "mailto"},
        strip=True,
    )


REVIEW_PAPER_PROMPT = """你是面试复习讲师。针对薄弱主题「{tag}」，结合下面题库中该主题的同类题，生成一份针对性复习讲义。

要求：
1. paper 用 markdown 编写（可用标题/列表/粗体/表格/代码块），包含三部分：
   - 核心知识点梳理（该主题必须掌握的概念与原理）
   - 高频考点与易错点（面试常问什么、候选人容易踩的坑）
   - 答题框架与表述要点（按题目难度分层：基础题怎么答、进阶题怎么答）
2. recommended_ids：从下面同类题中选出 3-5 道「最值得优先练习」的题（必须是下列 id 之一）
3. 只输出 JSON，不要其他文字：{{"paper": "markdown 讲义", "recommended_ids": [整数 id 列表]}}

同类题素材：
{materials}"""


def create_app(
    config: AppConfig,
    *,
    llm_factory=None,
    daily_runner=None,
    embedder_factory=None,
    enable_scheduler=False,
) -> FastAPI:
    """构建应用；llm_factory(role)/daily_runner/embedder_factory 可注入（测试用 Fake）。"""
    llm_factory = llm_factory or _default_llm_factory(config)
    embedder_factory = embedder_factory or _default_embedder_factory()
    daily_runner = daily_runner or (
        lambda: _run_daily_pipeline(config, llm_factory)
    )

    app = FastAPI(
        title="DeepGrill",
        # 生产默认禁用 /docs /redoc /openapi.json（公网暴露完整接口结构，降低侦察面）；
        # 本地调试 .env 配 ENABLE_DOCS=1 恢复
        docs_url="/docs" if os.environ.get("ENABLE_DOCS") == "1" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if os.environ.get("ENABLE_DOCS") == "1" else None,
    )

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(
            Path(static_dir) / "index.html",
            headers={"Cache-Control": "no-store"},  # index.html 永不过期缓存，防止旧 HTML 配新 JS 白屏
        )

    @app.middleware("http")
    async def auth_rate_limit_middleware(request: Request, call_next):
        """登录/注册防爆破：失败（≥400）滑动窗口限速，成功清零（内存态）。"""
        if request.url.path not in ("/api/auth/login", "/api/auth/register"):
            return await call_next(request)
        ip = request.client.host if request.client else "unknown"
        if not _rl_check(ip):
            return JSONResponse(status_code=429, content={"detail": "尝试过于频繁，请稍后再试"})
        response = await call_next(request)
        if response.status_code >= 400:
            _rl_fail(ip)
        else:
            _rl_success(ip)
        return response

    WRITE_LIMIT = 60  # 写接口通用突发限流（次/分钟/用户）：防脚本滥用，正常 UI 操作远低于此
    LLM_COST_LIMIT = 10  # LLM 成本接口更严（answer/upload/daily）：防刷爆 LLM 配额

    @app.middleware("http")
    async def write_rate_limit_middleware(request: Request, call_next):
        """写接口通用限流（防脚本滥用）：按 user_id（未登录按 IP）滑动窗口计数。

        answer/upload/daily 会触发 LLM 调用，单独更严的额度；登录/注册已由
        auth_rate_limit_middleware 覆盖（失败计数制），此处跳过。
        """
        if request.method not in ("POST", "PUT", "DELETE"):
            return await call_next(request)
        path = request.url.path
        if not path.startswith("/api/") or path.startswith("/api/auth/"):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        user = user_from_token(auth.removeprefix("Bearer ").strip())
        if user is not None:
            key = f"u{user.id}"
        elif request.client is not None:
            key = f"i{request.client.host}"
        else:
            return await call_next(request)
        is_llm_cost = (
            path == "/api/daily/run"
            or (path.startswith("/api/sessions/") and path.endswith("/answer"))
            or path.startswith("/api/upload")
        )
        if not _rl_allow(key, WRITE_LIMIT) or (
            is_llm_cost and not _rl_allow(key, LLM_COST_LIMIT)
        ):
            return JSONResponse(status_code=429, content={"detail": "操作过于频繁，请稍后再试"})
        return await call_next(request)

    # --- 认证：注册 / 登录 / 登出 / 当前用户 ---

    @app.post("/api/auth/send-code")
    def auth_send_code(body: dict):
        """发送注册验证码：邮箱格式校验 + 60s 限频；SMTP 未配置返回 503。"""
        if len(str(body)) > 1024:
            raise HTTPException(status_code=400, detail="请求体过大")
        from ..email_code import can_send, issue_code, validate_email

        email = str(body.get("email", "")).strip().lower()
        err = validate_email(email)
        if err:
            raise HTTPException(status_code=400, detail=err)
        if not can_send(email):
            raise HTTPException(status_code=429, detail="发送过于频繁，请 60 秒后再试")
        try:
            issue_code(email)
        except RuntimeError as e:
            logger.warning("send code failed for %s: %s", email, e)
            raise HTTPException(status_code=503, detail="邮件服务未配置，请联系管理员")
        except Exception as e:
            logger.warning("send code failed for %s: %s", email, e)
            raise HTTPException(status_code=500, detail="邮件发送失败，请稍后重试")
        return {"ok": True}

    @app.post("/api/auth/register")
    def auth_register(body: dict):
        if len(str(body)) > 4096:
            raise HTTPException(status_code=400, detail="请求体过大")
        from ..email_code import validate_email, verify_code

        email = str(body.get("email", "")).strip().lower()
        code = str(body.get("code", "")).strip()
        password = str(body.get("password", ""))
        err = validate_email(email) or validate_password(password)
        if err:
            raise HTTPException(status_code=400, detail=err)
        if not verify_code(email, code):
            raise HTTPException(status_code=400, detail="验证码错误或已过期")
        focus, focus_lang = _validate_focus(body.get("focus"), body.get("focus_lang"))
        with _register_lock:  # 查重 + owner 判定 + 名额 + 建用户原子化（防并发竞态）
            with db.get_session() as session:
                exists = session.scalars(
                    select(User).where(User.email == email)
                ).first()
                if exists is not None:
                    raise HTTPException(status_code=409, detail="该邮箱已注册")
                has_owner = (
                    session.scalars(select(User).where(User.role == "owner").limit(1)).first()
                    is not None
                )
                if has_owner and user_count() >= MAX_USERS:
                    raise HTTPException(status_code=409, detail="注册名额已满（20 人）")
                user = User(
                    email=email,
                    username=_display_name_for(email, session),
                    password_hash=hash_password(password),
                    role="owner" if not has_owner else "user",  # 无 owner 时补位，永不双生
                    focus=focus,
                    focus_lang=focus_lang,
                )
                session.add(user)
                db.commit(session)
                session.refresh(user)
        token = create_token(user.id)
        return {
            "token": token,
            "user": {"id": user.id, "username": user.username, "role": user.role,
                     "focus": user.focus, "focus_lang": user.focus_lang},
        }

    @app.put("/api/auth/profile")
    def auth_update_profile(body: dict, user: User = Depends(require_user)):
        """更新求职岗位/语言方向（只影响推荐排序，不阻断做题）。"""
        focus, focus_lang = _validate_focus(body.get("focus"), body.get("focus_lang"))
        with db.get_session() as session:
            row = session.get(User, user.id)
            row.focus = focus
            row.focus_lang = focus_lang
            db.commit(session)
            session.refresh(row)
        return {"id": row.id, "username": row.username, "focus": row.focus,
                "focus_lang": row.focus_lang}

    @app.post("/api/auth/login")
    def auth_login(body: dict):
        if len(str(body)) > 4096:
            raise HTTPException(status_code=400, detail="请求体过大")
        email = str(body.get("email", "")).strip().lower()
        password = str(body.get("password", ""))
        with db.get_session() as session:
            user = session.scalars(
                select(User).where(User.email == email)
            ).first()
            if user is None or not verify_password(password, user.password_hash):
                raise HTTPException(status_code=401, detail="邮箱或密码错误")
        token = create_token(user.id)
        return {
            "token": token,
            "user": {"id": user.id, "username": user.username, "role": user.role,
                     "focus": user.focus, "focus_lang": user.focus_lang},
        }

    @app.post("/api/auth/logout")
    def auth_logout(user: User = Depends(require_user), authorization: str = Header(default="")):
        revoke_token(authorization.removeprefix("Bearer ").strip())
        return {"ok": True}

    @app.get("/api/auth/me")
    def auth_me(user: User = Depends(require_user)):
        return {"id": user.id, "username": user.username, "role": user.role,
                "focus": user.focus, "focus_lang": user.focus_lang}

    if enable_scheduler:
        from .. import scheduler

        @app.on_event("startup")
        def startup():
            db.init_db(_db_url())
            from ..tags import reload_tags

            reload_tags()  # 词表从 DB 加载（首次写入种子）
            _seed_owner_if_configured()  # OWNER_USERNAME/OWNER_PASSWORD 预置 owner（防空库抢注）
            scheduler.start_scheduler(config, daily_runner)

        @app.on_event("shutdown")
        def shutdown():
            scheduler.shutdown()

    @app.exception_handler(AppError)
    async def app_error_handler(request, exc: AppError):
        logger.warning("request failed: %s", exc)
        # 不向客户端回显原始异常文本（StorageError 含 SQL/路径，其余可能含内部细节）
        return JSONResponse(status_code=500, content={"error": "服务内部错误，请稍后重试"})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request, exc: RequestValidationError):
        """Pydantic 校验失败 → 400 + 中文 detail（保持前端 body.detail 字符串契约）。"""
        first = exc.errors()[0] if exc.errors() else {}
        msg = str(first.get("msg", "请求参数无效"))
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        return JSONResponse(status_code=400, content={"detail": msg})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request, exc: Exception):
        logger.exception("unhandled error on %s", request.url)
        return JSONResponse(status_code=500, content={"error": "internal error"})

    # --- 今日题目与题目详情 ---

    @app.get("/api/today")
    def today_questions(date: str | None = None, user: User = Depends(require_user)):
        """今日题目（按用户懒加载选题）；date=YYYY-MM-DD 回看该用户当日 picks。"""
        with db.get_session() as session:
            if date is not None:
                try:
                    day = datetime.strptime(date, "%Y-%m-%d")
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"invalid date: {date}")
                start = day.replace(hour=0, minute=0, second=0, microsecond=0)
                end = start + timedelta(days=1)
                picks = list(
                    session.scalars(
                        select(UserPick).where(
                            UserPick.user_id == user.id,
                            UserPick.picked_at >= start,
                            UserPick.picked_at < end,
                        )
                    )
                )
            else:
                picks = _ensure_today_picks(session, user.id)
            questions = [
                q
                for q in (session.get(Question, p.question_id) for p in picks)
                if q is not None and q.reviewed_at is not None  # 审核中题目不展示
            ]
            payload = []
            tag_map = _question_tag_map(session, [q.id for q in questions])
            for q in questions:
                done = _has_finished_session(session, q.id, user.id)
                active = _active_session_id(session, q.id, user.id)
                payload.append(
                    {
                        "id": q.id,
                        "stem": q.stem,
                        "type": q.type.value,
                        "difficulty": q.difficulty,
                        "tags": tag_map.get(q.id, []),
                        "done": done,
                        "active_session_id": active,
                    }
                )
            return payload

    @app.get("/api/questions/{question_id}")
    def question_detail(question_id: int, user: User = Depends(require_user)):
        with db.get_session() as session:
            q = session.get(Question, question_id)
            if q is None:
                raise HTTPException(status_code=404, detail="question not found")
            if user.role != "owner" and q.reviewed_at is None:  # 审核中题目仅 owner 可见
                raise HTTPException(status_code=404, detail="question not found")
            return {
                "id": q.id,
                "stem": q.stem,
                "type": q.type.value,
                "difficulty": q.difficulty,
                "tags": _question_tag_map(session, [question_id]).get(question_id, []),
                "good_criteria": q.good_criteria,
                "bad_criteria": q.bad_criteria,
                "favorited": (
                    session.scalars(
                        select(UserFavorite).where(
                            UserFavorite.user_id == user.id,
                            UserFavorite.question_id == question_id,
                        )
                    ).first()
                    is not None
                ),
            }

    @app.get("/api/questions/{question_id}/history")
    def question_history(question_id: int, user: User = Depends(require_user)):
        """历史详情：该题全部会话（倒序）+ 各自问答记录与判分（D18 复盘）。"""
        with db.get_session() as session:
            q = session.get(Question, question_id)
            if q is None:
                raise HTTPException(status_code=404, detail="question not found")
            rows = session.scalars(
                select(Session)
                .where(
                    Session.question_id == question_id,
                    Session.user_id == user.id,
                )
                .order_by(Session.id.desc())
            ).all()
            attempts = []
            for s in rows:
                judgment = _latest_judgment(session, s.id)
                transcript = _attempts_transcript(s.id)
                if not transcript and judgment is None:
                    continue  # 空壳会话（无回答无判分）不计入统计
                item = {
                    "session_id": s.id,
                    "kind": s.kind.value,
                    "status": s.status.value,
                    "started_at": s.started_at.isoformat(),
                    "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                    "judgment_status": judgment.status if judgment else None,
                    "total_score": judgment.total_score if judgment else None,
                    "transcript": transcript,
                }
                if judgment is not None and judgment.status != STATUS_FAILED:
                    item["judgment"] = _judgment_payload(judgment)
                else:
                    item["judgment"] = None
                attempts.append(item)
            return {
                "id": q.id,
                "stem": q.stem,
                "type": q.type.value,
                "tags": _question_tag_map(session, [question_id]).get(question_id, []),
                "attempts": attempts,
            }

    # --- 会话 ---

    @app.post("/api/sessions")
    def create_session(body: CreateSessionBody, user: User = Depends(require_user)):
        question_id = body.question_id
        kind = body.kind
        with db.get_session() as session:
            if session.get(Question, question_id) is None:
                raise HTTPException(status_code=404, detail="question not found")
            if user.role != "owner" and (
                session.scalars(
                    select(Question.reviewed_at).where(Question.id == question_id)
                ).one()
                is None
            ):
                raise HTTPException(status_code=404, detail="question not found")
            existing = session.scalars(
                select(Session)
                .where(
                    Session.question_id == question_id,
                    Session.user_id == user.id,
                    Session.status == SessionStatus.active,
                )
                .order_by(Session.id.desc())
                .limit(1)
            ).first()
            if existing is not None:
                return {"session_id": existing.id, "status": "active", "resumed": True}
            row = Session(
                question_id=question_id,
                user_id=user.id,
                kind=SessionKind(kind),
            )
            session.add(row)
            try:
                db.commit(session)
            except db.StorageError:
                # 并发双开同题：partial unique index 拦截 → 复用已存在 active 会话
                session.rollback()
                existing = session.scalars(
                    select(Session)
                    .where(
                        Session.question_id == question_id,
                        Session.user_id == user.id,
                        Session.status == SessionStatus.active,
                    )
                    .order_by(Session.id.desc())
                    .limit(1)
                ).first()
                if existing is not None:
                    return {"session_id": existing.id, "status": "active", "resumed": True}
                raise
            session.refresh(row)
        return {"session_id": row.id, "status": "active", "resumed": False}

    @app.post("/api/sessions/{session_id}/answer")
    def submit_answer(
        session_id: int,
        body: AnswerBody,
        background: BackgroundTasks,
        user: User = Depends(require_user),
    ):
        answer = body.answer
        with db.get_session() as session:
            row = session.get(Session, session_id)
            if row is None or row.user_id != user.id:
                raise HTTPException(status_code=404, detail="session not found")
            latest = _latest_judgment(session, session_id)
            if row.status == SessionStatus.finished and not (
                latest is not None and latest.status == STATUS_FAILED
            ):
                raise HTTPException(status_code=409, detail="session already finished")
        with _inflight_lock:
            if session_id in _inflight:
                raise HTTPException(status_code=409, detail="session is being judged")
            _inflight[session_id] = time.time()
        background.add_task(
            _run_answer_job, session_id, answer, config, llm_factory, embedder_factory
        )
        return {"status": "judging"}

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: int, user: User = Depends(require_user)):
        """删除单次作答（含回答轮次与判分）；题目保留。"""
        with _inflight_lock:
            if session_id in _inflight:
                raise HTTPException(status_code=409, detail="session is being judged")
        with db.get_session() as session:
            row = session.get(Session, session_id)
            if row is None or row.user_id != user.id:
                raise HTTPException(status_code=404, detail="session not found")
            session.delete(row)  # cascade 级联删 attempts/judgments
            db.commit(session)
        return {"deleted": True}

    @app.delete("/api/questions/{question_id}/history")
    def delete_question_history(question_id: int, user: User = Depends(require_user)):
        """删除该题全部作答记录（会话/回答/判分）；题目本身保留。"""
        with db.get_session() as session:
            if session.get(Question, question_id) is None:
                raise HTTPException(status_code=404, detail="question not found")
            if _question_inflight(session, question_id):
                raise HTTPException(status_code=409, detail="该题正在判分中，请稍后再试")
            rows = session.scalars(
                select(Session).where(
                    Session.question_id == question_id,
                    Session.user_id == user.id,
                )
            ).all()
            for row in rows:
                session.delete(row)
            db.commit(session)
        return {"deleted": True}

    @app.get("/api/sessions/{session_id}")
    def session_status(session_id: int, user: User = Depends(require_user)):
        with db.get_session() as session:
            row = session.get(Session, session_id)
            if row is None or row.user_id != user.id:
                raise HTTPException(status_code=404, detail="session not found")
            question = session.get(Question, row.question_id)
            payload = {
                "session_id": row.id,
                "status": "active",
                "question_id": row.question_id,
            }
            if question is not None:
                payload["stem"] = question.stem
            if row.status == SessionStatus.finished:
                judgment = _latest_judgment(session, session_id)
                if judgment is None:
                    payload["status"] = "failed"
                    payload["message"] = "判分失败，点击重试"
                elif judgment.status == STATUS_FAILED:
                    payload["status"] = "failed"
                    payload["message"] = "判分失败，点击重试"
                else:
                    payload["status"] = "done"
                    payload["judgment"] = _judgment_payload(judgment)
                return payload
            if session_id in _inflight_snapshot():
                payload["status"] = "judging"
            else:
                payload["rounds_done"] = _count_attempts(session, session_id)
                payload["followup"] = _latest_followup(session, session_id)
                payload["transcript"] = _attempts_transcript(session_id)
            return payload

    @app.get("/api/sessions/{session_id}/resume")
    def resume_session(session_id: int, user: User = Depends(require_user)):
        with db.get_session() as session:
            row = session.get(Session, session_id)
            if row is None or row.user_id != user.id:
                raise HTTPException(status_code=404, detail="session not found")
            if row.status == SessionStatus.finished:
                raise HTTPException(status_code=409, detail="session already finished")
            question = session.get(Question, row.question_id)
            if question is None:
                raise HTTPException(status_code=404, detail="question not found")
            max_rounds = max_rounds_for(question.difficulty, config.daily.chain_max_rounds)
            chain = resume(session_id, question, llm_factory("judge"),
                           judge_model=config.llm.judge_model,
                           max_rounds=max_rounds,
                           target_level=target_level_for(question.difficulty))
            return {
                "session_id": row.id,
                "question_id": row.question_id,
                "stem": question.stem,
                "rounds_done": chain.rounds_done,
                "max_rounds": max_rounds,
            }

    # --- 历史（按被选为今日题目的日期分组，未作答也展示） ---

    @app.get("/api/history")
    def history(qtype: str | None = None, user: User = Depends(require_user)):
        with db.get_session() as session:
            picks = list(
                session.scalars(
                    select(UserPick)
                    .where(UserPick.user_id == user.id)
                    .order_by(UserPick.picked_at.desc())
                )
            )
            latest = _latest_judgment_by_question(session, user.id)

            groups: dict[str, list[dict]] = {}
            qids = [p.question_id for p in picks if session.get(Question, p.question_id) is not None]
            tag_map = _question_tag_map(session, qids)
            for p in picks:
                q = session.get(Question, p.question_id)
                if q is None:
                    continue
                if qtype and q.type.value != qtype:
                    continue
                item = {
                    "question_id": q.id,
                    "stem": q.stem,
                    "type": q.type.value,
                    "difficulty": q.difficulty,
                    "tags": tag_map.get(q.id, []),
                    "done": False,
                    "status": "not_answered",
                    "total_score": None,
                    "session_id": None,
                    "done_at": None,
                }
                pair = latest.get(q.id)
                if pair is not None:
                    s, j = pair
                    item["done"] = True
                    item["status"] = j.status
                    item["total_score"] = j.total_score
                    item["session_id"] = s.id
                    item["done_at"] = s.ended_at.isoformat() if s.ended_at else None
                day = p.picked_at.date().isoformat()
                groups.setdefault(day, []).append(item)
            return [
                {"date": day, "items": groups[day]}
                for day in sorted(groups, reverse=True)
            ]

    # --- 统计概览（打卡日历 + 答题趋势 + 汇总数字） ---

    @app.get("/api/stats")
    def stats(user: User = Depends(require_user)):
        """该用户统计：题库总数/已答数/已完成题数/平均分 + 近 30 天答题趋势。

        trend 按 finished 判分会话（ended_at 当天）聚合；failed 判分不计入
        （failed 的 judgment total_score 为 NULL，用 IS NOT NULL 过滤）。
        """
        today = datetime.now().date()
        with db.get_session() as session:
            bank_total = session.scalar(
                select(func.count())
                .select_from(Question)
                .where(Question.reviewed_at.is_not(None))
            )
            rows = session.execute(
                select(
                    func.date(Session.ended_at),
                    func.count(Session.id),
                    func.avg(Judgment.total_score),
                )
                .join(Judgment, Judgment.session_id == Session.id)
                .where(
                    Session.user_id == user.id,
                    Session.status == SessionStatus.finished,
                    Session.ended_at.is_not(None),
                    Judgment.total_score.is_not(None),
                )
                .group_by(func.date(Session.ended_at))
            ).all()
            done_questions = len(_latest_judgment_by_question(session, user.id))
        by_day = {date_str: (n, avg) for date_str, n, avg in rows}
        trend = []
        answered_total = 0
        score_sum = 0.0
        for i in range(29, -1, -1):
            day = today - timedelta(days=i)
            date_str = day.isoformat()
            n, avg = by_day.get(date_str, (0, None))
            answered_total += n or 0
            if avg is not None:
                score_sum += avg * n
            trend.append(
                {
                    "date": date_str,
                    "answered": n or 0,
                    "avg_score": round(avg) if avg is not None else None,
                }
            )
        avg_score = round(score_sum / answered_total) if answered_total else None
        return {
            "bank_total": bank_total,
            "answered_total": answered_total,
            "done_questions": done_questions,
            "avg_score": avg_score,
            "trend": trend,
        }

    # --- 题库浏览（分页 + 筛选） ---

    @app.get("/api/bank")
    def bank_questions(
        page: int = 1,
        page_size: int = 20,
        type: str | None = None,
        category: str | None = None,
        q: str | None = None,
        difficulty: int | None = None,
        done: str | None = None,
        sort: str | None = None,
        reviewed: str | None = None,
        user: User = Depends(require_user),
    ):
        """全部题目分页浏览（id 倒序）；type/category/difficulty/done 筛选；sort 排序；q 关键词搜题干+标签；每项带 done 标志。

        reviewed=0/1 仅 owner 可用（审核页）：0=待审核队列，1=已审核；缺省时非 owner 只见已审核。
        """
        from ..models import QuestionType as _QT

        if page < 1 or not 1 <= page_size <= 50:
            raise HTTPException(status_code=400, detail="invalid page or page_size")
        is_owner = user.role == "owner"
        if reviewed is not None and not is_owner:
            raise HTTPException(status_code=403, detail="permission denied")
        if reviewed is not None and reviewed not in ("0", "1"):
            raise HTTPException(status_code=400, detail="invalid reviewed: must be 0 or 1")
        qtype = None
        if type is not None:
            try:
                qtype = _QT(type)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"invalid type: {type}")
        if difficulty is not None and not 1 <= difficulty <= 5:
            raise HTTPException(status_code=400, detail=f"invalid difficulty: {difficulty}")
        if done is not None and done not in ("done", "todo"):
            raise HTTPException(status_code=400, detail=f"invalid done: {done}")
        if sort is not None and sort not in ("newest", "oldest", "easy", "hard"):
            raise HTTPException(status_code=400, detail=f"invalid sort: {sort}")
        if category is not None and category not in {name for name, _ in TAG_CATEGORIES}:
            raise HTTPException(status_code=400, detail=f"invalid category: {category}")
        cat_tags = None
        if category is not None:
            cat_tags = dict(TAG_CATEGORIES)[category]
        keyword = q.strip().lower() if q else ""

        with db.get_session() as session:
            # 筛选/排序/分页全部下推 SQL：Python 层全量过滤 2293 行在 GIL 下并发膨胀（单请求 65ms → 10 并发 ~1s）
            filters = []
            if reviewed == "0":
                filters.append(Question.reviewed_at.is_(None))  # owner 审核队列
            elif reviewed == "1" or not is_owner:
                filters.append(Question.reviewed_at.is_not(None))  # 已审核（非 owner 只能看已审核）
            if qtype is not None:
                filters.append(Question.type == qtype)
            if difficulty is not None:
                filters.append(Question.difficulty == difficulty)
            if keyword:
                filters.append(
                    or_(
                        Question.stem.ilike(f"%{keyword}%"),
                        _tags_contains(keyword),
                    )
                )
            if cat_tags is not None:
                filters.append(
                    or_(*(_tags_exists(t) for t in cat_tags))
                )
            if done is not None:
                answered = exists().where(
                    Session.question_id == Question.id,
                    Session.user_id == user.id,
                    Session.status == SessionStatus.finished,
                )
                filters.append(answered if done == "done" else ~answered)
            order_by = {
                "oldest": Question.id.asc(),
                "easy": (Question.difficulty.asc(), Question.id.desc()),
                "hard": (Question.difficulty.desc(), Question.id.desc()),
                "newest": Question.id.desc(),
            }[sort or "newest"]
            total = session.scalar(
                select(func.count()).select_from(Question).where(*filters)
            )
            rows = session.execute(
                select(
                    Question.id, Question.stem, Question.type, Question.difficulty,
                    Question.reviewed_at,
                )
                .where(*filters)
                .order_by(*([order_by] if not isinstance(order_by, tuple) else order_by))
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            total_pages = (total + page_size - 1) // page_size
            qids = [q.id for q in rows]
            tag_map = _question_tag_map(session, qids)
            fav_ids = set(
                session.scalars(
                    select(UserFavorite.question_id).where(
                        UserFavorite.user_id == user.id,
                        UserFavorite.question_id.in_(qids),
                    )
                ).all()
            ) if qids else set()
            latest = _latest_judgment_by_question(session, user.id)
            items = [
                {
                    "id": q.id,
                    "stem": q.stem,
                    "type": q.type.value,
                    "difficulty": q.difficulty,
                    "tags": tag_map.get(q.id, []),
                    "done": q.id in latest,
                    "favorited": q.id in fav_ids,
                    "reviewed": q.reviewed_at is not None,
                }
                for q in rows
            ]
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "items": items,
        }

    # --- 收藏 ---

    @app.post("/api/favorites/{question_id}")
    def favorite_question(question_id: int, user: User = Depends(require_user)):
        """收藏题目（幂等：已收藏返回成功）。"""
        with db.get_session() as session:
            if session.get(Question, question_id) is None:
                raise HTTPException(status_code=404, detail="question not found")
            if (
                session.scalars(
                    select(UserFavorite).where(
                        UserFavorite.user_id == user.id,
                        UserFavorite.question_id == question_id,
                    )
                ).first()
                is None
            ):
                session.add(UserFavorite(user_id=user.id, question_id=question_id))
                db.commit(session)
        return {"favorited": True}

    @app.delete("/api/favorites/{question_id}")
    def unfavorite_question(question_id: int, user: User = Depends(require_user)):
        """取消收藏（幂等：未收藏返回成功）。"""
        with db.get_session() as session:
            fav = session.scalars(
                select(UserFavorite).where(
                    UserFavorite.user_id == user.id,
                    UserFavorite.question_id == question_id,
                )
            ).first()
            if fav is not None:
                session.delete(fav)
                db.commit(session)
        return {"favorited": False}

    @app.get("/api/favorites")
    def favorite_questions(
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
        user: User = Depends(require_user),
    ):
        """我的收藏列表（分页，id 倒序）；q 关键词搜题干。"""
        if page < 1 or not 1 <= page_size <= 50:
            raise HTTPException(status_code=400, detail="invalid page or page_size")
        with db.get_session() as session:
            filters = [UserFavorite.user_id == user.id]
            if q and q.strip():
                filters.append(Question.stem.ilike(f"%{q.strip().lower()}%"))
            total = session.scalar(
                select(func.count())
                .select_from(UserFavorite)
                .join(Question, Question.id == UserFavorite.question_id)
                .where(*filters)
            )
            rows = session.execute(
                select(Question.id, Question.stem, Question.type, Question.difficulty)
                .join(UserFavorite, UserFavorite.question_id == Question.id)
                .where(*filters)
                .order_by(UserFavorite.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            total_pages = (total + page_size - 1) // page_size
            qids = [r.id for r in rows]
            tag_map = _question_tag_map(session, qids)
            latest = _latest_judgment_by_question(session, user.id)
            items = [
                {
                    "id": r.id,
                    "stem": r.stem,
                    "type": r.type.value,
                    "difficulty": r.difficulty,
                    "tags": tag_map.get(r.id, []),
                    "done": r.id in latest,
                    "favorited": True,
                }
                for r in rows
            ]
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "items": items,
        }

    # --- 薄弱点复习（Phase 2 v1：SQL 标签匹配） ---

    @app.get("/api/tags")
    def tag_categories():
        """标签分类结构（岗位/语言标注 + 标签，前端筛选联动用）。"""
        from ..tags import CATEGORY_LANG, CATEGORY_ROLES

        return [
            {
                "name": name,
                "lang": CATEGORY_LANG.get(name),
                "roles": list(CATEGORY_ROLES.get(name, ())),
                "tags": list(tags),
            }
            for name, tags in TAG_CATEGORIES
        ]

    @app.get("/api/review/tags")
    def review_tags(user: User = Depends(require_user)):
        """全部词表标签 + 薄弱点计数（该用户 judgments.weak_tags 聚合）；有计数在前。"""
        with db.get_session() as session:
            rows = session.execute(
                select(Judgment.weak_tags)
                .join(Session, Judgment.session_id == Session.id)
                .where(Session.user_id == user.id)
            ).all()
        counter: Counter = Counter()
        for (tags,) in rows:
            if isinstance(tags, list):
                for t in tags:
                    if isinstance(t, str):
                        counter[t] += 1
        items = [{"tag": t, "count": counter.get(t, 0)} for t in TAG_VOCABULARY]
        items.sort(key=lambda x: (-x["count"], list(TAG_VOCABULARY).index(x["tag"])))
        return items

    @app.get("/api/review")
    def review(tag: str, user: User = Depends(require_user)):
        if tag not in TAG_VOCABULARY:
            raise HTTPException(status_code=400, detail=f"invalid tag: {tag}")
        with db.get_session() as session:
            questions, fallback, fallback_category = _review_questions(session, tag)
            latest = _latest_judgment_by_question(session, user.id)
            tag_map = _question_tag_map(session, [q.id for q in questions])
            items = []
            for q in questions:
                item = {
                    "id": q.id,
                    "stem": q.stem,
                    "type": q.type.value,
                    "difficulty": q.difficulty,
                    "tags": tag_map.get(q.id, []),
                    "done": False,
                    "total_score": None,
                }
                pair = latest.get(q.id)
                if pair is not None:
                    s, j = pair
                    item["done"] = True
                    item["total_score"] = j.total_score if j.status != STATUS_FAILED else None
                items.append(item)
            items.sort(key=lambda i: (i["done"], -i["id"]))  # 未做优先，组内新题在前
            return {
                "items": items,
                "fallback": fallback,
                "fallback_category": fallback_category,
            }

    @app.get("/api/review/paper")
    def review_paper(tag: str, user: User = Depends(require_user)):
        """薄弱点复习卷 v2：LLM 生成针对性复习讲义（markdown 转 HTML）+ 推荐练习题目。

        同步生成（10-30s）；用户+标签级内存缓存（1 小时 TTL）；LLM 失败降级返回
        空讲义 + error 提示（不 500），可稍后重试。该标签无题时回退同分类题目
        （fallback 语义与 /api/review 一致，讲义仍按原标签主题生成）。
        """
        if tag not in TAG_VOCABULARY:
            raise HTTPException(status_code=400, detail=f"invalid tag: {tag}")
        cache_key = f"{user.id}:{tag}"
        with _review_paper_lock:
            cached = _review_paper_cache.get(cache_key)
            if cached is not None and time.time() - cached["ts"] < _review_paper_ttl:
                return cached["payload"]
            inflight_ev = _review_paper_inflight.get(cache_key)
            if inflight_ev is None:
                inflight_ev = threading.Event()
                _review_paper_inflight[cache_key] = inflight_ev  # 本请求承担生成
                wait_ev = None
            else:
                wait_ev = inflight_ev  # 已有请求在生成：等待复用结果，避免双份 LLM 调用
        if wait_ev is not None:
            wait_ev.wait(timeout=90)
            with _review_paper_lock:
                cached = _review_paper_cache.get(cache_key)
                if cached is not None and time.time() - cached["ts"] < _review_paper_ttl:
                    return cached["payload"]
            return {
                "paper_html": "",
                "recommended_ids": [],
                "error": "复习卷生成中，请稍后重试",
            }
        with db.get_session() as session:
            questions, fallback, fallback_category = _review_questions(session, tag)
        if not questions:
            with _review_paper_lock:
                done_ev = _review_paper_inflight.pop(cache_key, None)
                if done_ev is not None:
                    done_ev.set()
            return {"paper_html": "", "recommended_ids": []}
        materials = "\n".join(
            f"- [id={q.id}]（难度 {q.difficulty}/5）{q.stem}"
            for q in questions[:8]
        )
        knowledge = _knowledge_for(tag, None, embedder_factory, llm=llm_factory("generate"))
        if knowledge:
            materials += "\n\n【知识资料（供讲义要点核对）】\n" + knowledge
        try:
            parsed = llm_factory("generate").complete(
                [{"role": "user", "content": REVIEW_PAPER_PROMPT.format(
                    tag=tag, materials=materials,
                )}],
                json_schema={},
            )
        except Exception as e:
            logger.warning("review paper generate failed for %s: %s", tag, e)
            with _review_paper_lock:
                done_ev = _review_paper_inflight.pop(cache_key, None)
                if done_ev is not None:
                    done_ev.set()
            return {
                "paper_html": "",
                "recommended_ids": [],
                "error": "复习卷生成失败，请稍后重试",
            }
        paper = parsed.get("paper", "") if isinstance(parsed, dict) else ""
        recommended = (
            [int(i) for i in parsed.get("recommended_ids", [])]
            if isinstance(parsed, dict)
            else []
        )
        valid_ids = {q.id for q in questions}
        recommended = [i for i in recommended if i in valid_ids][:5]
        from markdown import markdown as _md

        paper_html = _sanitize_html(_md(paper)) if paper else ""
        payload = {"paper_html": paper_html, "recommended_ids": recommended}
        with _review_paper_lock:
            _review_paper_cache[cache_key] = {"payload": payload, "ts": time.time()}
            done_ev = _review_paper_inflight.pop(cache_key, None)
            if done_ev is not None:
                done_ev.set()
        return payload

    # --- 管理员题库管理（owner） ---

    @app.put("/api/admin/questions/{question_id}")
    def admin_update_question(
        question_id: int, body: AdminQuestionUpdate, user: User = Depends(require_owner)
    ):
        """管理员修改题目：stem/tags/difficulty/good_criteria/bad_criteria 可改（type 不可改）。"""
        data = body.model_dump(exclude_unset=True)
        with db.get_session() as session:
            row = session.get(Question, question_id)
            if row is None:
                raise HTTPException(status_code=404, detail="question not found")
            if "stem" in data:
                row.stem = data["stem"]
            if "tags" in data:
                valid = [t for t in data["tags"] if t in TAG_VOCABULARY][:5]
                _set_question_tags(session, question_id, valid)
            if "reviewed" in data:
                row.reviewed_at = datetime.now() if data["reviewed"] else None
            if "difficulty" in data:
                row.difficulty = data["difficulty"]
            for field in ("good_criteria", "bad_criteria"):
                if field in data:
                    setattr(row, field, data[field])
            db.commit(session)
            session.refresh(row)
        _review_paper_clear_cache()  # 讲义素材含题干，改题后失效
        return {
            "id": row.id,
            "stem": row.stem,
            "difficulty": row.difficulty,
            "tags": _question_tag_map(session, [question_id]).get(question_id, []),
        }

    @app.delete("/api/admin/questions/{question_id}")
    def admin_delete_question(
        question_id: int, user: User = Depends(require_owner)
    ):
        """管理员删除题目：一条 DELETE，会话链/收藏/标签关联 DB 级联清理。"""
        with db.get_session() as session:
            row = session.get(Question, question_id)
            if row is None:
                raise HTTPException(status_code=404, detail="question not found")
            if _question_inflight(session, question_id):
                raise HTTPException(status_code=409, detail="该题正在判分中，请稍后再试")
            session.delete(row)
            db.commit(session)
        _review_paper_clear_cache()
        return {"deleted": True}

    @app.post("/api/admin/questions/batch-delete")
    def admin_batch_delete_questions(
        body: dict, user: User = Depends(require_owner)
    ):
        """管理员批量删除题目：ids 列表（单次 ≤500）；判分中的题跳过（返回 skipped）；分批事务防长锁。"""
        raw_ids = body.get("ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise HTTPException(status_code=400, detail="ids 需为非空列表")
        if len(raw_ids) > 500:
            raise HTTPException(status_code=400, detail="单次最多 500 题")
        try:
            ids = [int(i) for i in raw_ids]
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="ids 必须都是整数")
        deleted = 0
        skipped = 0
        with db.get_session() as session:
            rows = list(session.scalars(select(Question).where(Question.id.in_(ids))))
            rows = [r for r in rows if not _question_inflight(session, r.id)]
            for i in range(0, len(rows), 50):  # 分批提交：500 题单事务会持写锁超 5s 撞 busy_timeout
                batch = rows[i : i + 50]
                for row in batch:
                    session.delete(row)
                db.commit(session)
                deleted += len(batch)
            skipped = len(ids) - deleted
        _review_paper_clear_cache()
        return {"deleted": deleted, "skipped": skipped}

    # --- 标签/分类管理（词表数据库化，owner） ---

    @app.get("/api/admin/tags/tree")
    def admin_tags_tree(user: User = Depends(require_owner)):
        """完整词表树（带 id，供审核页管理区）。"""
        from ..models import Tag, TagCategory

        with db.get_session() as session:
            cats = list(session.scalars(select(TagCategory).order_by(TagCategory.id)))
            items = list(session.scalars(select(Tag).order_by(Tag.id)))
        by_cat: dict[int, list[dict]] = {}
        for it in items:
            by_cat.setdefault(it.category_id, []).append({"id": it.id, "name": it.name, "is_custom": it.is_custom})
        return [
            {"id": c.id, "name": c.name, "is_custom": c.is_custom, "tags": by_cat.get(c.id, [])}
            for c in cats
        ]

    @app.get("/api/review/suggestions")
    def review_suggestions(ids: str = "", user: User = Depends(require_user)):
        """AI 审核参考（并入 questions 的建议快照）：?ids=1,2,3 → {suggestions: {qid: {...}}}。"""
        qids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        if not qids:
            return {"suggestions": {}}
        with db.get_session() as session:
            rows = session.execute(
                select(
                    Question.id,
                    Question.suggested_category,
                    Question.suggested_tags,
                    Question.suggested_difficulty,
                ).where(Question.id.in_(qids))
            ).all()
        return {
            "suggestions": {
                qid: {
                    "suggested_category": cat or "",
                    "suggested_tags": tags or [],
                    "suggested_difficulty": diff,
                }
                for qid, cat, tags, diff in rows
            }
        }

    @app.post("/api/admin/categories")
    def admin_add_category(body: dict, user: User = Depends(require_owner)):
        """新增分类（词表动态化）；可选指定岗位 roles 与语言 lang。"""
        from ..models import TagCategory

        name = str(body.get("name", "")).strip()
        if not name or len(name) > 20:
            raise HTTPException(status_code=400, detail="分类名需 1-20 字符")
        raw_roles = body.get("roles")
        if raw_roles is None:
            roles: list[str] = []
        elif isinstance(raw_roles, list) and all(isinstance(r, str) for r in raw_roles):
            roles = raw_roles
        else:
            raise HTTPException(status_code=400, detail="roles 需为字符串数组")
        lang = body.get("lang")
        if lang is not None and not isinstance(lang, str):
            raise HTTPException(status_code=400, detail="lang 需为字符串")
        with db.get_session() as session:
            if session.scalars(select(TagCategory).where(TagCategory.name == name)).first():
                raise HTTPException(status_code=409, detail="分类已存在")
            cat = TagCategory(name=name, is_custom=1, roles=roles, lang=lang)
            session.add(cat)
            db.commit(session)
            session.refresh(cat)
        from ..tags import reload_tags

        reload_tags()
        _review_paper_clear_cache()
        return {"id": cat.id, "name": cat.name, "roles": cat.roles, "lang": cat.lang}

    @app.delete("/api/admin/categories/{category_id}")
    def admin_delete_category(category_id: int, user: User = Depends(require_owner)):
        """删除空分类（非空拒绝）；DB 级联删除其下标签（业务层 409 先拦非空）。"""
        from ..models import Tag, TagCategory

        with db.get_session() as session:
            cat = session.get(TagCategory, category_id)
            if cat is None:
                raise HTTPException(status_code=404, detail="category not found")
            if session.scalars(select(Tag).where(Tag.category_id == category_id).limit(1)).first():
                raise HTTPException(status_code=409, detail="分类下有标签，先清空标签")
            session.delete(cat)
            db.commit(session)
        from ..tags import reload_tags

        reload_tags()
        _review_paper_clear_cache()
        return {"deleted": True}

    @app.post("/api/admin/tags")
    def admin_add_tag(body: dict, user: User = Depends(require_owner)):
        """新增标签（指定分类）。"""
        from ..models import Tag, TagCategory

        name = str(body.get("name", "")).strip()
        category_id = body.get("category_id")
        if not name or len(name) > 20:
            raise HTTPException(status_code=400, detail="标签名需 1-20 字符")
        with db.get_session() as session:
            if session.get(TagCategory, category_id) is None:
                raise HTTPException(status_code=400, detail="分类不存在")
            if session.scalars(select(Tag).where(Tag.name == name)).first():
                raise HTTPException(status_code=409, detail="标签已存在")
            item = Tag(category_id=category_id, name=name, is_custom=1)
            session.add(item)
            db.commit(session)
            session.refresh(item)
        from ..tags import reload_tags

        reload_tags()
        _review_paper_clear_cache()
        return {"id": item.id, "name": item.name}

    @app.delete("/api/admin/tags/{tag_id}")
    def admin_delete_tag(tag_id: int, user: User = Depends(require_owner)):
        """删除标签：一条 DELETE，题上关联（question_tags）DB 级联清除。"""
        from ..models import Tag

        with db.get_session() as session:
            item = session.get(Tag, tag_id)
            if item is None:
                raise HTTPException(status_code=404, detail="tag not found")
            session.delete(item)
            db.commit(session)
        from ..tags import reload_tags

        reload_tags()
        _review_paper_clear_cache()
        return {"deleted": True}

    # --- 数据备份：导出/导入（sqlite 整库备份 zip） ---

    @app.get("/api/export")
    def export_backup(user: User = Depends(require_owner)):
        """整库备份：sqlite3 backup API 复制当前库 → zip（interview.db + meta.json）下载。"""
        import io
        import json as _json
        import sqlite3 as _sqlite3
        import tempfile as _tempfile
        import zipfile

        with db.get_session() as session:
            total = session.scalars(select(func.count(Question.id))).one()
        tmp_db = Path(_tempfile.gettempdir()) / f"interview_backup_{secrets.token_hex(4)}.db"
        dst = _sqlite3.connect(str(tmp_db))
        try:
            with db.engine().connect() as conn:
                conn.connection.backup(dst)
            dst.close()
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(tmp_db, "interview.db")
                meta = {
                    "app": "DeepGrill",
                    "exported_at": datetime.now().isoformat(),
                    "questions": total,
                }
                zf.writestr("meta.json", _json.dumps(meta, ensure_ascii=False, indent=1))
            zip_buf.seek(0)
            return Response(
                content=zip_buf.getvalue(),
                media_type="application/zip",
                headers={
                    "Content-Disposition": (
                        f"attachment; filename=interview_backup_{datetime.now():%Y%m%d_%H%M%S}.zip"
                    )
                },
            )
        finally:
            try:
                dst.close()
            except Exception:
                pass
            tmp_db.unlink(missing_ok=True)

    @app.post("/api/import")
    def import_backup(body: dict, user: User = Depends(require_owner)):
        """从备份 zip 恢复：解压校验 → 当前库备份 → 原子替换 → 重建引擎。

        替换运行中 SQLite 文件前先 dispose 引擎连接；恢复后调用方刷新页面。
        """
        import io
        import sqlite3 as _sqlite3
        import tempfile as _tempfile
        import zipfile

        from .. import db as _db
        from ..db import db_url as _db_url_fn

        content_base64 = str(body.get("content_base64", ""))
        try:
            data = base64.b64decode(content_base64)
        except Exception:
            raise HTTPException(status_code=400, detail="文件编码无效")
        if len(data) > 200 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="备份文件超过 200MB 限制")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
            names = zf.namelist()
            if len(names) > 100:
                raise HTTPException(status_code=400, detail="备份文件成员过多")
            if "interview.db" not in names:
                raise HTTPException(status_code=400, detail="备份文件中缺少 interview.db")
            max_db = 500 * 1024 * 1024
            with zf.open("interview.db") as f:
                db_bytes = f.read(max_db + 1)  # 流式 inflate：超限即停，防解压炸弹
            if len(db_bytes) > max_db:
                raise HTTPException(status_code=400, detail="解压后超过 500MB 限制")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"备份文件无效：{e}")
        if db_bytes[:16] != b"SQLite format 3\x00":
            raise HTTPException(status_code=400, detail="interview.db 不是有效的 SQLite 文件")
        tmp_check = Path(_tempfile.gettempdir()) / f"interview_restore_{secrets.token_hex(4)}.db"
        tmp_check.write_bytes(db_bytes)
        check_conn = _sqlite3.connect(str(tmp_check))
        try:
            tables = {
                r[0]
                for r in check_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required = {
                "questions", "sources", "sessions", "attempts", "judgments", "task_logs",
                "users", "user_tokens", "user_picks",
            }
            if not required.issubset(tables):
                raise HTTPException(
                    status_code=400,
                    detail="备份不含用户数据（非多用户备份），拒绝导入",
                )
        finally:
            check_conn.close()
            tmp_check.unlink(missing_ok=True)

        db_path = Path(_db_url_fn().removeprefix("sqlite:///"))
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        backup_dir = Path("data/backup")
        backup_dir.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            pre = backup_dir / f"pre_import_{datetime.now():%Y%m%d_%H%M%S}.db"
            # sqlite3.backup API：WAL 模式下裸读主文件会丢 -wal 内容（快照陈旧/撕裂）
            _src = _sqlite3.connect(str(db_path))
            try:
                _dst = _sqlite3.connect(str(pre))
                try:
                    _src.backup(_dst)
                finally:
                    _dst.close()
            finally:
                _src.close()
            # 保留最近 5 份 pre_import 备份，防磁盘无限膨胀
            for old in sorted(backup_dir.glob("pre_import_*.db"))[:-5]:
                old.unlink(missing_ok=True)
        with _db._import_lock:
            _db.close()
            tmp_swap = Path(str(db_path) + f".restore_{secrets.token_hex(4)}")
            tmp_swap.write_bytes(db_bytes)
            last_err = None
            for _attempt in range(5):  # Windows 文件句柄释放有时序，重试
                try:
                    tmp_swap.replace(db_path)
                    last_err = None
                    break
                except PermissionError as e:
                    last_err = e
                    import time as _time

                    _time.sleep(0.4)
            if last_err is not None:
                logger.error("import backup failed: %s", last_err)
                raise HTTPException(status_code=500, detail="数据库文件被占用，恢复失败，请稍后重试")
            _db.init_db(_db_url_fn())
        with db.get_session() as session:
            total = session.scalars(select(func.count(Question.id))).one()
        new_token = None
        with db.get_session() as session:
            imported_user = session.scalars(
                select(User).where(User.username == user.username)
            ).first()
            if imported_user is not None:
                from ..auth import create_token

                new_token = create_token(imported_user.id)  # 备份库中同用户 → 补新 token 保持登录态
        return {"restored": True, "questions": total, "new_token": new_token}

    # --- 用户上传题目（格式：题目列表 / 面经文本 / 简历；支持 pdf/docx/doc 二进制） ---

    @app.post("/api/upload")
    def upload_questions(
        body: UploadBody, background: BackgroundTasks, user: User = Depends(require_owner)
    ):
        filename = body.filename
        content = body.content
        content_base64 = body.content_base64
        up_type = body.type
        suffix = Path(filename).suffix.lower()
        if suffix not in {".md", ".txt", ".pdf", ".doc", ".docx"}:
            raise HTTPException(status_code=400, detail="仅支持 .md/.txt/.pdf/.doc/.docx 文件")
        if content_base64:
            try:
                data = base64.b64decode(content_base64)
            except Exception:
                raise HTTPException(status_code=400, detail="文件编码无效")
            if len(data) > 20 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="文件超过 20MB 限制")
            if not data:
                raise HTTPException(status_code=400, detail="文件内容为空")
            token = _start_binary_upload(
                filename, data, up_type, body.count, llm_factory, embedder_factory, user.id
            )
            return {"mode": "parsing", "token": token}
        if len(content.encode("utf-8")) > 20 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="文件超过 20MB 限制")
        if not content.strip():
            raise HTTPException(status_code=400, detail="文件内容为空")
        if up_type == "resume":
            count = _clamp_resume_count(body.count)
            source = _import_resume(content)
            token = _start_resume_parse(source.id, count, llm_factory, user.id)
            return {"mode": "resume", "token": token, "source_id": source.id}
        if up_type == "facejing" or (up_type == "auto" and not _is_direct_format(content)):
            source = _import_facejing(content, filename)
            background.add_task(
                _generate_uploaded_source, source.id, llm_factory, embedder_factory
            )
            return {"mode": "facejing", "source_id": source.id}
        source, count = _insert_direct_questions(content)
        background.add_task(_tag_direct_questions, source.id, llm_factory)
        return {"mode": "direct", "count": count, "source_id": source.id}

    @app.get("/api/upload/status/{token}")
    def upload_status(token: str, user: User = Depends(require_owner)):
        """二进制上传解析轮询：parsing/done/failed；resume 完成带 candidates_token 续候选流程。"""
        with _upload_lock:
            entry = _upload_tasks.get(token)
            if entry is None:
                raise HTTPException(status_code=404, detail="token not found")
            return dict(entry)

    @app.get("/api/upload/candidates/{token}")
    def upload_candidates(token: str, user: User = Depends(require_owner)):
        """简历解析轮询：running/done/failed；done 返回候选题（题干可编辑，tags/难度只读）。"""
        with _resume_lock:
            entry = _resume_candidates.get(token)
            if entry is None:
                raise HTTPException(status_code=404, detail="token not found")
            payload = {"status": entry["status"]}
            if entry["status"] == "done":
                payload["items"] = [
                    {
                        "stem": q.stem,
                        "tags": list(getattr(q, "_pending_tags", [])),
                        "difficulty": q.difficulty,
                    }
                    for q in entry["questions"]
                ]
            elif entry["status"] == "failed":
                payload["error"] = entry.get("error", "")
            return payload

    @app.post("/api/upload/confirm")
    def upload_confirm(body: dict, background: BackgroundTasks, user: User = Depends(require_owner)):
        """简历候选确认：用户编辑后的题 → 校验 → 去重 → 入库（background 内跑 embedding dedup）。

        token 绑定创建者 user_id：他人 confirm 一律 404（B7 越权拦截）。
        """
        token = str(body.get("token", ""))
        items = body.get("items", [])
        if not isinstance(items, list) or not items:
            raise HTTPException(status_code=400, detail="no items")
        with _resume_lock:
            entry = _resume_candidates.get(token)
            if entry is None or entry.get("user_id") != user.id:
                raise HTTPException(status_code=404, detail="token not found")
            source_id = entry["source_id"]
            _resume_candidates.pop(token, None)
        background.add_task(_confirm_project_questions, source_id, items, embedder_factory)
        return {"status": "importing", "source_id": source_id}

    # --- 手动触发流水线 ---

    @app.post("/api/daily/run")
    def run_daily(background: BackgroundTasks, user: User = Depends(require_owner)):
        background.add_task(_run_daily_safe, daily_runner)
        return {"status": "running"}

    @app.get("/api/daily/latest")
    def daily_latest(user: User = Depends(require_owner)):
        """最近一次流水线运行报告（TaskLog）+ 当前待审核题数（前端轮询「立即更新」完成态）。"""
        from ..models import TaskLog

        with db.get_session() as session:
            log = session.scalars(
                select(TaskLog).order_by(TaskLog.id.desc()).limit(1)
            ).first()
            pending_count = session.scalar(
                select(func.count())
                .select_from(Question)
                .where(Question.reviewed_at.is_(None))
            )
        return {
            "pending_count": pending_count or 0,
            "last_run": (
                {
                    "status": log.status,
                    "fetched_count": log.fetched_count,
                    "generated_count": log.generated_count,
                    "error": log.error,
                    "ran_at": log.ran_at.isoformat(),
                }
                if log is not None
                else None
            ),
        }


    # --- UGC 用户提交 ---

    @app.post("/api/ugc/submissions")
    def ugc_create_submission(body: SubmitBody, background: BackgroundTasks, user: User = Depends(require_user)):
        if not config.ugc.enabled:
            raise HTTPException(status_code=403, detail="UGC 提交已关闭")
        if config.ugc.require_consent and not body.consent:
            raise HTTPException(status_code=400, detail="必须勾选内容授权声明")
        if len(body.content.encode("utf-8")) > config.ugc.max_content_bytes:
            raise HTTPException(status_code=400, detail="内容超过大小限制")
        with db.get_session() as session:
            start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            count = session.scalar(
                select(func.count())
                .select_from(Submission)
                .where(Submission.user_id == user.id, Submission.created_at >= start)
            )
            if count and count >= config.ugc.max_per_user_per_day:
                raise HTTPException(status_code=429, detail="今日提交次数已达上限")
            sub = Submission(
                user_id=user.id,
                kind=body.kind,
                title=(body.title or "")[:200],
                content=body.content,
                consent=body.consent,
                status=SubmissionStatus.pending,
            )
            session.add(sub)
            db.commit(session)
            session.refresh(sub)
            sub_id = sub.id
        background.add_task(_process_ugc_submission, sub_id, llm_factory, embedder_factory)
        return {"id": sub_id, "status": SubmissionStatus.pending.value}

    @app.get("/api/ugc/submissions/me")
    def ugc_my_submissions(user: User = Depends(require_user)):
        with db.get_session() as session:
            rows = session.scalars(
                select(Submission)
                .where(Submission.user_id == user.id)
                .order_by(Submission.id.desc())
            ).all()
        return [_submission_payload(s) for s in rows]

    @app.get("/api/ugc/submissions/{submission_id}")
    def ugc_get_submission(submission_id: int, user: User = Depends(require_user)):
        with db.get_session() as session:
            sub = session.get(Submission, submission_id)
            if sub is None:
                raise HTTPException(status_code=404, detail="submission not found")
            if sub.user_id != user.id and user.role != "owner":
                raise HTTPException(status_code=403, detail="无权查看")
            return _submission_payload(sub)

    @app.get("/api/admin/ugc/submissions")
    def admin_ugc_submissions(status: str = "", user: User = Depends(require_owner)):
        with db.get_session() as session:
            stmt = select(Submission).order_by(Submission.id.desc())
            if status:
                stmt = stmt.where(Submission.status == status)
            rows = session.scalars(stmt).all()
        return [_submission_payload(s) for s in rows]

    @app.post("/api/admin/ugc/submissions/{submission_id}/retry")
    def admin_ugc_retry(submission_id: int, background: BackgroundTasks, user: User = Depends(require_owner)):
        with db.get_session() as session:
            sub = session.get(Submission, submission_id)
            if sub is None or sub.status not in (SubmissionStatus.failed,):
                raise HTTPException(status_code=404, detail="submission not found or not retryable")
            sub.status = SubmissionStatus.pending
            sub.error = ""
            sub.updated_at = datetime.now()
            db.commit(session)
        background.add_task(_process_ugc_submission, submission_id, llm_factory, embedder_factory)
        return {"id": submission_id, "status": SubmissionStatus.pending.value}

    @app.post("/api/admin/ugc/submissions/{submission_id}/remove")
    def admin_ugc_remove(submission_id: int, user: User = Depends(require_owner)):
        """下架标记：保留 submission 溯源；派生题目由 owner 用现有删题入口处理。"""
        with db.get_session() as session:
            sub = session.get(Submission, submission_id)
            if sub is None:
                raise HTTPException(status_code=404, detail="submission not found")
            sub.status = SubmissionStatus.removed
            sub.updated_at = datetime.now()
            db.commit(session)
        return {"id": submission_id, "status": SubmissionStatus.removed.value}

    # --- 题目质量反馈（非举报） ---

    @app.post("/api/feedback")
    def create_feedback(body: FeedbackBody, user: User = Depends(require_user)):
        if not config.feedback.enabled:
            raise HTTPException(status_code=403, detail="反馈已关闭")
        is_dup = body.category == FeedbackCategory.duplicate
        dup_ids = list(dict.fromkeys(int(i) for i in body.duplicate_question_ids if i > 0))
        if is_dup and not dup_ids:
            raise HTTPException(status_code=400, detail="duplicate 反馈必须勾选疑似重复题")
        if is_dup and len(dup_ids) > config.feedback.duplicate_max_select:
            raise HTTPException(status_code=400, detail=f"最多选择 {config.feedback.duplicate_max_select} 道疑似重复题")
        if not is_dup and dup_ids:
            raise HTTPException(status_code=400, detail="仅 duplicate 分类可携带重复题")
        with db.get_session() as session:
            question = session.get(Question, body.question_id)
            if question is None or question.reviewed_at is None:
                raise HTTPException(status_code=404, detail="题目不存在或未公开")
            existing = session.scalars(
                select(QuestionFeedback).where(
                    QuestionFeedback.question_id == body.question_id,
                    QuestionFeedback.user_id == user.id,
                    QuestionFeedback.status == FeedbackStatus.open,
                )
            ).first()
            if existing is not None:
                raise HTTPException(status_code=409, detail="你已反馈过该题，待管理员处理")
            fb = QuestionFeedback(
                question_id=body.question_id,
                user_id=user.id,
                category=body.category,
                duplicate_question_ids=dup_ids if is_dup else [],
                comment=(body.comment or "").strip()[:2000],
                status=FeedbackStatus.open,
            )
            session.add(fb)
            db.commit(session)
            session.refresh(fb)
            return _feedback_payload(fb)

    @app.get("/api/feedback/my")
    def my_feedback(user: User = Depends(require_user)):
        with db.get_session() as session:
            rows = session.scalars(
                select(QuestionFeedback)
                .where(QuestionFeedback.user_id == user.id)
                .order_by(QuestionFeedback.id.desc())
            ).all()
        return [_feedback_payload(f) for f in rows]

    @app.get("/api/admin/feedback")
    def admin_feedback(status: str = "open", user: User = Depends(require_owner)):
        with db.get_session() as session:
            stmt = select(QuestionFeedback).order_by(QuestionFeedback.id.desc())
            if status:
                stmt = stmt.where(QuestionFeedback.status == status)
            rows = session.scalars(stmt).all()
        return [_feedback_payload(f) for f in rows]

    @app.post("/api/admin/feedback/{feedback_id}/resolve")
    def admin_feedback_resolve(feedback_id: int, user: User = Depends(require_owner)):
        with db.get_session() as session:
            fb = session.get(QuestionFeedback, feedback_id)
            if fb is None:
                raise HTTPException(status_code=404, detail="feedback not found")
            fb.status = FeedbackStatus.resolved
            fb.resolved_at = datetime.now()
            db.commit(session)
            return _feedback_payload(fb)

    @app.post("/api/admin/feedback/{feedback_id}/dismiss")
    def admin_feedback_dismiss(feedback_id: int, user: User = Depends(require_owner)):
        with db.get_session() as session:
            fb = session.get(QuestionFeedback, feedback_id)
            if fb is None:
                raise HTTPException(status_code=404, detail="feedback not found")
            fb.status = FeedbackStatus.dismissed
            fb.resolved_at = datetime.now()
            db.commit(session)
            return _feedback_payload(fb)

    @app.get("/api/questions/{question_id}/similar")
    def question_similar(question_id: int, user: User = Depends(require_user)):
        """duplicate 反馈候选：余弦相似度 >= feedback.duplicate_candidate_min_sim 的公开题。"""
        min_sim = config.feedback.duplicate_candidate_min_sim
        return _similar_questions_for_feedback(question_id, min_sim=min_sim)

    return app


def _run_answer_job(
    session_id: int, answer: str, config: AppConfig, llm_factory, embedder_factory
) -> None:
    """后台任务：M11 追问 / M10 判分（含参考检索 + 知识库 RAG 注入）→ 落库；异常记日志，会话保持 active 可重试。"""
    try:
        with db.get_session() as session:
            row = session.get(Session, session_id)
            if row is None:
                logger.warning("answer job skipped: session %s 已被删除", session_id)
                return
            question = session.get(Question, row.question_id)
            if question is None:
                logger.warning("answer job skipped: 题目 %s 已被删除", row.question_id)
                return
            tags = _question_tag_map(session, [question.id]).get(question.id, [])
        question._tags = tags  # 非持久属性：judge/retrieval 读取题标签
        judge_llm = llm_factory("judge")
        knowledge = _knowledge_for(question.stem, tags, embedder_factory, llm=judge_llm)
        if row.status == SessionStatus.finished:
            # 判分失败重试：从 attempts 重建 transcript 直接重判，不再续答
            judgment = judge(
                question,
                _attempts_transcript(session_id),
                config.llm.judge_model,
                judge_llm,
                session_id=session_id,
                reference=_reference_for(question, embedder_factory, row.user_id),
                knowledge=knowledge,
                max_level=_max_level(session_id),
            )
            with db.get_session() as session:
                session.add(judgment)
                db.commit(session)
        elif row.kind == SessionKind.chain:
            max_rounds = max_rounds_for(question.difficulty, config.daily.chain_max_rounds)
            chain = resume(
                session_id,
                question,
                judge_llm,
                judge_model=config.llm.judge_model,
                max_rounds=max_rounds,
                target_level=target_level_for(question.difficulty),
                knowledge=knowledge,
            )
            result = chain.next_round(answer)
            if result["finished"]:
                chain.finish(
                    reference=_reference_for(question, embedder_factory, row.user_id),
                    knowledge=knowledge,
                )
        else:
            judgment = judge(
                question,
                [{"role": "user", "content": answer}],
                config.llm.judge_model,
                judge_llm,
                session_id=session_id,
                reference=_reference_for(question, embedder_factory, row.user_id),
                knowledge=knowledge,
            )
            with db.get_session() as session:
                session.add(judgment)
                row = session.get(Session, session_id)
                row.status = SessionStatus.finished
                row.ended_at = datetime.now()
                db.commit(session)
    except Exception:
        logger.exception("answer job failed for session %s", session_id)
    finally:
        with _inflight_lock:
            _inflight.pop(session_id, None)


_KNOWLEDGE_CANDIDATE_K = 8  # 自评阶段候选块数（比注入多，供 LLM 筛选）
_KNOWLEDGE_CONDENSED_MAX = 1200  # 提炼要点上限

KNOWLEDGE_CONDENSE_PROMPT = """你是知识筛选器。给定一道面试题和若干候选知识块，判断每块与题目是否相关（主题一致、能支撑判分），并对相关块提炼本题要点。

题目：{stem}

候选知识块：
{numbered}

要求：
1. 逐块判断相关性：与题目主题一致、包含可支撑判分/追问的知识点 = 相关；仅字面巧合/主题无关 = 不相关
2. 只保留相关块，从保留块中提炼"本题相关要点"（保留技术结论与关键细节，删除无关内容）
3. 要点中标注每块出处（如 [kamacoder/go/gmp]），总长不超过 {max_chars} 字
4. 全部不相关时 kept 输出空数组

只输出 JSON，不要其他文字：{{"kept": [相关块序号], "condensed": "提炼后的要点"}}"""


def _knowledge_for(stem: str, tags, embedder_factory, k: int = 5, llm=None) -> str | None:
    """判分/追问/复习卷前检索知识库（多查询 + 混合检索 + 相关性自评/精炼）。

    - 检索候选 _KNOWLEDGE_CANDIDATE_K 块 → LLM 自评相关 + 提炼要点（合并一次调用）
    - 全部不相关/无知识/失败 → 返回 None（不注入）；LLM 失败降级为原文拼接（现状）
    """
    try:
        from ..retrieval import format_knowledge, knowledge_search_multi

        tag_list = [t for t in (tags or []) if isinstance(t, str)][:3]
        query_texts = [stem] + tag_list
        embedder = embedder_factory()
        query_vecs = embedder.encode(query_texts)
        chunks = knowledge_search_multi(query_vecs, query_texts, max(k, _KNOWLEDGE_CANDIDATE_K))
        if not chunks:
            return None
        if llm is not None:
            try:
                numbered = "\n".join(
                    f"[{i}]（{title}）\n{content}" for i, (title, content) in enumerate(chunks, 1)
                )
                parsed = llm.complete(
                    [{"role": "user", "content": KNOWLEDGE_CONDENSE_PROMPT.format(
                        stem=stem, numbered=numbered, max_chars=_KNOWLEDGE_CONDENSED_MAX,
                    )}],
                    json_schema={},
                )
                kept = parsed.get("kept", []) if isinstance(parsed, dict) else []
                condensed = parsed.get("condensed") if isinstance(parsed, dict) else ""
                kept_ids = [
                    i for i in kept if isinstance(i, int) and 1 <= i <= len(chunks)
                ]
                if kept_ids and isinstance(condensed, str) and condensed.strip():
                    return condensed[:_KNOWLEDGE_CONDENSED_MAX * 2]
                if kept_ids and not (isinstance(condensed, str) and condensed.strip()):
                    # 判相关但未提炼：退回保留块的原文拼接
                    return format_knowledge([chunks[i - 1] for i in kept_ids])
                if isinstance(parsed, dict) and not kept_ids:
                    return None  # 全部不相关：不注入
            except Exception as e:
                logger.warning("knowledge condense failed, fallback to raw: %s", e)
        return format_knowledge(chunks)
    except Exception as e:
        logger.warning("knowledge retrieval failed, skip injection: %s", e)
        return None


def _reference_for(question, embedder_factory, user_id: int) -> str | None:
    """判分前检索库内同类高分回答片段；只检索该用户自己的高分回答（跨用户泄漏修复）。

    无候选/失败返回 None（judge 降级不注入）。
    """
    try:
        from ..retrieval import HIGH_SCORE, build_reference

        with db.get_session() as session:
            candidates = list(
                session.scalars(
                    select(Question)
                    .join(Session, Session.question_id == Question.id)
                    .join(Judgment, Judgment.session_id == Session.id)
                    .where(Judgment.total_score >= HIGH_SCORE)
                    .where(Session.user_id == user_id)
                    .distinct()
                )
            )
        if not candidates:
            return None
        tag_map = {}
        with db.get_session() as session:
            tag_map = _question_tag_map(session, [q.id for q in candidates])
        for q in candidates:
            q._tags = tag_map.get(q.id, [])  # 非持久属性：检索评分用
        return build_reference(question, candidates, embedder=embedder_factory())
    except Exception as e:
        logger.warning("reference retrieval failed, judge without reference: %s", e)
        return None


def _attempts_transcript(session_id: int) -> list[dict]:
    with db.get_session() as session:
        attempts = session.scalars(
            select(Attempt)
            .where(Attempt.session_id == session_id)
            .order_by(Attempt.round_no)
        ).all()
    transcript = []
    for a in attempts:
        transcript.append({"role": "user", "content": a.answer_text})
        if a.feedback_text:
            item: dict = {"role": "interviewer", "content": a.feedback_text}
            if a.level is not None:
                item["level"] = a.level
            transcript.append(item)
    return transcript


def _run_daily_safe(runner) -> None:
    try:
        runner()
    except Exception:
        logger.exception("daily pipeline run failed")


def _default_llm_factory(config: AppConfig):
    def factory(role: str):
        from ..llm.llm_client import LLMClient

        if role == "judge":
            model = config.llm.judge_model
        else:
            model = config.llm.generate_model
        return LLMClient(
            model,
            config.llm.base_url,
            secret_value(config.llm.api_key_env),
        )

    return factory


def _is_direct_format(content: str) -> bool:
    """格式识别：全部非空行匹配 Q:/列表行且每行 <120 字符 → 直接入库模式。"""
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    if not lines or not all(len(l) < 120 for l in lines):
        return False
    for l in lines:
        if re.match(r"^Q[:：]", l) or re.match(r"^[-*]\s", l):
            continue
        return False
    return True


def _parse_direct_questions(content: str) -> list[str]:
    """Q:/列表行 → 题干列表（去前缀、空行跳过）。"""
    stems = []
    for l in content.splitlines():
        l = l.strip()
        if not l:
            continue
        m = re.match(r"^Q[:：]\s*", l)
        if m:
            l = l[m.end():]
        else:
            l = re.sub(r"^[-*]\s*", "", l)
        if l:
            stems.append(l)
    return stems


def _insert_direct_questions(content: str) -> tuple[Source, int]:
    """直接模式入库：建 manual source + knowledge 题（难度 1、默认 criteria）；与库内归一化哈希去重。

    source_hash 唯一约束：内容相同的重复上传复用已有 source（题已全去重）。
    """
    from ..pipeline.dedup import _dedup_hash_only
    from ..pipeline.generate import DEFAULT_BAD_CRITERIA, DEFAULT_GOOD_CRITERIA

    stems = _parse_direct_questions(content)
    source_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with db.get_session() as session:
        source = session.scalars(
            select(Source).where(Source.source_hash == source_hash)
        ).first()
        if source is None:
            source = Source(
                type=SourceType.manual,
                title="用户上传",
                cleaned_text=content,
                source_hash=source_hash,
            )
            session.add(source)
            try:
                db.commit(session)
            except DuplicateSource:  # 并发相同内容上传：唯一约束拦截，复用已有 source
                source = session.scalars(
                    select(Source).where(Source.source_hash == source_hash)
                ).first()
            else:
                session.refresh(source)
        existing = [
            Question(stem=stem)
            for stem in session.scalars(select(Question.stem)).all()
        ]
    kept = _dedup_hash_only([Question(stem=s) for s in stems], existing)
    if kept:
        with db.get_session() as session:
            for q in kept:
                session.add(
                    Question(
                        source_id=source.id,
                        type=QuestionType.knowledge,
                        stem=q.stem,
                        difficulty=1,
                        good_criteria=list(DEFAULT_GOOD_CRITERIA),
                        bad_criteria=list(DEFAULT_BAD_CRITERIA),
                    )
                )
            db.commit(session)
    return source, len(kept)


def _set_suggestions(row: Question, tags: list[str], difficulty: int) -> None:
    """写入审核建议快照（审核页预填）；不额外调 LLM，直接映射已有标签/难度。"""
    from ..tags import category_of_tag

    row.suggested_tags = list(tags)
    row.suggested_difficulty = difficulty
    row.suggested_category = category_of_tag(tags[0]) if tags else ""
    row.suggested_at = datetime.now()


def _tag_direct_questions(source_id: int, llm_factory) -> None:
    """直接模式后台校验 + 补标签/难度（≤20 题一次调用）：verdict=delete 删除、rewrite 改写题干、keep 仅补标签难度。"""
    try:
        from ..difficulty import DIFFICULTY_SCALE_TEXT
        from ..tags import MAX_TAGS, TAG_VOCABULARY, tag_vocab_text

        with db.get_session() as session:
            questions = list(
                session.scalars(
                    select(Question)
                    .where(Question.source_id == source_id)
                    .order_by(Question.id)
                )
            )
        if not questions or len(questions) > 20:
            return
        stems = [q.stem for q in questions]
        numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(stems))
        prompt = (
            "为以下题目各选 1-{max_tags} 个最相关的标签（必须从词表中选）并按难度分级标准标注难度；"
            "同时判断每题是否合格面试题并给出处理方式：\n{vocab}\n\n"
            "难度分级标准：\n{difficulty_scale}\n\n"
            "verdict 判定标准：\n"
            "- delete：非技术面试题（面试者反问如「贵公司主要做什么业务」、闲聊、流程题如「还有什么想问的」）；"
            "手撕算法题（白板手写代码题，如「手写快排」「实现一个 LRU 缓存」，本系统只收录口述问答/场景设计类）\n"
            "- rewrite：技术考点但表述不自然（口语化/含糊/一题多问），new_stem 给出自然改写（保持语义，"
            "\"请解释/请描述/请设计/为什么…\"句式，一问一题）\n"
            "- keep：表述自然的技术题，new_stem 留空\n\n"
            "题目列表：\n{numbered}\n\n"
            '只输出 JSON：{{"items": [{{"stem": "题干原文", "tags": [标签], "difficulty": 1-5 整数, '
            '"verdict": "keep|rewrite|delete", "new_stem": "改写后题干或空"}}]}}'
        ).format(
            max_tags=MAX_TAGS,
            vocab=tag_vocab_text(),
            difficulty_scale=DIFFICULTY_SCALE_TEXT,
            numbered=numbered,
        )
        parsed = llm_factory("generate").complete(
            [{"role": "user", "content": prompt}], json_schema={}
        )
        items = parsed.get("items", []) if isinstance(parsed, dict) else []
        by_stem = {}
        for item in items:
            if isinstance(item, dict):
                s = item.get("stem")
                tags = item.get("tags", [])
                if isinstance(s, str) and isinstance(tags, list):
                    verdict = item.get("verdict")
                    if verdict not in ("delete", "rewrite", "keep"):
                        verdict = "keep"
                    new_stem = item.get("new_stem")
                    by_stem[s] = {
                        "tags": [
                            t for t in tags if isinstance(t, str) and t in TAG_VOCABULARY
                        ][:MAX_TAGS],
                        "difficulty": _clamp_difficulty(item.get("difficulty")),
                        "verdict": verdict,
                        "new_stem": (
                            new_stem.strip()
                            if isinstance(new_stem, str) and new_stem.strip()
                            else None
                        ),
                    }
        if not by_stem:
            return
        with db.get_session() as session:
            for q in questions:
                if q.stem not in by_stem:
                    continue
                info = by_stem[q.stem]
                row = session.get(Question, q.id)
                if row is None:
                    continue
                if info["verdict"] == "delete":
                    if _question_inflight(session, q.id):
                        logger.info("质检跳过删除：题目 %s 正在判分", q.id)
                        continue
                    sessions = session.scalars(
                        select(Session).where(Session.question_id == q.id)
                    ).all()
                    for s in sessions:
                        session.delete(s)
                    session.delete(row)
                elif info["verdict"] == "rewrite" and info["new_stem"]:
                    row.stem = info["new_stem"]
                    _set_question_tags(session, row.id, info["tags"])
                    row.difficulty = info["difficulty"]
                    _set_suggestions(row, info["tags"], info["difficulty"])
                else:
                    _set_question_tags(session, row.id, info["tags"])
                    row.difficulty = info["difficulty"]
                    _set_suggestions(row, info["tags"], info["difficulty"])
            db.commit(session)
    except Exception as e:
        logger.warning("tag direct questions failed (留空): %s", e)


def _import_facejing(content: str, filename: str) -> Source:
    """面经模式：内容写入 data/uploads/ 并作为 manual 源导入；source 入库后删除文件。"""
    from ..crawler.importer import import_file

    uploads = Path("data/uploads")
    uploads.mkdir(parents=True, exist_ok=True)
    path = uploads / f"{hashlib.sha256(content.encode('utf-8')).hexdigest()[:12]}.md"
    path.write_text(content, encoding="utf-8")
    try:
        return import_file(path, "manual")
    finally:
        path.unlink(missing_ok=True)


def _import_resume(content: str) -> Source:
    """简历模式：内容写入 data/uploads/resume_*.md（文件名前缀识别 resume 类型）并导入；读后删。"""
    from ..crawler.importer import import_file

    uploads = Path("data/uploads")
    uploads.mkdir(parents=True, exist_ok=True)
    path = uploads / f"resume_{hashlib.sha256(content.encode('utf-8')).hexdigest()[:12]}.md"
    path.write_text(content, encoding="utf-8")
    try:
        return import_file(path, "resume")
    finally:
        path.unlink(missing_ok=True)


def _clamp_resume_count(value) -> int:
    try:
        return max(1, min(10, int(value)))
    except (TypeError, ValueError):
        return 5


def _start_resume_parse(source_id: int, count: int, llm_factory, user_id: int) -> str:
    """后台线程解析简历 → project 候选题（内存 token 态，重启丢失可接受）；token 绑定创建者 user_id。"""
    from ..pipeline.generate import generate_project_questions

    token = secrets.token_hex(8)
    with _resume_lock:
        _resume_candidates[token] = {
            "status": "running",
            "source_id": source_id,
            "user_id": user_id,
            "questions": [],
            "error": "",
        }

    def run() -> None:
        try:
            with db.get_session() as session:
                source = session.get(Source, source_id)
            questions = generate_project_questions(source, count, llm_factory("generate"))
            with _resume_lock:
                entry = _resume_candidates.get(token)
                if entry is not None:
                    entry["status"] = "done"
                    entry["questions"] = questions
        except Exception as e:
            logger.warning("resume parse %s failed: %s", token, e)
            with _resume_lock:
                entry = _resume_candidates.get(token)
                if entry is not None:
                    entry["status"] = "failed"
                    entry["error"] = _friendly_upload_error(e)

    threading.Thread(target=run, daemon=True).start()
    return token


def _friendly_upload_error(e: Exception) -> str:
    """异步解析异常 → 中文友好文案（避免向用户透传原始 stderr 英文报错）。"""
    text = str(e)
    if "LibreOffice" in text:
        return "DOC 转换失败：服务器未安装 LibreOffice，请转用 PDF/MD/TXT 格式"
    if "MinerU" in text or "mineru" in text:
        return "PDF/Word 解析失败（解析器异常），请重试或转用文本格式"
    if "timed out" in text or "Timeout" in text:
        return "解析超时（超过 20 分钟），请重试或改用更小文件"
    logger.warning("upload parse failed: %s", text)
    return "解析失败，请重试或转用文本格式"


def _start_binary_upload(
    filename: str, data: bytes, up_type, count, llm_factory, embedder_factory, user_id: int
) -> str:
    """二进制文件（pdf/docx/doc）后台任务：提取文本 → 按 type 走现有流程。

    BackgroundTasks 是请求作用域，线程内直接同步调用后台任务函数（均自带 try/except）。
    """
    token = secrets.token_hex(8)
    with _upload_lock:
        _upload_tasks[token] = {"status": "parsing"}

    def run() -> None:
        try:
            with _parse_semaphore:  # 只锁 extract_text（MinerU 内存大户），LLM 生成不排队
                text = extract_text(filename, data)
            if up_type == "resume":
                count_n = _clamp_resume_count(count)
                source = _import_resume(text)
                cand_token = _start_resume_parse(source.id, count_n, llm_factory, user_id)
                with _upload_lock:
                    _upload_tasks[token] = {
                        "status": "done",
                        "mode": "resume",
                        "source_id": source.id,
                        "candidates_token": cand_token,
                    }
            elif up_type == "direct" or (
                up_type == "auto" and _is_direct_format(text)
            ):
                source, n = _insert_direct_questions(text)
                _tag_direct_questions(source.id, llm_factory)
                with _upload_lock:
                    _upload_tasks[token] = {
                        "status": "done",
                        "mode": "direct",
                        "count": n,
                        "source_id": source.id,
                    }
            else:
                source = _import_facejing(text, filename)
                _generate_uploaded_source(source.id, llm_factory, embedder_factory)
                with _upload_lock:
                    _upload_tasks[token] = {
                        "status": "done",
                        "mode": "facejing",
                        "source_id": source.id,
                    }
        except Exception as e:
            logger.warning("binary upload %s failed: %s", token, e)
            with _upload_lock:
                _upload_tasks[token] = {"status": "failed", "error": _friendly_upload_error(e)}

    threading.Thread(target=run, daemon=True).start()
    return token


def _confirm_project_questions(source_id: int, items: list[dict], embedder_factory) -> None:
    """简历候选确认入库：构造 project 题（保留候选 tags/difficulty）→ 与库内 embedding 去重 → 入库。"""
    try:
        from ..pipeline.dedup import dedup
        from ..pipeline.generate import DEFAULT_BAD_CRITERIA, DEFAULT_GOOD_CRITERIA

        valid = [
            i
            for i in items
            if isinstance(i, dict) and str(i.get("stem", "")).strip()
        ]
        if not valid:
            logger.warning("confirm: no valid stems for source %s", source_id)
            return
        with db.get_session() as session:
            pool = list(session.scalars(select(Question)))
        questions = [
            Question(
                source_id=source_id,
                type=QuestionType.project,
                stem=str(i["stem"]).strip(),
                difficulty=_clamp_difficulty(i.get("difficulty")),
                good_criteria=list(DEFAULT_GOOD_CRITERIA),
                bad_criteria=list(DEFAULT_BAD_CRITERIA),
            )
            for i in valid
        ]
        for q, i in zip(questions, valid):
            tags = [
                t for t in (i.get("tags") or [])
                if isinstance(t, str) and t in TAG_VOCABULARY
            ][:5]
            _set_suggestions(q, tags, q.difficulty)
        kept = dedup(questions, pool, embedder_factory())
        with db.get_session() as session:
            session.add_all(kept)
            db.commit(session)
            # 候选标签（词表命中）写 question_tags 关联
            tags_by_stem = {
                str(i["stem"]).strip(): [
                    t for t in (i.get("tags") or [])
                    if isinstance(t, str) and t in TAG_VOCABULARY
                ][:5]
                for i in valid
            }
            for q in kept:
                _set_question_tags(session, q.id, tags_by_stem.get(q.stem, []))
            db.commit(session)
        logger.info("confirmed %d project questions for source %s", len(kept), source_id)
    except Exception as e:
        logger.warning("confirm project questions failed for source %s: %s", source_id, e)


def _generate_uploaded_source(source_id: int, llm_factory, embedder_factory) -> None:
    """面经模式后台生成该源（单源，不受 36 上限）。"""
    try:
        from ..pipeline.daily import generate_source_immediately

        n = generate_source_immediately(
            source_id, llm_factory("generate"), embedder_factory()
        )
        logger.info("uploaded source %s generated %d questions", source_id, n)
    except Exception as e:
        logger.warning("uploaded source %s generate failed: %s", source_id, e)


def _run_daily_pipeline(config: AppConfig, llm_factory):
    from ..pipeline.daily import default_sources, run_daily

    return run_daily(
        # 复用共享单例：每次 new Embedder 会与答题/上传中的单例并存双份 ~2.3GB（OOM 风险）
        config, default_sources(config), llm_factory("generate"), _default_embedder_factory()()
    )


def _display_name_for(email: str, session) -> str:
    """显示名 = 邮箱 @ 前缀；重名自动加 -2/-3 后缀。"""
    base = email.split("@")[0][:20] or "user"
    name, n = base, 2
    while session.scalars(select(User).where(User.username == name).limit(1)).first() is not None:
        name = f"{base}-{n}"
        n += 1
    return name


def _seed_owner_if_configured() -> None:
    """库无 owner 且 .env 配置了 OWNER_EMAIL/OWNER_PASSWORD 时自动创建 owner（幂等）。

    防"首个注册者=owner"抢注窗口：新库/重建/导入覆盖后首次启动即锁定管理员。
    未配置时回退现状（首注册者=owner）并 WARN。
    """
    owner_email = os.environ.get("OWNER_EMAIL", "").strip().lower()
    owner_pass = os.environ.get("OWNER_PASSWORD", "")
    if not owner_email or not owner_pass:
        logger.warning("OWNER_EMAIL/OWNER_PASSWORD 未配置：空库时首个注册用户将成为管理员")
        return
    from ..auth import hash_password

    with db.get_session() as session:
        if session.scalars(select(User).where(User.role == "owner").limit(1)).first() is not None:
            return
        if session.scalars(select(User).where(User.email == owner_email)).first() is not None:
            return
        session.add(User(
            email=owner_email,
            username=_display_name_for(owner_email, session),
            password_hash=hash_password(owner_pass),
            role="owner",
        ))
        db.commit(session)
        logger.info("seeded owner user %s from OWNER_* env", owner_email)


def _db_url() -> str:
    from ..main import DEFAULT_DB_URL

    return f"sqlite:///{DEFAULT_DB_URL}"


def _tags_exists(tag: str):
    """题含指定标签的 EXISTS 子查询（question_tags 关联表 JOIN tags）。"""
    from ..models import QuestionTag, Tag

    return Question.id.in_(
        select(QuestionTag.question_id)
        .join(Tag, QuestionTag.tag_id == Tag.id)
        .where(Tag.name == tag)
    )


def _tags_contains(keyword: str):
    """题含模糊匹配标签的 EXISTS 子查询（标签名 ilike，大小写不敏感）。"""
    from ..models import QuestionTag, Tag

    return Question.id.in_(
        select(QuestionTag.question_id)
        .join(Tag, QuestionTag.tag_id == Tag.id)
        .where(Tag.name.ilike(f"%{keyword}%"))
    )


def _set_question_tags(session, question_id: int, names: list[str]) -> None:
    """覆写题目标签（db.set_question_tags 转发，调用处保持统一）。"""
    db.set_question_tags(session, question_id, names)


def _tag_ids_by_name(session, names: list[str]) -> list[int]:
    """词表标签名 → id 映射（question_tags 写入用）。"""
    from ..models import Tag

    if not names:
        return []
    rows = session.scalars(select(Tag).where(Tag.name.in_(names))).all()
    return [t.id for t in rows]


def _question_tag_map(session, qids: list[int]) -> dict[int, list[str]]:
    """批量取题-标签名映射（question_tags → tags），供列表接口组装 tags 字段。"""
    from ..models import QuestionTag, Tag

    if not qids:
        return {}
    rows = session.execute(
        select(QuestionTag.question_id, Tag.name)
        .join(Tag, QuestionTag.tag_id == Tag.id)
        .where(QuestionTag.question_id.in_(qids))
        .order_by(QuestionTag.question_id, Tag.name)
    ).all()
    tag_map: dict[int, list[str]] = {}
    for qid, name in rows:
        tag_map.setdefault(qid, []).append(name)
    return tag_map


def _review_questions(session, tag: str, limit: int = 30):
    """按标签查复习题目；该标签无题时回退到同分类（TAG_CATEGORIES）标签的题目。

    返回 (questions, fallback, fallback_category)：fallback=True 表示已回退，
    前端据此提示「该标签暂无题目，展示相关分类题目」（判分 weak_tags 可能指向
    题库无该标签的题，此前复习列表空 + 复习卷死胡同）。
    """
    questions = list(
        session.scalars(
            select(Question)
            .where(_tags_exists(tag), Question.reviewed_at.is_not(None))
            .order_by(Question.created_at.desc())
            .limit(limit)
        )
    )
    if questions:
        return questions, False, None
    category = next(
        (name for name, tags in TAG_CATEGORIES if tag in tags), None
    )
    if category is None:
        return questions, False, None
    cat_tags = list(dict(TAG_CATEGORIES)[category])
    questions = list(
        session.scalars(
            select(Question)
            .where(or_(*(_tags_exists(t) for t in cat_tags)), Question.reviewed_at.is_not(None))
            .order_by(Question.created_at.desc())
            .limit(limit)
        )
    )
    return questions, True, category


def _has_finished_session(session, question_id: int, user_id: int) -> bool:
    return (
        session.scalars(
            select(Session)
            .where(
                Session.question_id == question_id,
                Session.user_id == user_id,
                Session.status == SessionStatus.finished,
            )
            .limit(1)
        ).first()
        is not None
    )


def _active_session_id(session, question_id: int, user_id: int) -> int | None:
    row = session.scalars(
        select(Session)
        .where(
            Session.question_id == question_id,
            Session.user_id == user_id,
            Session.status == SessionStatus.active,
        )
        .order_by(Session.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    texts = session.scalars(
        select(Attempt.answer_text).where(Attempt.session_id == row.id)
    ).all()
    if not any((t or "").strip() for t in texts):
        return None
    return row.id


def _latest_judgment_by_question(
    session, user_id: int
) -> dict[int, tuple[Session, Judgment]]:
    """每题最新 finished session 及其最新 judgment（按 Session.id 倒序首条即最新），按用户隔离。"""
    rows = session.execute(
        select(Session, Judgment)
        .join(Judgment, Judgment.session_id == Session.id)
        .where(
            Session.status == SessionStatus.finished,
            Session.user_id == user_id,
        )
        .order_by(Session.id.desc())
    ).all()
    latest: dict[int, tuple[Session, Judgment]] = {}
    for s, j in rows:
        if s.question_id not in latest:
            latest[s.question_id] = (s, j)
    return latest


def _latest_judgment(session, session_id: int) -> Judgment | None:
    return session.scalars(
        select(Judgment)
        .where(Judgment.session_id == session_id)
        .order_by(Judgment.id.desc())
        .limit(1)
    ).first()


def _count_attempts(session, session_id: int) -> int:
    return len(
        session.scalars(
            select(Attempt).where(Attempt.session_id == session_id)
        ).all()
    )


def _latest_followup(session, session_id: int) -> str | None:
    """该会话最新一轮面试官的追问消息（attempts.feedback_text）；无则 None。"""
    row = session.scalars(
        select(Attempt)
        .where(Attempt.session_id == session_id)
        .order_by(Attempt.round_no.desc())
        .limit(1)
    ).first()
    return row.feedback_text if row and row.feedback_text else None


def _max_level(session_id: int) -> int | None:
    """该会话深挖追问探到的最大层级（attempts.level）；无层级数据返回 None。"""
    with db.get_session() as session:
        levels = session.scalars(
            select(Attempt.level).where(Attempt.session_id == session_id)
        ).all()
    valid = [lvl for lvl in levels if lvl is not None]
    return max(valid) if valid else None


def _judgment_payload(j: Judgment) -> dict:
    return {
        "scores": j.scores,
        "total_score": j.total_score,
        "review": j.review,
        "reference_answer": j.reference_answer,
        "weak_tags": j.weak_tags,
        "model": j.model,
        "reference_used": bool(j.scores.get("reference_used")),
        "created_at": j.created_at.isoformat(),
    }


# --- UGC / feedback helpers ---


def _submission_payload(sub: Submission) -> dict:
    return {
        "id": sub.id,
        "kind": sub.kind.value if hasattr(sub.kind, "value") else str(sub.kind),
        "title": sub.title,
        "status": sub.status.value if hasattr(sub.status, "value") else str(sub.status),
        "source_id": sub.source_id,
        "error": sub.error,
        "created_at": sub.created_at.isoformat(),
    }


def _feedback_payload(fb: QuestionFeedback) -> dict:
    return {
        "id": fb.id,
        "question_id": fb.question_id,
        "category": fb.category.value if hasattr(fb.category, "value") else str(fb.category),
        "duplicate_question_ids": list(fb.duplicate_question_ids or []),
        "comment": fb.comment,
        "status": fb.status.value if hasattr(fb.status, "value") else str(fb.status),
        "created_at": fb.created_at.isoformat(),
        "resolved_at": fb.resolved_at.isoformat() if fb.resolved_at else None,
    }


def _process_ugc_submission(submission_id: int, llm_factory, embedder_factory) -> None:
    """后台处理 UGC 提交：pending → processing → completed/failed。

    生成出的题目默认 reviewed_at=NULL，进入现有 owner 题目审核门禁。
    """
    try:
        with db.get_session() as session:
            sub = session.get(Submission, submission_id)
            if sub is None or sub.status != SubmissionStatus.pending:
                return
            sub.status = SubmissionStatus.processing
            sub.updated_at = datetime.now()
            db.commit(session)
            kind = sub.kind.value if hasattr(sub.kind, "value") else str(sub.kind)
            content = sub.content
            title = sub.title or "ugc.md"
        source_id = None
        if kind == SubmissionKind.facejing.value:
            source = _import_facejing(content, title)
            source_id = source.id
            _generate_uploaded_source(source.id, llm_factory, embedder_factory)
        elif kind == SubmissionKind.resume.value:
            source = _import_resume(content)
            source_id = source.id
            _generate_uploaded_source(source.id, llm_factory, embedder_factory)
        elif kind == SubmissionKind.direct.value:
            source, _count = _insert_direct_questions(content)
            source_id = source.id
            _tag_direct_questions(source.id, llm_factory)
        else:
            raise ValueError(f"unsupported submission kind: {kind}")
        with db.get_session() as session:
            sub = session.get(Submission, submission_id)
            if sub is not None:
                sub.status = SubmissionStatus.completed
                sub.source_id = source_id
                sub.error = ""
                sub.updated_at = datetime.now()
                db.commit(session)
        logger.info("ugc submission %s completed source=%s", submission_id, source_id)
    except Exception as e:
        logger.warning("ugc submission %s failed: %s", submission_id, e)
        with db.get_session() as session:
            sub = session.get(Submission, submission_id)
            if sub is not None:
                sub.status = SubmissionStatus.failed
                sub.error = str(e)[:500]
                sub.updated_at = datetime.now()
                db.commit(session)


def _similar_questions_for_feedback(question_id: int, min_sim: float = 0.78, limit: int = 50) -> list[dict]:
    """返回与指定题余弦相似度 >= min_sim 的公开(reviewed)题候选，按相似度降序。"""
    import numpy as np

    from ..embed import from_bytes

    with db.get_session() as session:
        target = session.get(Question, question_id)
        if target is None or not target.embedding:
            return []
        others = session.scalars(
            select(Question)
            .where(Question.id != question_id, Question.reviewed_at.isnot(None), Question.embedding.isnot(None))
        ).all()
        if not others:
            return []
        vec = from_bytes(target.embedding)
        vecs = np.stack([from_bytes(o.embedding) for o in others])
        sims = vec @ vecs.T
        hits = [
            (float(sims[i]), others[i])
            for i in range(len(others))
            if sims[i] >= min_sim
        ]
    hits.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "id": q.id,
            "stem": q.stem,
            "difficulty": q.difficulty,
            "sim": round(sim, 4),
        }
        for sim, q in hits[:limit]
    ]