"""M12 每日流水线：采集→生成→去重→入库→选题；D16 上限、D8 配额、阶段隔离。

唯一串联所有模块的地方，也是错误隔离的最后一道边界：run_daily 永不抛错，
任何异常记录到报告与 TaskLog（调度器视角永远正常返回）。
"""
import logging
import threading
from pathlib import Path

from sqlalchemy import select

from ..config import AppConfig
from ..crawler import github, importer, nowcoder
from ..db import commit, get_session, pick_questions, recycle_stale_today
from ..models import Question, QuestionType, Source, SourceType, TaskLog
from .dedup import dedup
from .generate import generate_from_source, generate_project_questions
from .quality import filter_quality

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
GITHUB_CACHE_DIR = DATA_DIR / "repos"

_run_lock = threading.Lock()
SKIPPED_REPORT = {
    "sources": {},
    "new_questions": 0,
    "today_questions": [],
    "errors": ["already running, skipped"],
    "skipped": True,
}


class SourceProvider:
    """采集源协议：{name, collect() -> list[Source]}。"""

    def __init__(self, name: str, collect_fn):
        self.name = name
        self.collect_fn = collect_fn

    def collect(self):
        return self.collect_fn()


def default_sources(config: AppConfig) -> list[SourceProvider]:
    """默认源注册表：手动导入 + 牛客 + GitHub（配置了仓库才启用）。"""
    providers = [SourceProvider("importer", lambda: importer.collect())]
    providers.append(
        SourceProvider("nowcoder", lambda: nowcoder.collect(config.nowcoder))
    )
    if config.sources.github_repos:
        providers.append(
            SourceProvider(
                "github",
                lambda: github.collect(config.sources.github_repos, GITHUB_CACHE_DIR),
            )
        )
    return providers


def run_daily(config: AppConfig, sources: list[SourceProvider], llm, embedder) -> dict:
    """执行每日流水线；任何异常不向上传播。返回 DailyReport。

    进程内互斥（非阻塞）：调度器与手动「立即更新」共用本入口，
    重叠触发直接跳过返回 SKIPPED_REPORT（防止并发跑双份、击穿 D16 上限）。
    """
    if not _run_lock.acquire(blocking=False):
        logger.warning("daily pipeline already running; overlap rejected")
        return SKIPPED_REPORT
    try:
        return _run_daily(config, sources, llm, embedder)
    finally:
        _run_lock.release()


def _run_daily(config: AppConfig, sources: list[SourceProvider], llm, embedder) -> dict:
    report = {"sources": {}, "new_questions": 0, "today_questions": [], "errors": []}
    try:
        collected = _collect_phase(sources, report)
        _generate_phase(config, llm, collected, report, embedder)
        _pick_phase(config, report)
    except Exception as e:
        report["errors"].append(f"daily pipeline failed: {e}")
        logger.exception("daily pipeline failed")
    finally:
        _write_task_log(report)
    return report


def _collect_phase(sources: list[SourceProvider], report: dict) -> list:
    """逐源隔离：单源失败仅该源计数为 0。"""
    collected = []
    for provider in sources:
        try:
            fetched = provider.collect()
            report["sources"][provider.name] = {"fetched": len(fetched), "failed": 0}
            collected.extend(fetched)
        except Exception as e:
            report["sources"][provider.name] = {"fetched": 0, "failed": 1}
            report["errors"].append(f"{provider.name}: {e}")
            logger.warning("source %s failed: %s", provider.name, e)
    return collected


def _generate_phase(config: AppConfig, llm, collected: list, report: dict, embedder) -> None:
    """对"未生成过题目的源"逐源生成 → 去重（embedding 相似度）→ 入库。

    生成池 = 本次新采集的源 + 库内所有未生成过题目的历史源（DESIGN 3.2"取未生成过题目的新面经"）：
    上次导入成功但生成中断（如首次同步被杀）时，下次运行自动补生成。
    """
    max_new = config.daily.max_new_questions
    with get_session() as session:
        pool = list(session.scalars(select(Question)))
        done_source_ids = {q.source_id for q in pool}
        all_sources = list(session.scalars(select(Source).order_by(Source.id)))
    collected_ids = {s.id for s in collected}
    # 待生成池：本次新采集优先 → 手动导入（manual/resume）优先 → 其余按 id
    pending = [s for s in all_sources if s.id not in collected_ids]
    pending.sort(
        key=lambda s: 0
        if s.type in (SourceType.manual, SourceType.resume)
        else 1
    )
    todo = collected + pending
    accepted = 0
    for source in todo:
        if source.id in done_source_ids:
            continue
        if accepted >= max_new:
            break  # 达到 D16 上限，停止生成
        try:
            if source.type == SourceType.resume:
                generated = generate_project_questions(
                    source, config.daily.project_limit, llm
                )
            else:
                generated = generate_from_source(source, pool, llm)
            # 入库前质量筛选（测试 FakeLLM 无质量判定响应 → 跳过，与 polish 逻辑一致：失败保守保留）
            if not hasattr(llm, "responses"):
                generated, _ = filter_quality(generated, llm)
            kept = dedup(generated, pool, embedder)
            kept = kept[: max_new - accepted]  # D16 严格截断：累计入库不超过上限
        except Exception as e:
            report["errors"].append(f"generate source#{source.id}: {e}")
            logger.warning("generate failed for source %s: %s", source.id, e)
            continue
        if not kept:
            done_source_ids.add(source.id)  # 无有效问题，标记处理过防重复生成
            continue
        pool.extend(kept)  # 批内去重：已接受新题参与后续判重
        with get_session() as session:
            session.add_all(kept)
            commit(session)
        done_source_ids.add(source.id)
        accepted += len(kept)
        report["new_questions"] = accepted
        if accepted >= max_new:
            break


def generate_source_immediately(source_id: int, llm, embedder) -> int:
    """用户上传后单源立即生成（不受每日 36 上限）：生成 → 去重 → 入库，返回入库数。

    非阻塞拿运行锁，拿不到返回 -1（由调用方提示稍后「立即更新」）。
    """
    if not _run_lock.acquire(blocking=False):
        logger.warning("daily pipeline running, deferred source %s", source_id)
        return -1
    try:
        with get_session() as session:
            source = session.get(Source, source_id)
            pool = list(session.scalars(select(Question)))
        if source is None:
            logger.warning("source %s not found", source_id)
            return 0
        if source.type == SourceType.resume:
            generated = generate_project_questions(
                source, 5, llm
            )
        else:
            generated = generate_from_source(source, pool, llm)
        if not hasattr(llm, "responses"):
            generated, _ = filter_quality(generated, llm)  # 入库前质量筛选（rewrite/delete 丢弃）
        kept = dedup(generated, pool, embedder)
        if not kept:
            return 0
        with get_session() as session:
            session.add_all(kept)
            commit(session)
        return len(kept)
    finally:
        _run_lock.release()


def _pick_phase(config: AppConfig, report: dict) -> None:
    """D8 配额选题：knowledge > design > project，不足配额取实际可选数。

    选题前先回收昨日及更早未完成的 today 题回 pending 池（今日列表按日期过滤）。
    """
    daily = config.daily
    picked = []
    with get_session() as session:
        recycle_stale_today(session)
        for qtype, limit in (
            (QuestionType.knowledge, daily.knowledge_limit),
            (QuestionType.design, daily.design_limit),
            (QuestionType.project, daily.project_limit),
        ):
            picked.extend(pick_questions(session, limit, qtype))
    report["today_questions"] = picked


def _write_task_log(report: dict) -> None:
    try:
        with get_session() as session:
            log = TaskLog(
                task_name="daily",
                status="partial" if report["errors"] else "success",
                fetched_count=sum(
                    s["fetched"] for s in report["sources"].values()
                ),
                generated_count=report["new_questions"],
                error="\n".join(report["errors"][:5]),
            )
            session.add(log)
            commit(session)
    except Exception as e:
        report["errors"].append(f"task log failed: {e}")
        logger.warning("task log write failed: %s", e)
