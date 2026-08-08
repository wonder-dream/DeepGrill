"""M14 Web 路由层：请求编排与校验，业务委托下层模块；LLM 调用放后台任务 + 前端轮询。

- AppError → 500 统一 JSON；未知异常 → 500 + 日志
- 并发作答同一会话 → 409（in-flight 集合）
- 判分失败：结果接口返回 status="failed"，可再次作答重试
"""
import hashlib
import logging
import re
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import cast, select, String

from .. import db
from ..config import AppConfig, secret_value
from ..embed import Embedder
from ..errors import AppError
from ..judge.chain import ChainSession, resume
from ..judge.judge import STATUS_FAILED, judge
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
)
from ..tags import TAG_CATEGORIES, TAG_VOCABULARY
from . import static_dir

logger = logging.getLogger(__name__)

_inflight: set[int] = set()
_inflight_lock = threading.Lock()


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
    embedder_factory = embedder_factory or (lambda: Embedder())
    daily_runner = daily_runner or (
        lambda: _run_daily_pipeline(config, llm_factory)
    )

    app = FastAPI(title="InterviewAssistant")

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(Path(static_dir) / "index.html")

    if enable_scheduler:
        from .. import scheduler

        @app.on_event("startup")
        def startup():
            db.init_db(_db_url())
            scheduler.start_scheduler(config, daily_runner)

        @app.on_event("shutdown")
        def shutdown():
            scheduler.shutdown()

    @app.exception_handler(AppError)
    async def app_error_handler(request, exc: AppError):
        logger.warning("request failed: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request, exc: Exception):
        logger.exception("unhandled error on %s", request.url)
        return JSONResponse(status_code=500, content={"error": "internal error"})

    # --- 今日题目与题目详情 ---

    @app.get("/api/today")
    def today_questions():
        with db.get_session() as session:
            questions = db.list_today_questions(session)
            payload = []
            for q in questions:
                done = _has_finished_session(session, q.id)
                active = _active_session_id(session, q.id)
                payload.append(
                    {
                        "id": q.id,
                        "stem": q.stem,
                        "type": q.type.value,
                        "difficulty": q.difficulty,
                        "tags": q.tags,
                        "done": done,
                        "active_session_id": active,
                    }
                )
            return payload

    @app.get("/api/questions/{question_id}")
    def question_detail(question_id: int):
        with db.get_session() as session:
            q = session.get(Question, question_id)
            if q is None:
                raise HTTPException(status_code=404, detail="question not found")
            return {
                "id": q.id,
                "stem": q.stem,
                "type": q.type.value,
                "difficulty": q.difficulty,
                "tags": q.tags,
                "good_criteria": q.good_criteria,
                "bad_criteria": q.bad_criteria,
            }

    @app.get("/api/questions/{question_id}/history")
    def question_history(question_id: int):
        """历史详情：该题全部会话（倒序）+ 各自问答记录与判分（D18 复盘）。"""
        with db.get_session() as session:
            q = session.get(Question, question_id)
            if q is None:
                raise HTTPException(status_code=404, detail="question not found")
            rows = session.scalars(
                select(Session)
                .where(Session.question_id == question_id)
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
                "tags": q.tags,
                "attempts": attempts,
            }

    # --- 会话 ---

    @app.post("/api/sessions")
    def create_session(body: dict):
        question_id = body.get("question_id")
        kind = body.get("kind", "chain")
        if kind not in {k.value for k in SessionKind}:
            raise HTTPException(status_code=400, detail=f"invalid kind: {kind}")
        with db.get_session() as session:
            if session.get(Question, question_id) is None:
                raise HTTPException(status_code=404, detail="question not found")
            existing = session.scalars(
                select(Session)
                .where(
                    Session.question_id == question_id,
                    Session.status == SessionStatus.active,
                )
                .order_by(Session.id.desc())
                .limit(1)
            ).first()
            if existing is not None:
                return {"session_id": existing.id, "status": "active", "resumed": True}
            row = Session(question_id=question_id, kind=SessionKind(kind))
            session.add(row)
            db.commit(session)
            session.refresh(row)
        return {"session_id": row.id, "status": "active", "resumed": False}

    @app.post("/api/sessions/{session_id}/answer")
    def submit_answer(session_id: int, body: dict, background: BackgroundTasks):
        answer = str(body.get("answer", "")).strip()
        if len(answer) < 2:
            raise HTTPException(status_code=400, detail="answer too short")
        with db.get_session() as session:
            row = session.get(Session, session_id)
            if row is None:
                raise HTTPException(status_code=404, detail="session not found")
            latest = _latest_judgment(session, session_id)
            if row.status == SessionStatus.finished and not (
                latest is not None and latest.status == STATUS_FAILED
            ):
                raise HTTPException(status_code=409, detail="session already finished")
        with _inflight_lock:
            if session_id in _inflight:
                raise HTTPException(status_code=409, detail="session is being judged")
            _inflight.add(session_id)
        background.add_task(
            _run_answer_job, session_id, answer, config, llm_factory, embedder_factory
        )
        return {"status": "judging"}

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: int):
        """删除单次作答（含回答轮次与判分）；题目保留。"""
        with _inflight_lock:
            if session_id in _inflight:
                raise HTTPException(status_code=409, detail="session is being judged")
        with db.get_session() as session:
            row = session.get(Session, session_id)
            if row is None:
                raise HTTPException(status_code=404, detail="session not found")
            session.delete(row)  # cascade 级联删 attempts/judgments
            db.commit(session)
        return {"deleted": True}

    @app.delete("/api/questions/{question_id}/history")
    def delete_question_history(question_id: int):
        """删除该题全部作答记录（会话/回答/判分）；题目本身保留。"""
        with db.get_session() as session:
            if session.get(Question, question_id) is None:
                raise HTTPException(status_code=404, detail="question not found")
            rows = session.scalars(
                select(Session).where(Session.question_id == question_id)
            ).all()
            for row in rows:
                session.delete(row)
            db.commit(session)
        return {"deleted": True}

    @app.get("/api/sessions/{session_id}")
    def session_status(session_id: int):
        with db.get_session() as session:
            row = session.get(Session, session_id)
            if row is None:
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
            if session_id in _inflight:
                payload["status"] = "judging"
            else:
                payload["rounds_done"] = _count_attempts(session, session_id)
                payload["followup"] = _latest_followup(session, session_id)
                payload["transcript"] = _attempts_transcript(session_id)
            return payload

    @app.get("/api/sessions/{session_id}/resume")
    def resume_session(session_id: int):
        with db.get_session() as session:
            row = session.get(Session, session_id)
            if row is None:
                raise HTTPException(status_code=404, detail="session not found")
            if row.status == SessionStatus.finished:
                raise HTTPException(status_code=409, detail="session already finished")
            question = session.get(Question, row.question_id)
            if question is None:
                raise HTTPException(status_code=404, detail="question not found")
            chain = resume(session_id, question, llm_factory("judge"),
                           judge_model=config.llm.judge_model,
                           max_rounds=config.daily.chain_max_rounds)
            return {
                "session_id": row.id,
                "question_id": row.question_id,
                "stem": question.stem,
                "rounds_done": chain.rounds_done,
                "max_rounds": config.daily.chain_max_rounds,
            }

    # --- 历史（按被选为今日题目的日期分组，未作答也展示） ---

    @app.get("/api/history")
    def history(qtype: str | None = None):
        with db.get_session() as session:
            stmt = (
                select(Question)
                .where(Question.selected_at.is_not(None))
                .order_by(Question.selected_at.desc())
            )
            if qtype:
                from ..models import QuestionType

                stmt = stmt.where(Question.type == QuestionType(qtype))
            questions = list(session.scalars(stmt))
            latest = _latest_judgment_by_question(session)

            groups: dict[str, list[dict]] = {}
            for q in questions:
                item = {
                    "question_id": q.id,
                    "stem": q.stem,
                    "type": q.type.value,
                    "difficulty": q.difficulty,
                    "tags": q.tags,
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
                day = q.selected_at.date().isoformat()
                groups.setdefault(day, []).append(item)
            return [
                {"date": day, "items": groups[day]}
                for day in sorted(groups, reverse=True)
            ]

    # --- 题库浏览（分页 + 筛选） ---

    @app.get("/api/bank")
    def bank_questions(
        page: int = 1,
        page_size: int = 20,
        type: str | None = None,
        category: str | None = None,
    ):
        """全部题目分页浏览（id 倒序）；type/category 筛选；每项带 done 标志。"""
        from ..models import QuestionType as _QT

        if page < 1 or not 1 <= page_size <= 50:
            raise HTTPException(status_code=400, detail="invalid page or page_size")
        qtype = None
        if type is not None:
            try:
                qtype = _QT(type)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"invalid type: {type}")
        if category is not None and category not in {name for name, _ in TAG_CATEGORIES}:
            raise HTTPException(status_code=400, detail=f"invalid category: {category}")
        cat_tags = None
        if category is not None:
            cat_tags = dict(TAG_CATEGORIES)[category]

        with db.get_session() as session:
            stmt = select(Question)
            if qtype is not None:
                stmt = stmt.where(Question.type == qtype)
            questions = list(session.scalars(stmt))
            if cat_tags is not None:
                questions = [
                    q for q in questions
                    if any(t in cat_tags for t in (q.tags or []))
                ]
            questions.sort(key=lambda q: q.id, reverse=True)
            total = len(questions)
            total_pages = (total + page_size - 1) // page_size
            page_items = questions[(page - 1) * page_size : page * page_size]
            latest = _latest_judgment_by_question(session)
            items = [
                {
                    "id": q.id,
                    "stem": q.stem,
                    "type": q.type.value,
                    "difficulty": q.difficulty,
                    "tags": q.tags,
                    "done": q.id in latest,
                }
                for q in page_items
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
        """标签分类结构（前端筛选联动用）。"""
        return [
            {"name": name, "tags": list(tags)} for name, tags in TAG_CATEGORIES
        ]

    @app.get("/api/review/tags")
    def review_tags():
        """全部词表标签 + 薄弱点计数（judgments.weak_tags 聚合）；有计数在前。"""
        with db.get_session() as session:
            rows = session.execute(select(Judgment.weak_tags)).all()
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
    def review(tag: str):
        if tag not in TAG_VOCABULARY:
            raise HTTPException(status_code=400, detail=f"invalid tag: {tag}")
        with db.get_session() as session:
            questions = list(
                session.scalars(
                    select(Question)
                    .where(cast(Question.tags, String).like(f'%"{tag}"%'))
                    .order_by(Question.created_at.desc())
                    .limit(30)
                )
            )
            latest = _latest_judgment_by_question(session)
            items = []
            for q in questions:
                item = {
                    "id": q.id,
                    "stem": q.stem,
                    "type": q.type.value,
                    "difficulty": q.difficulty,
                    "tags": q.tags,
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
            return items

    # --- 用户上传题目（格式：面经文本 / Q: 直接入库） ---

    @app.post("/api/upload")
    def upload_questions(body: dict, background: BackgroundTasks):
        filename = str(body.get("filename", ""))
        content = str(body.get("content", ""))
        if not filename.lower().endswith((".md", ".txt")):
            raise HTTPException(status_code=400, detail="仅支持 .md/.txt 文件")
        if len(content.encode("utf-8")) > 2 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="文件超过 2MB 限制")
        if not content.strip():
            raise HTTPException(status_code=400, detail="文件内容为空")
        if _is_direct_format(content):
            source, count = _insert_direct_questions(content)
            background.add_task(_tag_direct_questions, source.id, llm_factory)
            return {"mode": "direct", "count": count, "source_id": source.id}
        source = _import_facejing(content, filename)
        background.add_task(
            _generate_uploaded_source, source.id, llm_factory, embedder_factory
        )
        return {"mode": "facejing", "source_id": source.id}

    # --- 手动触发流水线 ---

    @app.post("/api/daily/run")
    def run_daily(background: BackgroundTasks):
        background.add_task(_run_daily_safe, daily_runner)
        return {"status": "running"}

    return app


def _run_answer_job(
    session_id: int, answer: str, config: AppConfig, llm_factory, embedder_factory
) -> None:
    """后台任务：M11 追问 / M10 判分（含参考检索）→ 落库；异常记日志，会话保持 active 可重试。"""
    try:
        with db.get_session() as session:
            row = session.get(Session, session_id)
            question = session.get(Question, row.question_id)
        judge_llm = llm_factory("judge")
        if row.status == SessionStatus.finished:
            # 判分失败重试：从 attempts 重建 transcript 直接重判，不再续答
            judgment = judge(
                question,
                _attempts_transcript(session_id),
                config.llm.judge_model,
                judge_llm,
                session_id=session_id,
                reference=_reference_for(question, embedder_factory),
                max_level=_max_level(session_id),
            )
            with db.get_session() as session:
                session.add(judgment)
                db.commit(session)
        elif row.kind == SessionKind.chain:
            chain = resume(
                session_id,
                question,
                judge_llm,
                judge_model=config.llm.judge_model,
                max_rounds=config.daily.chain_max_rounds,
            )
            result = chain.next_round(answer)
            if result["finished"]:
                chain.finish(reference=_reference_for(question, embedder_factory))
        else:
            judgment = judge(
                question,
                [{"role": "user", "content": answer}],
                config.llm.judge_model,
                judge_llm,
                session_id=session_id,
                reference=_reference_for(question, embedder_factory),
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
            _inflight.discard(session_id)


def _reference_for(question, embedder_factory) -> str | None:
    """判分前检索库内同类高分回答片段；无候选/失败返回 None（judge 降级不注入）。"""
    try:
        from ..retrieval import HIGH_SCORE, build_reference

        with db.get_session() as session:
            candidates = list(
                session.scalars(
                    select(Question)
                    .join(Session, Session.question_id == Question.id)
                    .join(Judgment, Judgment.session_id == Session.id)
                    .where(Judgment.total_score >= HIGH_SCORE)
                    .distinct()
                )
            )
        if not candidates:
            return None
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
    """直接模式入库：建 manual source + knowledge 题（难度 1、默认 criteria）。"""
    from ..pipeline.generate import DEFAULT_BAD_CRITERIA, DEFAULT_GOOD_CRITERIA

    stems = _parse_direct_questions(content)
    with db.get_session() as session:
        source = Source(
            type=SourceType.manual,
            title="用户上传",
            raw_text=content,
            cleaned_text=content,
            source_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        session.add(source)
        db.commit(session)
        session.refresh(source)
        for stem in stems:
            session.add(
                Question(
                    source_id=source.id,
                    type=QuestionType.knowledge,
                    stem=stem,
                    difficulty=1,
                    good_criteria=list(DEFAULT_GOOD_CRITERIA),
                    bad_criteria=list(DEFAULT_BAD_CRITERIA),
                )
            )
        db.commit(session)
    return source, len(stems)


def _tag_direct_questions(source_id: int, llm_factory) -> None:
    """直接模式后台补标签（≤20 题一次调用，词表过滤；失败留空）。"""
    try:
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
            "为以下题目各选 1-{max_tags} 个最相关的标签，必须从词表中选择：\n{vocab}\n\n"
            "题目列表：\n{numbered}\n\n"
            '只输出 JSON：{{"items": [{{"stem": "题干原文", "tags": [标签]}}]}}'
        ).format(max_tags=MAX_TAGS, vocab=tag_vocab_text(), numbered=numbered)
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
                    by_stem[s] = [
                        t for t in tags if isinstance(t, str) and t in TAG_VOCABULARY
                    ][:MAX_TAGS]
        if by_stem:
            with db.get_session() as session:
                for q in questions:
                    if q.stem in by_stem:
                        row = session.get(Question, q.id)
                        row.tags = by_stem[q.stem]
                db.commit(session)
    except Exception as e:
        logger.warning("tag direct questions failed (留空): %s", e)


def _import_facejing(content: str, filename: str) -> Source:
    """面经模式：内容写入 data/uploads/ 并作为 manual 源导入。"""
    from ..crawler.importer import import_file

    uploads = Path("data/uploads")
    uploads.mkdir(parents=True, exist_ok=True)
    path = uploads / f"{hashlib.sha256(content.encode('utf-8')).hexdigest()[:12]}.md"
    path.write_text(content, encoding="utf-8")
    return import_file(path, "manual")


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
    from ..embed import Embedder
    from ..pipeline.daily import default_sources, run_daily

    return run_daily(
        config, default_sources(config), llm_factory("generate"), Embedder()
    )


def _db_url() -> str:
    from ..main import DEFAULT_DB_URL

    return f"sqlite:///{DEFAULT_DB_URL}"


def _has_finished_session(session, question_id: int) -> bool:
    return (
        session.scalars(
            select(Session)
            .where(
                Session.question_id == question_id,
                Session.status == SessionStatus.finished,
            )
            .limit(1)
        ).first()
        is not None
    )


def _active_session_id(session, question_id: int) -> int | None:
    row = session.scalars(
        select(Session)
        .where(
            Session.question_id == question_id,
            Session.status == SessionStatus.active,
        )
        .order_by(Session.id.desc())
        .limit(1)
    ).first()
    return row.id if row else None


def _latest_judgment_by_question(session) -> dict[int, tuple[Session, Judgment]]:
    """每题最新 finished session 及其最新 judgment（按 Session.id 倒序首条即最新）。"""
    rows = session.execute(
        select(Session, Judgment)
        .join(Judgment, Judgment.session_id == Session.id)
        .where(Session.status == SessionStatus.finished)
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
