"""Small, numeric snapshots of usage reported by the native CLIs."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .i18n import N_, tr


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
            result["context_basis"] = "request"
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
            result["context_basis"] = "response"
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
                result["context_basis"] = "request_input"
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


# Snapshots store a basis code; the label is rendered in the configured language.
CONTEXT_BASIS_LABELS = {
    "request": N_("latest request"),
    "response": N_("latest response"),
    "request_input": N_("latest request input"),
    "history": N_("latest successful request input · history"),
}
# Snapshots saved before GATEWAY_LANGUAGE existed hold the Chinese label itself.
# Remove once no state file predates it (each session's next report replaces it).
LEGACY_CONTEXT_BASIS = {
    "最近请求": "request",
    "最近响应": "response",
    "最近请求输入": "request_input",
    "最近成功请求输入 · 历史记录": "history",
}


def _context_basis_label(basis: Any) -> str:
    code = LEGACY_CONTEXT_BASIS.get(basis, basis) if isinstance(basis, str) else None
    label = CONTEXT_BASIS_LABELS.get(code) if code else None
    return tr(label) if label else tr("latest report")


def context_lines(usage: dict[str, Any] | None) -> list[str]:
    usage = usage or {}
    tokens = token_count(usage.get("context_tokens"))
    window = token_count(usage.get("context_window"))
    if tokens is None:
        lines = [tr("Context: no valid usage yet; waiting for the CLI's next report.")]
        if window:
            lines.append(tr("Context window: {window:,} tokens", window=window))
    else:
        label = tr("Context ({basis})", basis=_context_basis_label(usage.get("context_basis")))
        if window:
            lines = [
                tr(
                    "{label}: {tokens:,} / {window:,} tokens ({percent:.1f}%)",
                    label=label, tokens=tokens, window=window, percent=tokens / window * 100,
                ),
                tr("Context left: {tokens:,} tokens", tokens=max(0, window - tokens)),
            ]
        else:
            lines = [
                tr("{label}: {tokens:,} tokens", label=label, tokens=tokens),
                tr("Context window: not reported by the CLI, so the share is unknown."),
            ]
    if usage.get("model"):
        lines.append(tr("Usage model: {model}", model=usage["model"]))
    if reported_at := usage.get("reported_at") or usage.get("updated_at"):
        lines.append(tr("Usage updated: {time}", time=reported_at))
    return lines


def usage_lines(usage: dict[str, Any] | None) -> list[str]:
    usage = usage or {}
    parts = []
    for key, label in (
        ("input_tokens", N_("input")), ("cache_read_tokens", N_("cache read")),
        ("cache_write_tokens", N_("cache write")), ("output_tokens", N_("output")),
    ):
        count = token_count(usage.get(key))
        if count is not None:
            parts.append(f"{tr(label)} {count:,}")
    lines = [tr("CLI-reported usage: {parts}", parts=" · ".join(parts))] if parts else []
    total = token_count(usage.get("total_tokens"))
    if total is not None:
        lines.append(tr("Session total: {total:,} tokens", total=total))
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
        return [tr("Account quota: Claude returned no quota data.")]
    if value.get("rate_limits_available") is False:
        return [tr("Account quota: this Claude sign-in has no plan quota.")]
    limits = value.get("rate_limits")
    if not isinstance(limits, dict):
        return [tr("Account quota: Claude returned no quota data.")]
    rows = limits.get("limits")
    if not isinstance(rows, list):
        # Older native responses expose fixed windows rather than meter rows.
        rows = [
            {"kind": kind, "percent": window.get("utilization"), "resets_at": window.get("resets_at"),
             "scope": {"model": {"display_name": tr(scope)}} if scope else None}
            for key, kind, scope in (
                ("five_hour", "session", ""), ("seven_day", "weekly_all", ""),
                ("seven_day_opus", "weekly_scoped", "Opus"),
                ("seven_day_sonnet", "weekly_scoped", "Sonnet"),
                ("seven_day_oauth_apps", "weekly_scoped", N_("OAuth apps")),
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
        name = tr({
            "session": N_("5-hour quota"), "weekly_all": N_("7-day quota"), "weekly_scoped": N_("7-day quota"),
        }.get(kind if isinstance(kind, str) else "", N_("Usage quota")))
        scope = row.get("scope")
        if isinstance(scope, dict):
            for key in ("model", "surface"):
                item = scope.get(key)
                label = item.get("display_name") if isinstance(item, dict) else None
                if isinstance(label, str) and label:
                    name += f" · {label[:80]}"
        reset = _claude_reset_time(row.get("resets_at"))
        detail.append(
            tr("{name}: {left:g}% left ({used:g}% used)", name=name, left=max(0, 100 - percent), used=percent)
            + (tr(" · resets {time}", time=reset) if reset else "")
        )
    extra = limits.get("extra_usage")
    if isinstance(extra, dict):
        if extra.get("is_enabled") is False:
            detail.append(tr("Extra usage: off"))
        elif extra.get("is_enabled") is True:
            percent = extra.get("utilization")
            if type(percent) in {int, float} and math.isfinite(percent) and percent >= 0:
                detail.append(tr("Extra usage: on, {percent:g}% used", percent=percent))
            else:
                detail.append(tr("Extra usage: on, usage not reported"))
    if not detail:
        return [tr("Account quota: Claude returned no usable quota data.")]
    plan = value.get("subscription_type")
    heading = tr("Account quota · Claude") + (
        tr(" ({plan})", plan=plan) if isinstance(plan, str) and plan in {"pro", "max", "team", "enterprise"} else ""
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
        return tr("{days}-day quota", days=minutes // 1440)
    if minutes % 60 == 0:
        return tr("{hours}-hour quota", hours=minutes // 60)
    return tr("{minutes}-minute quota", minutes=minutes)


def quota_lines(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return [tr("Account quota: the CLI returned no quota data.")]
    buckets = value.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        buckets = {"codex": value.get("rateLimits")}
    lines: list[str] = []
    for bucket_id, bucket in list(buckets.items())[:6]:
        if not isinstance(bucket, dict):
            continue
        detail = []
        for key, fallback in (("primary", N_("Primary quota window")), ("secondary", N_("Secondary quota window"))):
            window = bucket.get(key)
            if not isinstance(window, dict):
                continue
            percent = window.get("usedPercent")
            if type(percent) not in {int, float} or not math.isfinite(percent) or percent < 0:
                continue
            name = _window_name(window.get("windowDurationMins"), tr(fallback))
            reset = _reset_time(window.get("resetsAt"))
            detail.append(
                tr("{name}: {left:g}% left ({used:g}% used)", name=name, left=max(0, 100 - percent), used=percent)
                + (tr(" · resets {time}", time=reset) if reset else "")
            )
        credits = bucket.get("credits")
        if isinstance(credits, dict):
            if credits.get("unlimited") is True:
                detail.append(tr("Credits: unlimited"))
            else:
                balance_shown = False
                try:
                    balance = Decimal(str(credits.get("balance")))
                    if balance.is_finite() and balance >= 0 and balance.adjusted() < 20:
                        detail.append(tr("Credit balance: {balance} (reported by the CLI)", balance=f"{balance:.2f}"))
                        balance_shown = True
                except InvalidOperation:
                    pass
                if not balance_shown:
                    if credits.get("hasCredits") is True:
                        detail.append(tr("Credits: available, balance not reported"))
                    elif credits.get("hasCredits") is False:
                        detail.append(tr("Credits: none available"))
        if bucket.get("rateLimitReachedType"):
            detail.append(tr("Quota status: usage limit reached"))
        if bucket.get("spendControlReached") is True:
            detail.append(tr("Quota status: spend limit reached"))
        if detail:
            label = str(bucket.get("limitName") or bucket.get("limitId") or bucket_id)[:100]
            plan = bucket.get("planType")
            lines.append(tr("Account quota · {label}", label=label) + (tr(" ({plan})", plan=str(plan)[:40]) if plan else ""))
            lines.extend(detail)
    if value.get("ordinaryUsageAllowed") is False:
        lines.append(tr("Account: plan quota can't be used right now"))
    return lines or [tr("Account quota: the CLI returned no usable quota data.")]
