"""部署产物的测试（决策 73）。

`.service` / `.timer` 文件最容易腐烂的方式是**它们引用的命令已经不存在了** ——
没有人会在改 CLI 时想起 `/etc/systemd/system` 里的那几行。而它坏掉的表现是
凌晨三点备份静默失败，或者重启后服务起不来。所以这里把三件事钉住：

· 单元里写的每个 `app.cli <子命令>` **真的是 CLI 认识的子命令**（跑一次 `--help`）
· 单元里**不跑迁移**（ADR-0008 / 决策 55：迁移必须是与启动无关的显式步骤）
· 定时器必须 `Persistent=true`（错过的那些次要在开机后补跑 —— "看起来一直在跑、
  其实三天没跑"就是这么来的）

⚠️ 它不启动 systemd（CI 里没有），它校验的是**这些文件与代码的一致性**。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import cli

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"

#: systemd 的行续接是 `\` + 换行 —— 解析前先接回来，否则 `ExecStart` 只读到一半
def _unit_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\\\n", " ")


def _units() -> list[Path]:
    return sorted(DEPLOY.glob("*.service"))


def _exec_lines(path: Path) -> list[str]:
    return [
        line.split("=", 1)[1].strip()
        for line in _unit_text(path).splitlines()
        if line.startswith("ExecStart=") or line.startswith("ExecStartPost=")
    ]


def test_the_deploy_directory_is_not_empty() -> None:
    """没有单元文件时，下面每条测试都会"通过" —— 先把这个前提钉住。"""
    assert len(_units()) >= 2
    assert list(DEPLOY.glob("*.timer"))


@pytest.mark.parametrize("unit", _units(), ids=lambda p: p.name)
def test_every_command_in_the_units_is_a_known_entry_point(unit: Path) -> None:
    """**这条是本次的重点**：单元里写的命令，CLI 必须真的认识。

    判据不是"字符串对得上"，而是**跑一次 argparse**：`--help` 让 argparse 在派发
    之前就退出（0 = 认这个子命令，2 = 不认）。这样改 CLI 的子命令名时，这里会红。

    每个 `ExecStart` 只许是两种东西之一：`app.cli <子命令>` 或 uvicorn ——
    多出第三种就说明有人在单元里塞了别的东西（脚本、shell 管道、`cd &&` 链），
    而那些在 systemd 里连"失败"都不会好好报。
    """
    lines = _exec_lines(unit)
    assert lines, f"{unit.name} 里没有 ExecStart"
    for line in lines:
        match = re.search(r"-m app\.cli ([a-z-]+)", line)
        if match:
            with pytest.raises(SystemExit) as exited:
                cli.main([match.group(1), "--help"])
            assert exited.value.code == 0, (
                f"{unit.name} 引用了不存在的子命令：{match.group(1)}"
            )
            continue
        assert "-m uvicorn app.main:app" in line, (
            f"{unit.name} 里的这条命令不是已知入口：{line}"
        )


def test_no_unit_runs_migrations() -> None:
    """**迁移不许挂在启动上**（ADR-0008 / 决策 55）。

    启动时自动迁移会把"回滚一次发布"变成"回滚一次数据" —— 而 v1 的
    `_migrate_users_email` 正是在启动时 `DROP TABLE users`。
    """
    for unit in _units():
        for line in _exec_lines(unit):
            assert "migrations.run" not in line, f"{unit.name} 在启动/收尾里跑了迁移"


def test_the_web_unit_serves_one_worker_on_loopback() -> None:
    """2C2G 单机：一个 worker（多开只会让 SQLite 的写锁互相排队），且只监听回环 ——
    对外暴露与 TLS 由 Cloudflare + 反代负责（ADR-0008）。"""
    text = _unit_text(DEPLOY / "deepgrill-web.service")
    assert "--workers 1" in text
    assert "--host 127.0.0.1" in text
    assert "EnvironmentFile=" in text
    assert "NoNewPrivileges=true" in text


def test_every_unit_has_the_survives_a_reboot_basics() -> None:
    """`Restart` / `User` / `EnvironmentFile` 缺一个都会在真机器上出问题，
    而在本地"看起来没问题"（手工跑当然没事）。"""
    for unit in _units():
        text = _unit_text(unit)
        assert "User=deepgrill" in text, f"{unit.name} 没指定用户（会用 root 跑）"
        assert "EnvironmentFile=" in text, f"{unit.name} 拿不到配置与密钥"
        assert "WorkingDirectory=" in text, f"{unit.name} 的工作目录是随机的"


def test_the_backup_timer_is_persistent() -> None:
    """错过的定时要在开机后补跑。`Persistent=false` 的失败方式是**静默的**
    —— 只有等到真需要备份的那一天才会发现它三天没跑。"""
    text = _unit_text(DEPLOY / "deepgrill-backup.timer")
    assert "Persistent=true" in text
    assert "OnCalendar=" in text
    assert "WantedBy=timers.target" in text


def test_the_maintenance_timer_enqueues_then_drains() -> None:
    """增量维护两步：先**投递**（不调模型）再跑空队列（决策 46）。

    顺序反了不行：`worker --once` 先跑时队列还是空的，任务要等到下一次才被处理。
    """
    lines = [
        line for line in _unit_text(DEPLOY / "deepgrill-maintenance.service").splitlines()
        if line.startswith("ExecStart=")
    ]
    assert "generate --enqueue" in lines[0]
    assert "worker --once" in lines[1]


def test_the_readme_carries_the_three_must_flip_switches() -> None:
    """三个开关**线上必须开**，而它们的默认值是为了本机开发方便才关着的。
    部署文档不写它们，就等于让每个人自己想起来（决策 58 / 66）。"""
    text = (DEPLOY / "README.md").read_text(encoding="utf-8")
    for switch in (
        "DEEPGRILL_REQUIRE_SECURE_DB=true",
        "DEEPGRILL_SESSION_COOKIE_SECURE=true",
        "DEEPGRILL_TRUST_PROXY_HEADERS=true",
    ):
        assert switch in text, f"部署文档没写 {switch}"
    assert "verify-backup" in text, "没写恢复演练 —— 没演练过的备份不算备份"
