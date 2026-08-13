"""卡码笔记抓取（RAG 知识源）：抓分类目录 → 题目页正文 → markdown + manifest。

跑法：
    uv run python scripts/crawl_kamacoder.py --cat go            # 抓 Go 分类（默认）
    uv run python scripts/crawl_kamacoder.py --cat go --limit 5  # 试水前 5 页
    uv run python scripts/crawl_kamacoder.py --cat go --force    # 覆盖已存在文件

- 输出：data/knowledge/kamacoder/<cat>/<slug>.md（正文首行为问题标题）+ manifest.json（slug↔标题）
- 内容边界：仅用于本项目本地个人 RAG 参考，不对外分发、不商用
- 礼貌抓取：0.5s 间隔；幂等（已存在跳过）
"""
import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("crawl_kamacoder")

BASE = "https://notes.kamacoder.com"
OUT_DIR = Path("data/knowledge/kamacoder")
DELAY = 0.5  # 请求间隔（秒），礼貌抓取
ALLOWED_CATS = ("go", "java", "base", "llm")  # --cat 白名单：防 SSRF/路径穿越（仅本脚本手动运行）


def fetch(url: str) -> str:
    r = httpx.get(url, timeout=20, follow_redirects=True)
    r.raise_for_status()
    return r.text


def list_question_links(cat_url: str) -> list[tuple[str, str]]:
    """目录页 → [(slug, title)]：提取正文区的题目链接（.html），slug 为相对路径（支持多级，如 llm/intro/xxx）。"""
    html = fetch(BASE + cat_url)
    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen = set()
    for a in soup.select("a[href*='.html']"):
        href = a.get("href", "")
        if not href.startswith("/"):
            continue  # 站外/相对根链接跳过
        slug = href.split(".html")[0].lstrip("/")
        cat_prefix = cat_url.strip("/")
        if slug.startswith(cat_prefix + "/"):
            slug = slug[len(cat_prefix) + 1 :]  # 去分类前缀，保留子路径（llm/intro/xxx）
        title = a.get_text(strip=True)
        if not slug or not title or slug in seen:
            continue
        # 过滤导航/专栏介绍/课程页（标题特征）
        if any(k in title for k in ("专栏介绍", "零基础", "入门课", "必读", "面经")):
            continue
        if len(title) < 8:  # 题目标题一般较长
            continue
        seen.add(slug)
        links.append((slug, title))
    return links


def extract_article(html: str) -> str:
    """题目页正文 → markdown：h1 标题 + 各 section（简要/详细回答/知识扩展/追问 Q&A），排除导航/评论/广告。"""
    soup = BeautifulSoup(html, "html.parser")
    # 文章主体：页面主内容容器（h1 之后到 Last Updated 前）
    title_el = soup.find("h1")
    if title_el is None:
        raise ValueError("no h1")
    title = title_el.get_text(strip=True).lstrip("#").strip()  # markdown 源标题自带 # 符号

    # 从 h1 向后收集正文元素，遇到评论区/页脚停止
    body = title_el.find_next()
    sections = []
    buf = []
    last_updated_seen = False
    for el in body.find_all_next(["h1", "h2", "h3", "p", "pre", "li", "blockquote"], limit=400):
        if el.get("id") == "评论" or el.get_text(strip=True).startswith("Last Updated"):
            last_updated_seen = True
            break
        cls = " ".join(el.get("class", []))
        if "comment" in cls or "footer" in cls or "sidebar" in cls:
            continue
        text = el.get_text(strip=True).lstrip("#").strip()
        if not text:
            continue
        if el.name == "pre":
            code = el.get_text("\n").strip()
            buf.append(f"```\n{code}\n```")
        elif el.name in ("h2", "h3"):
            buf.append(f"\n## {text}")
        elif el.name == "h1":
            pass
        elif el.name == "li":
            buf.append(f"- {text}")
        else:
            buf.append(text)
    sections = "\n".join(buf).strip()
    if not sections:
        raise ValueError("empty article body")
    return f"# {title}\n\n{sections}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cat", default="go", help="分类目录（go/java/base/llm）")
    parser.add_argument("--limit", type=int, default=None, help="只抓前 N 页（试水）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在文件")
    args = parser.parse_args()
    if args.cat not in ALLOWED_CATS:
        parser.error(f"--cat 仅支持 {'/'.join(ALLOWED_CATS)}（收到 {args.cat!r}）")

    cat_dir = OUT_DIR / args.cat
    cat_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT_DIR / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    links = list_question_links(f"/{args.cat}/")
    logger.info("分类 %s：发现 %d 个题目页", args.cat, len(links))
    if args.limit:
        links = links[: args.limit]

    ok = skipped = failed = 0
    for slug, title in links:
        out = cat_dir / f"{slug}.md"  # 多级路径自动建子目录
        if out.exists() and not args.force:
            skipped += 1
            continue
        try:
            html = fetch(f"{BASE}/{args.cat}/{slug}.html")  # slug 无分类前缀，fetch 需补回
            markdown = extract_article(html)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(markdown, encoding="utf-8")
            manifest[slug] = title
            ok += 1
            logger.info("  [ok] %s（%s）", slug, title[:30])
        except Exception as e:
            failed += 1
            logger.warning("  [fail] %s: %s", slug, str(e)[:100])
        time.sleep(DELAY)

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    logger.info("完成：成功 %d，跳过 %d，失败 %d", ok, skipped, failed)


if __name__ == "__main__":
    main()
