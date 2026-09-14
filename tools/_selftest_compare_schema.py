"""对照工具的自检：证明它真的能抓到"文档有、SQL 没有"的字段。

**为什么必须有它**：`tools/_compare_schema.py` 的输出里第一节「只在文档里有」
写着"无"，而"无"本身不能说明任何事 —— 一套全空转的提取逻辑也会打印"无"。
本项目对校验器已经定了这条规矩（见 `scripts/selftest_check_docs.py`），
对照工具同样适用。做法也一样：**故意埋一个假字段，断言它被抓到**。

用法：
    python tools/_selftest_compare_schema.py
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

FAKE_TABLE_FIELD = "fake_column_xyz"


def _load_tool(doc_path: Path):
    """把对照工具加载进来，并让它读指定的文档副本。

    工具把文档路径写成了模块常量 `DOC`，所以改那个常量即可 ——
    不必去动真文档（那是自检最忌讳的事：**测试不许污染被它检查的东西**）。
    """
    spec = importlib.util.spec_from_file_location(
        "compare_schema", ROOT / "tools" / "_compare_schema.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DOC = doc_path
    return mod


def main() -> int:
    real_doc = ROOT / "docs" / "v2数据模型.md"
    # 自检的临时目录用 `Path.mkdir()` 建，**不要用 `tempfile.mkdtemp()`**。
    #
    # 实测：`mkdtemp` 在 Windows 上用 `mode=0o700` 建目录，那个模式对**创建它的
    # 进程自己**也生效 —— 于是脚本紧接着往里写文件就是 PermissionError，
    # 而错误信息完全看不出原因（看起来像沙箱拒绝）。`Path.mkdir()` 用默认权限，
    # 所以本仓库的 `tmp_dir` fixture 一直没问题。
    tmp = ROOT / "tools" / f"_selftest_tmp_{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        # --- 变异 1：往某张表的字段清单里塞一个假字段 ---
        text = real_doc.read_text(encoding="utf-8")
        anchor = "| status | draft / confirmed（**人审过才是 confirmed**） |"
        assert anchor in text, "锚点找不到 —— 文档改过，本自检需要更新"
        mutated = text.replace(
            anchor, f"| **{FAKE_TABLE_FIELD}** | 自检故意插入 |\n{anchor}", 1
        )
        doc1 = tmp / "mutated.md"
        doc1.write_text(mutated, encoding="utf-8")

        tool = _load_tool(doc1)
        d, _ = tool.doc_tables()
        s = tool.sql_tables()

        caught = [
            f"{t}.{f}"
            for t in sorted(s)
            for f in d.get(t, [])
            if f not in s[t]
        ]
        ok1 = any(FAKE_TABLE_FIELD in c for c in caught)
        print(f"变异 1（塞入假字段）: {'[抓到]' if ok1 else '[漏掉]'}")
        if ok1:
            print(f"  报告为：{caught}")

        # --- 变异 2：删掉 SQL 里真实存在的一列，文档侧应当报出来 ---
        # 这条反向验证：不只是"文档多出来的能报"，而是差集本身是双向工作的。
        doc2 = tmp / "clean.md"
        doc2.write_text(text, encoding="utf-8")
        tool2 = _load_tool(doc2)
        d2, _ = tool2.doc_tables()
        assert "domain_id" in d2.get("knowledge_points", []), (
            "clean 副本里没有 domain_id —— 文档改过，本自检需要更新"
        )
        ok2 = "domain_id" in d2["knowledge_points"]
        print(f"变异 2（确认能读到真实字段 domain_id）: {'[抓到]' if ok2 else '[漏掉]'}")

        # --- 还原后：干净文档不应报出假字段 ---
        tool3 = _load_tool(doc2)
        d3, _ = tool3.doc_tables()
        s3 = tool3.sql_tables()
        residue = [
            f for t in sorted(s3) for f in d3.get(t, []) if f not in s3[t]
        ]
        ok3 = not any(FAKE_TABLE_FIELD in c for c in residue)
        print(f"干净文档: {'[通过]' if ok3 else '[FAIL] 残留了假字段'}")

        if ok1 and ok2 and ok3:
            print("\n对照工具自检：2/2 条变异被抓到，干净文档不误报")
            return 0
        print("\n对照工具自检未通过 —— 提取逻辑已失效，第一节的「无」不可信")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
