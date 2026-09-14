"""prompt 从文件读，不写在代码里（ADR-0007）。

三条硬约束，都来自"这类方案最典型的坑"：

① **路径相对仓库根解析，不是相对 cwd**。否则从别的工作目录跑 `tools/` 时会
   **静默读到空集合**，而那正是 AGENTS.md §3.1「静默降级」的一个入口。
② **`prompts/` 不是包**（ADR-0010）：读法只有"相对仓库根 `open()`"这一种。
   放进 `app/prompts/` 会让 `importlib.resources` 与普通 `open` 并存，而后者
   打包进 wheel/docker 后才失效 —— 本地全绿、上线读空。
③ **读不到就抛，不许返回空字符串**。空缺的 prompt 会让模型自由发挥，而失败
   现场离这里很远。

缓存：prompt 文件在进程生命周期里不变（改它要重启/重载），所以按 mtime 缓存，
既省 IO，又能在开发时改了立刻生效。
"""

from __future__ import annotations

from pathlib import Path

from app.llm import Prompt

#: 仓库根 —— 本文件在 `app/llm/` 下，所以是上两级。
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"

#: 相对仓库根的相对路径缓存（`name -> (mtime, Prompt)`）
_cache: dict[str, tuple[float, Prompt]] = {}


class PromptNotFound(FileNotFoundError):
    """prompt 文件不存在。**它必须响亮** —— 见模块 docstring 第 ③ 条。"""


def resolve(name: str) -> Path:
    """把 prompt 名解析成绝对路径，并**拒绝逃出 `prompts/`**。"""
    path = (PROMPTS_DIR / name).resolve()
    if not path.is_relative_to(PROMPTS_DIR):
        raise PromptNotFound(f"prompt 名不许逃出 prompts/：{name}")
    return path


def load(name: str) -> Prompt:
    """读一个 prompt（带 mtime 缓存）。"""
    path = resolve(name)
    if not path.is_file():
        raise PromptNotFound(
            f"prompt 不存在：{name}（找的是 {path}）—— "
            f"路径相对仓库根解析，不是相对当前工作目录"
        )
    mtime = path.stat().st_mtime
    hit = _cache.get(name)
    if hit and hit[0] == mtime:
        return hit[1]

    text = path.read_text(encoding="utf-8")
    prompt = Prompt(name=name, text=text)
    _cache[name] = (mtime, prompt)
    return prompt


def clear_cache() -> None:
    _cache.clear()
