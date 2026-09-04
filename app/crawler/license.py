"""GitHub 仓库 LICENSE 解析与许可识别（本地文件解析，无网络）。

用于 github.py 导入前的许可白名单校验：只允许有明确可再分发许可的仓库
进入公开/生产流水线；无许可或不在白名单的仓库整体跳过。
"""
from __future__ import annotations

import re
from pathlib import Path

# 常见许可文件名（根目录，大小写不敏感）
_LICENSE_NAMES = {
    "LICENSE",
    "LICENSE.MD",
    "LICENSE.TXT",
    "LICENCE",
    "COPYING",
    "UNLICENSE",
}

_SPDX_RE = re.compile(r"SPDX-License-Identifier\s*:\s*([A-Za-z0-9.\-]+)")


def detect_license(repo_path: Path) -> str | None:
    """扫描仓库根目录的 LICENSE 文件并返回 SPDX 标识；未发现/无法识别返回 None。"""
    if not repo_path.is_dir():
        return None
    for p in repo_path.iterdir():
        if p.is_file() and p.name.upper() in _LICENSE_NAMES:
            try:
                head = p.read_text(encoding="utf-8", errors="replace")[:8192]
            except OSError:
                continue
            lic = _parse_license_text(head)
            if lic:
                return lic
    return None


def _parse_license_text(head: str) -> str | None:
    """从 LICENSE 文本前 8KB 中识别 SPDX 标识或常见许可标题。"""
    spdx = _SPDX_RE.search(head)
    if spdx:
        return spdx.group(1).strip()
    low = head.lower()
    # 顺序即优先级：更具体的许可文本先匹配
    if "mit license" in low or (
        "permission is hereby granted, free of charge" in low
        and "the software is provided \"as is\"" in low
    ):
        return "MIT"
    if "apache license" in low:
        return "Apache-2.0"
    if "bsd 3-clause" in low or (
        "redistribution and use in source and binary forms" in low
        and "neither the name" in low
    ):
        return "BSD-3-Clause"
    if "bsd 2-clause" in low:
        return "BSD-2-Clause"
    if "isc license" in low or "permission to use, copy, modify" in low:
        return "ISC"
    if "unlicense" in low or "this is free and unencumbered software" in low:
        return "Unlicense"
    if "cc0" in low or "creative commons zero" in low:
        return "CC0-1.0"
    return None
