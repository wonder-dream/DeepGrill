from pathlib import Path

from app.pipeline.clean import AD_KEYWORDS, clean_text, split_rounds

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- happy ---


def test_clean_html_to_text():
    result = clean_text(fixture("interview_normal.html"))
    assert "<div>" not in result
    assert "HashMap 底层原理" in result
    assert "TCP 三次握手" in result


def test_ad_lines_removed():
    result = clean_text(fixture("interview_ad.html"))
    assert "关注公众号" not in result
    assert "vx" not in result
    assert "Redis 缓存穿透" in result
    assert "分布式事务" in result


def test_split_rounds_three_rounds():
    rounds = split_rounds(clean_text(fixture("interview_normal.html")))
    # 标题行归入 name="" 的 prelude 轮，其后为三个面试轮次
    assert [r["name"] for r in rounds] == ["", "一面", "二面", "三面"]
    assert "阿里后端" in rounds[0]["content"]
    assert "HashMap" in rounds[1]["content"]
    assert "短链接" in rounds[2]["content"]
    assert "Agent" in rounds[3]["content"]


def test_split_rounds_beyond_three_rounds():
    """分批生成依赖：支持一面~十面（后端通用素材 8 章）。"""
    text = (
        "一面：缓存\n问了 Redis。\n"
        "四面：并发\n问了 GIL。\n"
        "八面：Spring\n问了 IoC。\n"
    )
    rounds = split_rounds(text)
    assert [r["name"] for r in rounds] == ["一面", "四面", "八面"]
    assert "Redis" in rounds[0]["content"]
    assert "GIL" in rounds[1]["content"]
    assert "IoC" in rounds[2]["content"]


# --- edge ---


def test_empty_string():
    assert clean_text("") == ""


def test_whitespace_only():
    assert clean_text("   \n\t  \n") == ""


def test_long_text_roundtrip():
    long_text = ("一面：\n" + "长" * 100 + "\n") * 100
    result = clean_text(long_text)
    assert len(result) > 10_000
    rounds = split_rounds(result)
    assert len(rounds) == 100


def test_chinese_punctuation_preserved():
    text = "一面：\n问了 RAG 的评估方法；以及：命中率、MRR。"
    result = clean_text(text)
    assert "；" in result
    assert "：" in result


def test_prelude_becomes_unnamed_round():
    text = "楼主背景：985 本硕，三年后端。\n一面：\n问了 MySQL 索引。"
    rounds = split_rounds(text)
    assert rounds[0]["name"] == ""
    assert "楼主背景" in rounds[0]["content"]
    assert rounds[1]["name"] == "一面"


# --- fail ---


def test_broken_html_no_exception():
    result = clean_text(fixture("interview_broken.html"))
    assert "文本一" in result
    assert "文本三" in result


def test_script_and_style_content_stripped():
    result = clean_text(fixture("interview_broken.html"))
    assert "var secret" not in result
    assert "display: none" not in result


def test_html_entities_decoded():
    result = clean_text("<p>R&amp;D 团队</p>")
    assert "R&D 团队" in result


def test_embedded_attribute_not_leaked():
    result = clean_text('<a href="https://example.com">跳转链接</a>')
    assert "跳转链接" in result
    assert "href" not in result


def test_no_round_structure_returns_single_unnamed():
    text = "只问了一道题：说说进程和线程的区别。"
    rounds = split_rounds(text)
    assert len(rounds) == 1
    assert rounds[0]["name"] == ""
    assert "进程和线程" in rounds[0]["content"]
