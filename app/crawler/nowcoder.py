"""M6 牛客爬虫：新版面经 API 列表拉取 → 清洗 → 入库；解析与 HTTP 分离。

2026-08 牛客改版：老 /discuss?type=2 页面 301 到首页，面经入口改为 SPA
（/interview/center），列表数据来自 gw-c.nowcoder.com 的 job-experience API，
列表响应自带正文全文（momentData.content），无需详情请求。
"""
import hashlib
import logging
import random
import time

import httpx

from ..config import NowcoderConfig, secret_value
from ..db import commit, find_source_by_hash, get_session
from ..errors import DuplicateSource, EnvVarMissing, NowcoderAuthError, NowcoderError
from ..models import Source, SourceType
from ..pipeline.clean import clean_text

logger = logging.getLogger(__name__)

NOWCODER_API_BASE = "https://gw-c.nowcoder.com"
NOWCODER_LIST_URL = NOWCODER_API_BASE + "/api/sparta/job-experience/experience/job/list"
_DISCUSS_URL = "https://www.nowcoder.com/discuss/{id}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MAX_LIST_PAGES = 10
_TIMEOUT = 15.0

# 登录态失效/异地登录的 API code（前端常量 USER_NOT_LOGIN=999 / LOGIN_OTHER_PLACE=998）
_AUTH_EXPIRED_CODES = (998, 999)
_LOGIN_MARKERS = ("请先登录", "登录后查看")


def collect(config: NowcoderConfig, *, api: "NowcoderAPI | None" = None) -> list[Source]:
    """本日增量采集：按页遍历直到遇到已存在条目或页尾；cookie 失效安静停用返回空。"""
    try:
        cookie = secret_value(config.cookie_env)
    except EnvVarMissing as e:
        logger.warning("nowcoder disabled: %s", e)
        return []
    api = api or NowcoderAPI(cookie, config.request_interval, config.retries)
    sources: list[Source] = []
    try:
        for page in range(1, MAX_LIST_PAGES + 1):
            entries = api.get_list(page)
            if not entries:
                break
            for entry in entries:
                source = _import_entry(entry)
                if source is None:
                    return sources  # 增量边界：已存在条目，本日停止
                sources.append(source)
    except NowcoderAuthError as e:
        logger.warning("nowcoder disabled today: %s", e)
    return sources


class NowcoderAPI:
    """HTTP 传输层：cookie、随机限速、指数退避重试。"""

    def __init__(
        self,
        cookie: str,
        interval: float,
        retries: int,
        *,
        client: httpx.Client | None = None,
        sleep_func=None,
    ):
        self._cookie = cookie
        self._interval = interval
        self._retries = retries
        self._sleep = sleep_func or time.sleep
        self._client = client or httpx.Client(
            timeout=_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Cookie": cookie},
        )

    def get_list(self, page: int) -> list[dict]:
        resp = self._request(
            NOWCODER_LIST_URL,
            json_body={
                "companyList": None,
                "jobId": -1,
                "level": 1,
                "order": 3,  # 3=最新（tabMap: 最新=3 / 综合=1）
                "page": page,
                "isNewJob": True,
            },
        )
        try:
            payload = resp.json()
        except ValueError:
            raise NowcoderError("list api returned non-JSON (structure changed or blocked)")
        return parse_list_response(payload)

    def _request(self, url: str, *, json_body: dict | None = None) -> httpx.Response:
        last_exc = None
        for attempt in range(self._retries + 1):
            if attempt > 0:
                self._sleep(2.0**attempt)
            self._sleep(random.uniform(0.7, 1.3) * self._interval)
            try:
                if json_body is not None:
                    resp = self._client.post(url, json=json_body)
                else:
                    resp = self._client.get(url)
            except httpx.HTTPError as e:
                last_exc = e
                continue
            if detect_session_expired(resp):
                raise NowcoderAuthError(f"cookie expired or logged out: http {resp.status_code}")
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = NowcoderError(f"http {resp.status_code} for {url}")
                continue
            if resp.status_code != 200:
                raise NowcoderError(f"unexpected http {resp.status_code} for {url}")
            return resp
        raise NowcoderError(f"request failed after {self._retries} retries: {last_exc}")


# --- 解析层（纯函数，牛客改版只改这里） ---


def parse_list_response(payload: dict) -> list[dict]:
    """列表 API JSON → [{title, url, content}]；非面经条目（AI 卡片等）跳过。"""
    if not isinstance(payload, dict) or payload.get("success") is not True:
        code = payload.get("code") if isinstance(payload, dict) else None
        raise NowcoderError(f"list api failed: success=false code={code}")
    records = (payload.get("data") or {}).get("records") or []
    entries = []
    for rec in records:
        moment = rec.get("momentData") or {}
        if not moment.get("id"):
            continue  # 无 momentData 的条目（如 contentType=250 的 AI 总结卡片）
        title = moment.get("title") or ""
        content = moment.get("content") or ""
        if not title and not content:
            continue
        entries.append(
            {
                "title": title,
                "url": _DISCUSS_URL.format(id=moment["id"]),
                "content": content,
                "created_at": moment.get("createdAt"),
            }
        )
    return entries


def detect_session_expired(response: httpx.Response) -> bool:
    """登录态失效识别：HTTP 401 / 响应体登录提示 / JSON envelope code 998/999。"""
    if response.status_code == 401:
        return True
    if response.status_code != 200:
        return False
    body = response.text[:2000].lower()
    if any(m in body for m in _LOGIN_MARKERS):
        return True
    try:
        payload = response.json()
    except ValueError:
        return False
    if isinstance(payload, dict) and payload.get("success") is False:
        return payload.get("code") in _AUTH_EXPIRED_CODES
    return False


# --- 入库 ---


def _import_entry(entry: dict) -> Source | None:
    """单条入库（正文已随列表返回）；已存在返回 None（增量边界信号）。"""
    url_hash = hashlib.sha256(entry["url"].encode("utf-8")).hexdigest()
    with get_session() as session:
        if find_source_by_hash(session, url_hash):
            return None
    with get_session() as session:
        source = Source(
            type=SourceType.nowcoder,
            url=entry["url"],
            title=entry["title"],
            raw_text=entry["content"],
            cleaned_text=clean_text(entry["content"]),
            source_hash=url_hash,
        )
        session.add(source)
        try:
            commit(session)
        except DuplicateSource:
            return None
        session.refresh(source)
    return source
