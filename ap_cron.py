# -*- coding: utf-8 -*-
"""5 字段 cron（分 时 日 月 周）。**纯函数**，不调度、不记日志。

只支持心跳需要的语法：`*`、`a`、`a-b`、`*/n`、`a-b/n`、逗号列表。
非法表达式一律返回 False —— 调用方在启动时用 `valid()` 响亮地警告一次，
之后不再重复（本项目最忌的是「配错了但静默地按某种规则跑」）。
"""
from __future__ import annotations

from datetime import datetime

# (名字, 下界, 上界)。周按 cron 惯例 0 = 星期日。
_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 6),
)


def _parse_field(expr, lo, hi):
    """展开成一个 set；非法返回 None。

    ⚠️ 步长在**区间内**走：`0-30/10` = {0,10,20,30}。`a/n` 这种写法按
    「a 到 hi」处理，与主流 cron 实现一致。
    """
    out = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            return None
        step = 1
        if "/" in part:
            base, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError:
                return None
            if step <= 0:
                return None
        else:
            base = part
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start_text, _, end_text = base.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError:
                return None
        else:
            try:
                start = end = int(base)
            except ValueError:
                return None
            if "/" in part:
                # `a/n` 是「a 到 hi，每 n 个」—— 与 `*` 同源的分支，
                # 不能退化成只取 {a}（以前就是这样，注释与实现不符）。
                end = hi
        if start < lo or end > hi or start > end:
            return None
        out.update(range(start, end + 1, step))
    return out


def _parts(expr):
    if not isinstance(expr, str):
        return None
    parts = expr.split()
    if len(parts) != 5:
        return None
    return parts


def valid(expr: str) -> bool:
    parts = _parts(expr)
    if parts is None:
        return False
    for raw, (_name, lo, hi) in zip(parts, _FIELDS):
        if _parse_field(raw, lo, hi) is None:
            return False
    return True


def matches(expr: str, moment: datetime) -> bool:
    parts = _parts(expr)
    if parts is None:
        return False
    values = (
        moment.minute,
        moment.hour,
        moment.day,
        moment.month,
        # Python: 周一=0 … 周日=6；cron: 周日=0 … 周六=6
        (moment.weekday() + 1) % 7,
    )
    for raw, (_name, lo, hi), value in zip(parts, _FIELDS, values):
        allowed = _parse_field(raw, lo, hi)
        if allowed is None or value not in allowed:
            return False
    return True
