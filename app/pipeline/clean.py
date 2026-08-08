import re
from typing import TypedDict

from bs4 import BeautifulSoup

# 广告/引流关键词：行内命中即整行滤除（模块常量，便于维护扩充）
AD_KEYWORDS = ("求私聊", "vx", "wx", "关注公众号")
# 水印关键词
WATERMARK_KEYWORDS = ("转载请联系", "禁止转载", "未经授权", "版权归作者")

_ROUND_RE = re.compile(r"^([一二三四五六七八九十]+面)(?:面试|终面)?(?:[：:、，,.\-—（(\s]|$)")


class RoundText(TypedDict):
    name: str
    content: str


def clean_text(raw: str) -> str:
    """去 HTML 标签/script/style/广告/水印/空白噪音，返回规范化纯文本。

    纯函数：损坏 HTML 不抛异常，能提多少提多少；实体解码由 bs4 自动完成。
    """
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    lines = [re.sub(r"\s+", " ", line).strip() for line in soup.get_text().splitlines()]
    lines = [line for line in lines if line]
    lines = [line for line in lines if not _contains_any(line, AD_KEYWORDS)]
    lines = [line for line in lines if not _contains_any(line, WATERMARK_KEYWORDS)]
    return "\n".join(lines)


def split_rounds(text: str) -> list[RoundText]:
    """按一面~十面结构拆分；无轮次结构时返回 [{name:"", content: text}]。

    轮次标题前的行（如背景介绍）归入 name="" 的轮次。
    """
    lines = text.splitlines()
    rounds: list[RoundText] = []
    name = ""
    buf: list[str] = []
    for line in lines:
        match = _ROUND_RE.match(line.strip())
        if match:
            rounds.append(RoundText(name=name, content="\n".join(buf).strip()))
            name = match.group(1)
            buf = []
        else:
            buf.append(line)
    rounds.append(RoundText(name=name, content="\n".join(buf).strip()))
    if len(rounds) == 1 and not rounds[0]["name"]:
        return rounds
    return [r for r in rounds if r["content"] or r["name"]]


def _contains_any(line: str, keywords: tuple[str, ...]) -> bool:
    return any(kw in line for kw in keywords)
