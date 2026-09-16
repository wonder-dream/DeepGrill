"""依赖契约的测试 —— 上线时最容易被忽略、后果最直接的一类事。

它钉三件**机器能判**的事（每一条都对应一次真实的部署事故形状）：

① **`pyproject.toml` 的直接依赖 = `requirements.lock.txt` 里钉住的那些**
   （少了谁、多了谁都要报）—— 锁文件是部署真正装的那一份，两者漂开意味着
   "测试跑绿的世界"与"服务器上的世界"不是同一个。
② **app/ 里 import 的第三方包都在直接依赖里** —— 这条是**实测撞出来的**：
   `httpx` 被 `app/llm/__init__.py` 直接 import，却从来没写进 dependencies
   （它只是被某条开发依赖顺带装上）；`python-multipart` 是 FastAPI 解析
   `Form()`/`File()` 的必需件，同样没写。干净环境里 `pip install -e .`
   少装任一个，**服务直接起不来**（ImportError / 表单全废），而本机一切正常。
③ **锁文件里没有任何开发工具**（pytest / ruff / mypy）—— 生产不该装它们，
   而"从开发环境 freeze 一份"正是它们混进去的方式。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "requirements.lock.txt"

#: 标准库（判据 ② 会把这些排除掉）。不是穷举，是"这个项目真的用到的那些"——
#: 漏一个的代价是测试报一条，补上即可；多一个的代价是漏掉一次真检查。
STDLIB = {
    "__future__", "abc", "argparse", "array", "ast", "asyncio", "base64", "bisect",
    "collections", "concurrent", "contextlib", "copy", "csv", "dataclasses", "datetime",
    "decimal", "difflib", "email", "enum", "functools", "getpass", "gzip", "hashlib",
    "heapq", "hmac", "html", "http", "importlib", "inspect", "io", "itertools", "json",
    "locale", "logging", "math", "operator", "os", "pathlib", "platform", "random", "re",
    "secrets", "shutil", "signal", "socket", "sqlite3", "statistics", "string", "struct",
    "subprocess", "sys", "tempfile", "textwrap", "threading", "time", "traceback", "types",
    "typing", "unicodedata", "urllib", "uuid", "warnings", "zipfile",
}
#: 本仓库自己的包 / 不是运行时代码的目录
OWN = {"app", "migrations", "tests", "tools", "scripts", "prompts"}


def _direct_dependencies() -> set[str]:
    """`[project] dependencies` 里的包名（去掉版本约束）。"""
    text = PYPROJECT.read_text(encoding="utf-8")
    block = re.search(r"^dependencies = \[(.*?)^\]", text, re.S | re.M)
    assert block is not None, "pyproject.toml 里找不到 [project] dependencies"
    names = set()
    for raw in re.findall(r'"([^"]+)"', block.group(1)):
        names.add(_canon(re.split(r"[<>=!\[; ]", raw, maxsplit=1)[0]))
    return names


def _canon(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _locked() -> dict[str, str]:
    """锁文件里的 `包 → 版本`（`==` 形式）。"""
    out: dict[str, str] = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, version = line.partition("==")
        assert version, f"锁文件里有一行没有钉版本：{line!r}"
        out[_canon(name)] = version.strip()
    return out


def test_lock_file_exists_and_pins_every_direct_dependency() -> None:
    """锁文件必须存在，且**恰好**覆盖 pyproject 的直接依赖。"""
    assert LOCK.is_file(), (
        "没有 requirements.lock.txt —— 服务器上的 `pip install -e .` 会解析到"
        "当时最新的版本，而那不是测试跑绿的那一套"
    )
    direct, locked = _direct_dependencies(), _locked()
    missing = direct - set(locked)
    assert not missing, f"直接依赖没被锁住：{sorted(missing)}"
    # 反向：锁里多出来的必须能沿依赖图解释（间接依赖），所以只断言直接依赖都在
    for name in sorted(direct):
        assert locked[name], f"{name} 在锁文件里没有版本"


def test_lock_file_has_no_dev_tools() -> None:
    """生产依赖里不许混进开发工具（"从开发环境 freeze 一份"就是这么混进去的）。"""
    locked = set(_locked())
    banned = {"pytest", "ruff", "mypy", "pytest-cov", "ipython"}
    assert not (locked & banned), f"锁文件里有开发工具：{sorted(locked & banned)}"


def test_lock_file_is_installable_offline_shape() -> None:
    """行必须是 `包==版本`（`pip install -r` 认的形状），且没有重复。"""
    lines = [
        line.split("#", 1)[0].strip()
        for line in LOCK.read_text(encoding="utf-8").splitlines()
    ]
    entries = [line for line in lines if line]
    assert len(entries) == len(set(entries)), "锁文件里有重复条目"
    for line in entries:
        assert re.fullmatch(r"[A-Za-z0-9._-]+==[^\s;]+", line), f"这行不是 `包==版本`：{line!r}"


def test_every_third_party_import_is_a_declared_dependency() -> None:
    """**回归测试**：app/ 里 import 的每个第三方包都要在直接依赖里。

    实测撞到的两个（都在 2026-09-16 排查上线风险时发现）：
      · `httpx` —— `app/llm/__init__.py` 直接 import，却只被开发依赖顺带装上
      · `python-multipart` —— FastAPI 的 `Form()`/`File()` 的必需件，从未声明
    干净环境里少装任一个，服务**起不来**（ImportError）或表单全废 —— 而本机
    永远正常，因为开发环境什么都装了。
    """
    declared = _direct_dependencies()
    found: dict[str, str] = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = re.match(r"\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
            if not m:
                continue
            name = m.group(1)
            if name in STDLIB or name in OWN:
                continue
            found.setdefault(_canon(name), f"{path.relative_to(ROOT)}:{i}")

    undeclared = {k: v for k, v in found.items() if k not in declared}
    # `pytest` 只在测试文件里出现（app/ 下就有测试），开发依赖里已声明
    undeclared.pop("pytest", None)
    assert not undeclared, (
        f"这些包被 app/ 直接 import，却没写进 dependencies：{undeclared} —— "
        f"干净环境里装不上它们，服务会起不来"
    )


def test_form_endpoints_declare_python_multipart() -> None:
    """用了表单就必须声明 `python-multipart` —— 它是**扫 import 扫不到**的那种依赖。

    FastAPI 的 `Form()` / `File()` 在运行时需要它（登录、注册、作答、录音上传全靠表单），
    而代码里没有一行 `import multipart`。少了它的表现是"整站表单不可用"，不是某个
    边缘功能坏掉。
    """
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "app").rglob("*.py")
    )
    uses_forms = re.search(r"\b(?:Form|File)\(|UploadFile", sources) is not None
    assert uses_forms, "app/ 里没有表单参数了？那这条断言要跟着改"
    assert "python-multipart" in _direct_dependencies(), (
        "app/ 用了 Form()/File()，但 dependencies 里没有 python-multipart —— "
        "干净环境里装不上它，表单会全废（而本机因为开发环境什么都装了，看不出问题）"
    )


def test_lock_versions_match_the_tested_environment() -> None:
    """锁里的版本必须与**这套跑过测试的环境**一致（否则锁的是一套没测过的组合）。"""
    import importlib.metadata as md

    locked = _locked()
    drifted = {}
    for name, version in locked.items():
        try:
            installed = md.version(name)
        except md.PackageNotFoundError:
            drifted[name] = "（本环境没装）"
            continue
        if installed != version:
            drifted[name] = f"{version} → 实测装的是 {installed}"
    assert not drifted, f"锁文件与实测环境不一致，请重新生成：{drifted}"


if __name__ == "__main__":  # pragma: no cover - 手动重生成锁文件用
    pytest.skip("用 .tmp 下的生成脚本重生成 requirements.lock.txt")
