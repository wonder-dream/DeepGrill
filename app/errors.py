"""异常层次（ADR-0005 的基础设施层）。

v1 的教训有两条，方向相反，都要保留：

① **降级可以，静默不行**（AGENTS.md §3.1）：v1 有 15 处 `except Exception`
   捕获后只写日志、不 re-raise，导致「界面显示成功但数据没落库」。所以本模块
   只定义**显式**的异常类型，并保留 v1 的好习惯：对外不回显内部细节。

② **`AppError` → 统一文案**：v1 的处理器是刻意不回显内部细节的（不把栈或 SQL
   泄漏给用户），但**必须留下可查的记录** —— 这条由日志与 `task_logs`/`jobs`
   承担（ADR-0006），不是由异常处理器承担。
"""

from __future__ import annotations


class AppError(Exception):
    """所有"预期内"的失败的基类。

    预期内 = 有明确处置方式的失败（参数不对、没登录、题不存在…）。
    它不是 `except Exception` 的替代品：捕到它说明你**知道**会发生什么。
    """

    #: 给用户看的文案。子类可覆盖。
    user_message = "请求无法完成"


class NotFound(AppError):
    user_message = "找不到这个资源"


class Forbidden(AppError):
    user_message = "没有权限"


class InvalidInput(AppError):
    user_message = "输入不合法"


class QuotaExhausted(AppError):
    """额度点耗尽。按决策 13：**降级为纯题库模式**，不做硬拒绝。"""

    user_message = "今日额度已用完，题库仍可浏览"
