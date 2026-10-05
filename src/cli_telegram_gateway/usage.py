"""Small, numeric snapshots of usage reported by the native CLIs."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def token_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def claude_context_window(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    model = value.get("model")
    window = token_count(value.get("rawMaxTokens")) or token_count(value.get("maxTokens"))
    if not isinstance(model, str) or not model or not window:
        return {}
    # This probe has no conversation. Its totalTokens/apiUsage must never
    # replace the selected session's last successful request usage.
    return {"model": model[:160], "context_window": window}


def codex_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    last = value.get("last")
    if isinstance(last, dict):
        for source, target in (
            ("totalTokens", "context_tokens"),
            ("inputTokens", "input_tokens"),
            ("outputTokens", "output_tokens"),
            ("cachedInputTokens", "cache_read_tokens"),
        ):
            count = token_count(last.get(source))
            if count is not None:
                result[target] = count
        if "context_tokens" in result:
            result["context_basis"] = "最近请求"
    window = token_count(value.get("modelContextWindow"))
    if window:
        result["context_window"] = window
    total = value.get("total")
    if isinstance(total, dict) and token_count(total.get("totalTokens")) is not None:
        result["total_tokens"] = total["totalTokens"]
    return result


def headless_usage(cli: str, value: dict[str, Any]) -> dict[str, Any]:
    kind = value.get("type")
    result: dict[str, Any] = {}
    if cli == "pi":
        if kind == "compaction_end" and not value.get("aborted") and not value.get("errorMessage"):
            return {"context_tokens": None}
        message = value.get("message")
        if kind != "message_end" or not isinstance(message, dict) or message.get("role") != "assistant":
            return {}
        if message.get("stopReason") in {"error", "aborted"}:
            return {}
        usage = message.get("usage")
        fields = {
            "input": "input_tokens", "output": "output_tokens",
            "cacheRead": "cache_read_tokens", "cacheWrite": "cache_write_tokens",
        }
    else:
        if value.get("parent_tool_use_id") or value.get("isSidechain"):
            return {}  # Subagent context is not the selected session's context.
        if value.get("isApiErrorMessage"):
            return {}
        if kind == "system" and value.get("subtype") == "compact_boundary":
            return {"context_tokens": None}
        message = value.get("message") if kind == "assistant" else value
        if kind not in {"assistant", "result", "system"} or not isinstance(message, dict):
            return {}
        usage = message.get("usage")
        fields = {
            "input_tokens": "input_tokens", "output_tokens": "output_tokens",
            "cache_read_input_tokens": "cache_read_tokens",
            "cache_creation_input_tokens": "cache_write_tokens",
        }
    model = message.get("model")
    if isinstance(model, str) and model:
        result["model"] = model[:160]
    counts = {}
    if isinstance(usage, dict):
        counts = {
            target: count for source, target in fields.items()
            if (count := token_count(usage.get(source))) is not None
        }
    if counts:
        if cli == "pi":
            result.update(counts)
            total = token_count(usage.get("totalTokens"))
            result["context_tokens"] = total if total else sum(counts.values())
            result["context_basis"] = "最近响应"
        elif kind == "assistant":
            # Result usage sums multiple API requests; it is not context occupancy.
            if "input_tokens" in counts:
                context = (
                    counts["input_tokens"] + counts.get("cache_read_tokens", 0)
                    + counts.get("cache_write_tokens", 0)
                )
                if not context:
                    return {}  # Synthetic error messages can carry all-zero usage.
                result["context_tokens"] = context
                result["context_basis"] = "最近请求输入"
        elif kind == "result":
            result.update(counts)
    if cli != "pi" and kind == "result":
        models = value.get("modelUsage")
        if isinstance(models, dict):
            windows = {
                model: window for model, metrics in models.items()
                if isinstance(metrics, dict) and (window := token_count(metrics.get("contextWindow")))
            }
            if windows:
                result["model_windows"] = windows
    return result


def merge_usage(previous: dict[str, Any] | None, update: dict[str, Any]) -> dict[str, Any]:
    result = dict(previous or {})
    model = update.get("model")
    model_changed = isinstance(model, str) and bool(model) and model != result.get("model")
    if model_changed:
        result.pop("context_window", None)
        result.pop("context_tokens", None)
    # Persist only known scalar fields, never raw CLI events or transcripts.
    for key in (
        "context_tokens", "context_window", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens", "total_tokens",
    ):
        count = token_count(update.get(key))
        if count is not None and (key != "context_window" or count > 0):
            result[key] = count
    if "context_tokens" in update and update["context_tokens"] is None:
        result["context_tokens"] = None
    for key in ("model", "context_basis"):
        if isinstance(update.get(key), str) and update[key]:
            result[key] = update[key][:160]
    windows = update.get("model_windows")
    if isinstance(windows, dict):
        window = windows.get(result.get("model"))
        if window is None and not result.get("model") and len(windows) == 1:
            window = next(iter(windows.values()))
        if token_count(window):
            result["context_window"] = window
    if "context_tokens" in update or model_changed:
        result.pop("reported_at", None)
    timestamp = update.get("reported_at")
    if not model_changed and "context_window" in update and set(update) <= {"model", "context_window"}:
        # Refreshing capacity does not make an old request's token usage fresh.
        # Use the locked, current snapshot so a concurrent live report wins.
        timestamp = result.get("reported_at") or result.get("updated_at")
    if isinstance(timestamp, str) and len(timestamp) <= 40:
        try:
            reported = datetime.fromisoformat(timestamp)
            if reported.tzinfo is not None:
                result["reported_at"] = reported.astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            pass
    result["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


def context_lines(usage: dict[str, Any] | None) -> list[str]:
    usage = usage or {}
    tokens = token_count(usage.get("context_tokens"))
    window = token_count(usage.get("context_window"))
    if tokens is None:
        lines = ["上下文：暂无有效用量，等待 CLI 下一次上报。"]
        if window:
            lines.append(f"上下文窗口：{window:,} tokens")
    else:
        label = f"上下文（{usage.get('context_basis', '最近上报')}）"
        if window:
            lines = [
                f"{label}：{tokens:,} / {window:,} tokens（{tokens / window * 100:.1f}%）",
                f"剩余上下文：{max(0, window - tokens):,} tokens",
            ]
        else:
            lines = [f"{label}：{tokens:,} tokens", "上下文窗口：CLI 未上报，无法计算占比。"]
    if usage.get("model"):
        lines.append(f"用量模型：{usage['model']}")
    if reported_at := usage.get("reported_at") or usage.get("updated_at"):
        lines.append(f"用量更新：{reported_at}")
    return lines


def usage_lines(usage: dict[str, Any] | None) -> list[str]:
    usage = usage or {}
    parts = []
    for key, label in (
        ("input_tokens", "输入"), ("cache_read_tokens", "缓存读取"),
        ("cache_write_tokens", "缓存写入"), ("output_tokens", "输出"),
    ):
        count = token_count(usage.get(key))
        if count is not None:
            parts.append(f"{label} {count:,}")
    lines = ["CLI 上报用量：" + " · ".join(parts)] if parts else []
    total = token_count(usage.get("total_tokens"))
    if total is not None:
        lines.append(f"会话累计用量：{total:,} tokens")
    return lines


def _claude_reset_time(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 40:
        return ""
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is not None:
            return timestamp.astimezone(timezone.utc).strftime("%m-%d %H:%M UTC")
    except ValueError:
        pass
    return ""


def claude_quota_lines(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["账户额度：Claude 未返回额度数据。"]
    if value.get("rate_limits_available") is False:
        return ["账户额度：当前 Claude 登录方式未提供套餐额度。"]
    limits = value.get("rate_limits")
    if not isinstance(limits, dict):
        return ["账户额度：Claude 未返回额度数据。"]
    rows = limits.get("limits")
    if not isinstance(rows, list):
        # Older native responses expose fixed windows rather than meter rows.
        rows = [
            {"kind": kind, "percent": window.get("utilization"), "resets_at": window.get("resets_at"),
             "scope": {"model": {"display_name": scope}} if scope else None}
            for key, kind, scope in (
                ("five_hour", "session", ""), ("seven_day", "weekly_all", ""),
                ("seven_day_opus", "weekly_scoped", "Opus"),
                ("seven_day_sonnet", "weekly_scoped", "Sonnet"),
                ("seven_day_oauth_apps", "weekly_scoped", "OAuth 应用"),
            )
            if isinstance(window := limits.get(key), dict)
        ]
    detail = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        percent = row.get("percent")
        if type(percent) not in {int, float} or not math.isfinite(percent) or percent < 0:
            continue
        kind = row.get("kind")
        name = {"session": "5小时额度", "weekly_all": "7天额度", "weekly_scoped": "7天额度"}.get(
            kind if isinstance(kind, str) else "", "使用额度",
        )
        scope = row.get("scope")
        if isinstance(scope, dict):
            for key in ("model", "surface"):
                item = scope.get(key)
                label = item.get("display_name") if isinstance(item, dict) else None
                if isinstance(label, str) and label:
                    name += f" · {label[:80]}"
        reset = _claude_reset_time(row.get("resets_at"))
        detail.append(
            f"{name}：剩余 {max(0, 100 - percent):g}%（已用 {percent:g}%）"
            + (f" · 重置 {reset}" if reset else "")
        )
    extra = limits.get("extra_usage")
    if isinstance(extra, dict):
        if extra.get("is_enabled") is False:
            detail.append("额外用量：未启用")
        elif extra.get("is_enabled") is True:
            percent = extra.get("utilization")
            if type(percent) in {int, float} and math.isfinite(percent) and percent >= 0:
                detail.append(f"额外用量：已启用，已用 {percent:g}%")
            else:
                detail.append("额外用量：已启用，用量未上报")
    if not detail:
        return ["账户额度：Claude 未返回可用额度数据。"]
    plan = value.get("subscription_type")
    heading = "账户额度 · Claude" + (
        f"（{plan}）" if isinstance(plan, str) and plan in {"pro", "max", "team", "enterprise"} else ""
    )
    return [heading, *detail]


def _reset_time(value: Any) -> str:
    seconds = token_count(value)
    if seconds is None:
        return ""
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return ""


def _window_name(value: Any, fallback: str) -> str:
    minutes = token_count(value)
    if not minutes:
        return fallback
    if minutes % 1440 == 0:
        return f"{minutes // 1440}天额度"
    if minutes % 60 == 0:
        return f"{minutes // 60}小时额度"
    return f"{minutes}分钟额度"


def quota_lines(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["账户额度：CLI 未返回额度数据。"]
    buckets = value.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        buckets = {"codex": value.get("rateLimits")}
    lines: list[str] = []
    for bucket_id, bucket in list(buckets.items())[:6]:
        if not isinstance(bucket, dict):
            continue
        detail = []
        for key, fallback in (("primary", "主额度窗口"), ("secondary", "次额度窗口")):
            window = bucket.get(key)
            if not isinstance(window, dict):
                continue
            percent = window.get("usedPercent")
            if type(percent) not in {int, float} or not math.isfinite(percent) or percent < 0:
                continue
            name = _window_name(window.get("windowDurationMins"), fallback)
            reset = _reset_time(window.get("resetsAt"))
            detail.append(
                f"{name}：剩余 {max(0, 100 - percent):g}%（已用 {percent:g}%）"
                + (f" · 重置 {reset}" if reset else "")
            )
        credits = bucket.get("credits")
        if isinstance(credits, dict):
            if credits.get("unlimited") is True:
                detail.append("附加额度：不限量")
            else:
                try:
                    balance = Decimal(str(credits.get("balance")))
                    if balance.is_finite() and balance >= 0 and balance.adjusted() < 20:
                        detail.append(f"附加额度余额：{balance:.2f}（CLI 上报）")
                except InvalidOperation:
                    pass
                if not any(line.startswith("附加额度余额") for line in detail):
                    if credits.get("hasCredits") is True:
                        detail.append("附加额度：可用，余额未上报")
                    elif credits.get("hasCredits") is False:
                        detail.append("附加额度：无可用额度")
        if bucket.get("rateLimitReachedType"):
            detail.append("额度状态：已达到使用限制")
        if bucket.get("spendControlReached") is True:
            detail.append("额度状态：已达到支出限制")
        if detail:
            label = str(bucket.get("limitName") or bucket.get("limitId") or bucket_id)[:100]
            plan = bucket.get("planType")
            lines.append(f"账户额度 · {label}" + (f"（{str(plan)[:40]}）" if plan else ""))
            lines.extend(detail)
    if value.get("ordinaryUsageAllowed") is False:
        lines.append("账户：当前不允许使用套餐额度")
    return lines or ["账户额度：CLI 未返回可用的额度数据。"]
