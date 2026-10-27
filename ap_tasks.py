# -*- coding: utf-8 -*-
"""任务合并、到期判定、状态持久化。**不碰网络、不发消息**。

配置里的任务（`_conf_schema.json` 的 `watch_tasks`）只读；AI 自建的任务存在
`data/plugin_data/ai_being/tasks.json`；两者直接拼接（重复 id 并存）。启停覆盖与水位线存在
`state.json`，所以 AI 关掉一条配置任务不会改写配置文件。
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path

try:
    from . import ap_config, ap_cron
except ImportError:  # 插件以顶层模块方式加载时
    import ap_config
    import ap_cron


def merge(tasks, toggles) -> list:
    """返回任务列表。

    ⚠️ 任务只有**一份**（配置里的 `watch_tasks`）—— AI 建的任务也写在这里，
    所以面板上看得见、保存配置也不会冲掉。
    """
    # 参数保留只为兼容既有调用点；**配置是唯一权威**。
    # 以前这里拿 `toggles`（state.json 里的内存覆盖）盖住配置的 `enabled`，
    # 结果用户在面板上重新启用后仍不触发：配置是开的，toggles 还记着关。
    # 现在 AI 的开关直接写进配置，`toggles` 不再参与。
    return list(tasks or [])


def due(tasks, moment: datetime, fired_minute=None) -> list:
    """这一分钟该触发的任务。

    `fired_minute` 是调用方持有的 `{分钟键: set(task_id)}` 字典，用来保证
    「同一分钟只触发一次」
    （tick 可能在一分钟内跑两遍）。判定通过的任务会被写进去。
    """
    key = moment.strftime("%Y%m%d%H%M")
    out = []
    for task in tasks or []:
        if not task.enabled or not str(task.session or "").strip():
            continue
        if fired_minute is not None and task.id in _fired_for(fired_minute, key):
            continue
        if not ap_cron.valid(task.cron) or not ap_cron.matches(task.cron, moment):
            continue
        if ap_config.quiet_now(task.quiet_hours, moment.hour):
            continue
        if fired_minute is not None:
            _mark_fired(fired_minute, key, task.id)
        out.append(task)
    return out


def _fired_for(store, key):
    """`store` 形如 `{"202606011210": {"a", "b"}}`；旧的分钟条目由 `_mark_fired` 清理。"""
    if not isinstance(store, dict):
        return set()
    return store.get(key, set())


def unmark_fired(store, task_id) -> None:
    """把某条任务从「本分钟已触发」里撤掉，允许它下一分钟重试。

    触发失败（注入异常、配置写回失败）时调它 —— 否则这一分钟就白白跳过了。
    """
    if not isinstance(store, dict):
        return
    for bucket in store.values():
        if isinstance(bucket, set):
            bucket.discard(str(task_id))

def _mark_fired(store, key, task_id):
    if not isinstance(store, dict):
        return
    for old in [k for k in store if k != key]:
        store.pop(old, None)  # 只保留当前分钟，避免无限增长
    store.setdefault(key, set()).add(task_id)


class StateStore:
    """游标 / 水位线 / 启停覆盖，全部落一个 JSON。"""

    def __init__(self, data_dir) -> None:
        self.dir = Path(data_dir)
        self.path = self.dir / "state.json"
        self.cursor = 0
        self.dropped_until_seq = 0
        self._watermarks = {}
        self._toggles = {}

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self.cursor = _as_int(raw.get("cursor"))
        marks = raw.get("watermarks")
        self._watermarks = (
            {str(k): _as_int(v) for k, v in marks.items()}
            if isinstance(marks, dict) else {}
        )
        toggles = raw.get("toggles")
        self._toggles = (
            {str(k): bool(v) for k, v in toggles.items()}
            if isinstance(toggles, dict) else {}
        )
        self.dropped_until_seq = _as_int(raw.get("dropped_until_seq"))
        # 向后兼容：旧版本存的是布尔 dropped_pending，升级时转成区间，
        # 否则旧部署会留一个永远清不掉的死标志。
        if not self.dropped_until_seq and raw.get("dropped_pending"):
            self.dropped_until_seq = _as_int(self.cursor)

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "cursor": _as_int(self.cursor),
            "watermarks": dict(self._watermarks),
            "toggles": dict(self._toggles),
            "dropped_until_seq": _as_int(self.dropped_until_seq),
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, self.path)

    def watermark(self, task_id) -> int:
        return int(self._watermarks.get(str(task_id), 0))

    def set_watermark(self, task_id, seq) -> None:
        self._watermarks[str(task_id)] = _as_int(seq)

    def mark_dropped_until(self, seq) -> None:
        """记录「截至这个 seq 为止，中间有事件丢失」。只进不退。"""
        value = _as_int(seq)
        if value > self.dropped_until_seq:
            self.dropped_until_seq = value

    def dropped_for(self, watermark) -> bool:
        """这个水位线的任务，是否被丢失区间覆盖。"""
        return _as_int(watermark) < _as_int(self.dropped_until_seq)

    def clear_dropped_if_all_passed(self, watermarks) -> None:
        """所有任务都已越过丢失区间时才清除标记。"""
        marks = [_as_int(v) for v in (watermarks or [])]
        if marks and min(marks) >= _as_int(self.dropped_until_seq):
            self.dropped_until_seq = 0

    def rebase_watermarks(self, current_seq) -> list:
        """服务端 seq 回退/归零时，把超前的水位线拉回当前最大 seq。

        返回被修正的任务 id（按水位线字典顺序）。不修正等于让任务
        永远等一个不会再出现的序号 —— 表现是永久静默。
        """
        seq = _as_int(current_seq)
        fixed = []
        for task_id, value in self._watermarks.items():
            if value > seq:
                self._watermarks[task_id] = seq
                fixed.append(task_id)
        return fixed

    def is_enabled(self, task_id, default=True) -> bool:
        return bool(self._toggles.get(str(task_id), default))

    def set_enabled(self, task_id, value) -> None:
        self._toggles[str(task_id)] = bool(value)

    def forget_task(self, task_id) -> None:
        """删任务时一并清掉它的水位线与启停覆盖。

        ⚠️ 不清的话，重建同名任务会继承旧水位线（重读/漏读一段动态）
        和旧的停用状态（建了但永远不触发）。
        """
        key = str(task_id)
        self._watermarks.pop(key, None)
        self._toggles.pop(key, None)

    def toggles(self) -> dict:
        return dict(self._toggles)

    def ensure_watermarks(self, tasks, current_seq) -> None:
        """新任务从**当前最大 seq** 起步，不翻旧账。"""
        for task in tasks or []:
            if task.id not in self._watermarks:
                self._watermarks[task.id] = _as_int(current_seq)


def _as_int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


# ---------------------------------------------------------------- AI 任务

def enabled_types(tasks) -> list:
    """所有启用任务的类型并集。空列表 ⇒ 调用方不该轮询。"""
    out = []
    for task in tasks or []:
        if not task.enabled or not str(task.session or "").strip():
            continue
        for etype in task.types:
            if etype not in out:
                out.append(etype)
    return out
