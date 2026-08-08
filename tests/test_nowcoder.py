import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app.config import NowcoderConfig
from app.crawler.nowcoder import (
    NowcoderAPI,
    collect,
    detect_session_expired,
    parse_list_response,
)
from app.errors import NowcoderAuthError, NowcoderError
from app.models import Source, SourceType

FIXTURES = Path(__file__).parent / "fixtures"

LIST_JSON = json.loads((FIXTURES / "nowcoder_list.json").read_text(encoding="utf-8"))
EMPTY_JSON = json.loads((FIXTURES / "nowcoder_list_empty.json").read_text(encoding="utf-8"))
EXPIRED_JSON = json.loads((FIXTURES / "nowcoder_auth_expired.json").read_text(encoding="utf-8"))
BLOCKED_HTML = (FIXTURES / "nowcoder_blocked.html").read_text(encoding="utf-8")


def make_api(handler, interval=0.0, retries=2):
    """handler: httpx.Request -> httpx.Response；sleep 全部记录不真实等待。"""
    sleeps = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    api = NowcoderAPI(
        "cookie=1",
        interval,
        retries,
        client=client,
        sleep_func=sleeps.append,
    )
    return api, sleeps


# --- 解析层 happy ---


def test_parse_list_response_entries():
    entries = parse_list_response(LIST_JSON)
    assert len(entries) == 3  # contentType=250 的 AI 卡片被跳过
    assert entries[0]["title"] == "美图日常一面面经"
    assert entries[0]["url"] == "https://www.nowcoder.com/discuss/2886615"
    assert "三色标记GC流程" in entries[0]["content"]
    assert "华为OD-Java岗位面经" in entries[1]["title"]


# --- 解析层 edge ---


def test_parse_list_response_empty():
    assert parse_list_response(EMPTY_JSON) == []


def test_parse_list_skips_records_without_moment():
    entries = parse_list_response(LIST_JSON)
    assert all("/discuss/" in e["url"] for e in entries)
    assert all(e["title"] for e in entries)


def test_parse_list_skips_moment_without_title_and_content():
    payload = {
        "success": True,
        "code": 0,
        "data": {"records": [{"contentType": 74, "momentData": {"id": 1}}]},
    }
    assert parse_list_response(payload) == []


# --- 解析层 fail ---


def test_parse_list_success_false_raises():
    with pytest.raises(NowcoderError, match="success=false"):
        parse_list_response({"success": False, "code": 3, "msg": "参数错误"})


def test_parse_list_non_dict_raises():
    with pytest.raises(NowcoderError):
        parse_list_response("not-a-json-object")


# --- detect_session_expired ---


def test_detect_expired_by_status_401():
    assert detect_session_expired(httpx.Response(401))


def test_detect_expired_by_json_code_999():
    resp = httpx.Response(200, text=json.dumps(EXPIRED_JSON))
    assert detect_session_expired(resp)


def test_detect_expired_by_json_code_998():
    resp = httpx.Response(200, text=json.dumps({"success": False, "code": 998}))
    assert detect_session_expired(resp)


def test_detect_not_expired_other_json_error():
    resp = httpx.Response(200, text=json.dumps({"success": False, "code": 1003}))
    assert not detect_session_expired(resp)


def test_detect_not_expired_normal_json():
    resp = httpx.Response(200, text=json.dumps({"success": True, "code": 0}))
    assert not detect_session_expired(resp)


def test_detect_not_expired_normal_page():
    assert not detect_session_expired(httpx.Response(200, text=BLOCKED_HTML))
    assert not detect_session_expired(httpx.Response(404))


def test_detect_expired_by_login_marker():
    resp = httpx.Response(200, text="<html>请先登录后再查看</html>")
    assert detect_session_expired(resp)


# --- HTTP 层 happy ---


def test_get_list_posts_json_body():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=LIST_JSON)

    api, _ = make_api(handler)
    entries = api.get_list(1)
    assert len(entries) == 3
    assert seen["method"] == "POST"
    assert seen["url"] == "https://gw-c.nowcoder.com/api/sparta/job-experience/experience/job/list"
    assert seen["body"]["page"] == 1
    assert seen["body"]["order"] == 3  # 最新
    assert seen["body"]["jobId"] == -1


def test_get_list_sequential_with_throttle():
    pages = iter([LIST_JSON, EMPTY_JSON])

    def handler(request):
        return httpx.Response(200, json=next(pages))

    api, sleeps = make_api(handler, interval=1.5)
    assert len(api.get_list(1)) == 3
    assert api.get_list(2) == []
    assert len(sleeps) == 2
    for s in sleeps:
        assert 0.7 * 1.5 <= s <= 1.3 * 1.5


# --- HTTP 层 edge/fail ---


def test_429_retries_then_success():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json=LIST_JSON)

    api, sleeps = make_api(handler, retries=2)
    entries = api.get_list(1)
    assert len(entries) == 3
    assert len(calls) == 2
    assert sleeps[0] == 0.0  # 限速间隔（interval=0）
    assert sleeps[1] == 2.0  # 退避 2**1
    assert sleeps[2] == 0.0


def test_502_retries_exhausted_raises():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(502)

    api, _ = make_api(handler, retries=1)
    with pytest.raises(NowcoderError):
        api.get_list(1)
    assert len(calls) == 2


def test_connection_timeout_raises():
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    api, _ = make_api(handler, retries=1)
    with pytest.raises(NowcoderError):
        api.get_list(1)


def test_401_raises_auth_error_without_retry():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401)

    api, _ = make_api(handler, retries=2)
    with pytest.raises(NowcoderAuthError):
        api.get_list(1)
    assert len(calls) == 1


def test_non_json_response_raises():
    def handler(request):
        return httpx.Response(200, text=BLOCKED_HTML)

    api, _ = make_api(handler)
    with pytest.raises(NowcoderError, match="non-JSON"):
        api.get_list(1)


# --- 集成 ---


def nowcoder_config():
    return NowcoderConfig(
        cookie_env="NOWCODER_COOKIE",
        request_interval=0.0,
        retries=1,
    )


def make_router():
    """按 URL 路由：列表第 1 页 3 条，第 2 页空。"""
    get_calls = []

    def handler(request):
        if request.method != "POST":
            get_calls.append(str(request.url))
            return httpx.Response(404)
        body = json.loads(request.content)
        if body.get("page") == 1:
            return httpx.Response(200, json=LIST_JSON)
        return httpx.Response(200, json=EMPTY_JSON)

    return handler, get_calls


def test_collect_imports_sources(db, monkeypatch):
    monkeypatch.setenv("NOWCODER_COOKIE", "cookie=1")
    handler, get_calls = make_router()
    api, _ = make_api(handler)
    sources = collect(nowcoder_config(), api=api)

    assert len(sources) == 3
    assert all(s.type == SourceType.nowcoder for s in sources)
    assert "三色标记GC流程" in sources[0].cleaned_text
    assert "vx" not in sources[0].cleaned_text  # M4 广告过滤生效
    assert get_calls == []  # 正文随列表返回，无详情请求


def test_collect_incremental_stops_at_existing(db, monkeypatch):
    monkeypatch.setenv("NOWCODER_COOKIE", "cookie=1")
    first_url = "https://www.nowcoder.com/discuss/2886615"
    existing_hash = hashlib.sha256(first_url.encode("utf-8")).hexdigest()
    with db:  # 预置已存在条目
        db.add(Source(type=SourceType.nowcoder, url=first_url, source_hash=existing_hash))
        from app.db import commit as db_commit

        db_commit(db)

    handler, get_calls = make_router()
    api, _ = make_api(handler)
    sources = collect(nowcoder_config(), api=api)
    assert sources == []
    assert get_calls == []


def test_collect_cookie_expired_returns_empty_with_log(db, monkeypatch, caplog):
    monkeypatch.setenv("NOWCODER_COOKIE", "cookie=1")

    def handler(request):
        return httpx.Response(200, json=EXPIRED_JSON)

    api, _ = make_api(handler)
    with caplog.at_level("WARNING"):
        sources = collect(nowcoder_config(), api=api)
    assert sources == []
    assert "disabled" in caplog.text


def test_collect_cookie_env_missing_returns_empty_with_log(db, monkeypatch, caplog):
    monkeypatch.delenv("NOWCODER_COOKIE", raising=False)
    with caplog.at_level("WARNING"):
        sources = collect(nowcoder_config())
    assert sources == []
    assert "disabled" in caplog.text
