"""线程安全的 Token 用量统计、增量计算与持久化合并。"""

from __future__ import annotations

import threading
from typing import Any

_USAGE_FIELDS = (
    "calls",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
)


def _usage_int(usage: Any, name: str) -> int:
    """从响应 usage 对象/字典读取整数字段，缺失或非数返回 0。"""
    value = getattr(usage, name, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(name)
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _hit_rate(hit: int, miss: int) -> float:
    total = hit + miss
    return round(hit / total, 4) if total else 0.0


def _normalize_usage_group(
    group: dict[str, dict[str, int]],
) -> dict[str, dict[str, Any]]:
    """规范化一组用量槽位，并重新计算各槽位缓存命中率。"""
    normalized: dict[str, dict[str, Any]] = {
        name: {field: _usage_int(values, field) for field in _USAGE_FIELDS}
        for name, values in group.items()
    }
    for slot in normalized.values():
        slot["cache_hit_rate"] = _hit_rate(
            slot["cache_hit_tokens"], slot["cache_miss_tokens"]
        )
    return normalized


def _usage_summary(
    by_tier: dict[str, dict[str, int]],
    by_stage: dict[str, dict[str, int]],
) -> dict[str, Any]:
    """生成规范汇总；总计仅由 tier 计算，stage 是同一用量的另一种归因维度。"""
    tiers = _normalize_usage_group(by_tier)
    stages = _normalize_usage_group(by_stage)
    totals: dict[str, Any] = dict.fromkeys(_USAGE_FIELDS, 0)
    for values in tiers.values():
        for field in _USAGE_FIELDS:
            totals[field] += values[field]
    totals["cache_hit_rate"] = _hit_rate(
        totals["cache_hit_tokens"], totals["cache_miss_tokens"]
    )
    return {"totals": totals, "by_tier": tiers, "by_stage": stages}


def _usage_group_delta(
    current: dict[str, dict[str, int]], previous: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    delta: dict[str, dict[str, int]] = {}
    for name, values in current.items():
        old = previous.get(name) or {}
        slot = {
            field: max(0, _usage_int(values, field) - _usage_int(old, field))
            for field in _USAGE_FIELDS
        }
        if any(slot.values()):
            delta[name] = slot
    return delta


def _merge_usage_groups(
    *groups: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for group in groups:
        for name, values in group.items():
            slot = merged.setdefault(name, dict.fromkeys(_USAGE_FIELDS, 0))
            for field in _USAGE_FIELDS:
                slot[field] += _usage_int(values, field)
    return merged


def usage_delta(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """计算两个累计快照之间的非负增量，用于避免重复落盘。"""
    tier_delta = _usage_group_delta(current["by_tier"], previous["by_tier"])
    stage_delta = _usage_group_delta(current["by_stage"], previous["by_stage"])
    return _usage_summary(tier_delta, stage_delta)


def merge_usage_summaries(
    accumulated: dict[str, Any], increment: dict[str, Any]
) -> dict[str, Any]:
    """把一次运行增量合并进某本书的历史累计用量。"""
    tiers = _merge_usage_groups(accumulated["by_tier"], increment["by_tier"])
    stages = _merge_usage_groups(accumulated["by_stage"], increment["by_stage"])
    return _usage_summary(tiers, stages)


class UsageTracker:
    """线程安全的 token 用量累加器，按 tier 和调用 stage 分别归因。

    DeepSeek 的 usage 里 prompt_cache_hit_tokens + prompt_cache_miss_tokens == prompt_tokens；
    缓存命中率 = cache_hit /(cache_hit + cache_miss)。fake provider 不产生 usage，保持空。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_tier: dict[str, dict[str, int]] = {}
        self._by_stage: dict[str, dict[str, int]] = {}

    def record(self, tier: str, usage: Any, stage: str | None = None) -> None:
        """累加一次响应的 usage；usage 缺失时静默跳过（不影响正常返回）。"""
        if usage is None:
            return
        prompt_tokens = _usage_int(usage, "prompt_tokens")
        completion_tokens = _usage_int(usage, "completion_tokens")
        total_tokens = _usage_int(usage, "total_tokens") or (
            prompt_tokens + completion_tokens
        )
        cache_hit_tokens = _usage_int(usage, "prompt_cache_hit_tokens")
        cache_miss_tokens = _usage_int(usage, "prompt_cache_miss_tokens")
        with self._lock:
            slots = [
                self._by_tier.setdefault(tier, dict.fromkeys(_USAGE_FIELDS, 0))
            ]
            if stage:
                slots.append(
                    self._by_stage.setdefault(stage, dict.fromkeys(_USAGE_FIELDS, 0))
                )
            for slot in slots:
                slot["calls"] += 1
                slot["prompt_tokens"] += prompt_tokens
                slot["completion_tokens"] += completion_tokens
                slot["total_tokens"] += total_tokens
                slot["cache_hit_tokens"] += cache_hit_tokens
                slot["cache_miss_tokens"] += cache_miss_tokens

    def summary(self) -> dict[str, Any]:
        """返回 totals、by_tier 和 by_stage，各槽位含 cache_hit_rate。"""
        with self._lock:
            by_tier = {tier: dict(values) for tier, values in self._by_tier.items()}
            by_stage = {
                stage: dict(values) for stage, values in self._by_stage.items()
            }
        return _usage_summary(by_tier, by_stage)
