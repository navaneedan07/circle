"""Sentry observability (spec §55).

Tracks backend/parser/watcher/RAG/LLM failures and latency WITHOUT ever
sending raw private conversation content. Only safe metadata goes out:
source, operation, latency, record count, error type, request id.

Disabled entirely (SDK never initialized) when SENTRY_ENABLED=false or no DSN.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from circle.config import get_settings

log = logging.getLogger("circle.sentry")

_initialized = False


def init_sentry() -> bool:
    global _initialized
    if _initialized:
        return True
    settings = get_settings()
    if not settings.sentry_enabled or not settings.sentry_dsn:
        log.info("Sentry disabled (no DSN or SENTRY_ENABLED=false)")
        return False
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            traces_sample_rate=0.2,
            profiles_sample_rate=0.2,
            send_default_pii=False,      # never send PII
            max_breadcrumbs=50,
            environment="production",
        )
        _initialized = True
        log.info("Sentry initialized")
        return True
    except Exception as e:
        log.warning("Sentry init failed: %s", e)
        return False


def capture_exception(op: str, error: Exception,
                      extra: Optional[dict[str, Any]] = None) -> None:
    """Report an error with SAFE metadata only (never message bodies)."""
    if not _initialized:
        return
    try:
        import sentry_sdk
        safe = {"operation": op}
        for k, v in (extra or {}).items():
            if k in ("source", "filename", "error_type", "record_count",
                     "latency_ms", "status", "request_id", "stage"):
                safe[k] = v if not isinstance(v, str) or len(v) < 200 else v[:200]
        with sentry_sdk.configure_scope() as scope:
            for k, v in safe.items():
                scope.set_tag(k, str(v))
        sentry_sdk.capture_exception(error)
    except Exception:
        pass


def capture_event(op: str, message: str,
                  extra: Optional[dict[str, Any]] = None) -> None:
    if not _initialized:
        return
    try:
        import sentry_sdk
        sentry_sdk.capture_message(f"{op}: {message[:150]}", level="warning")
    except Exception:
        pass


def trace_stage(op: str, latency_ms: int, extra: Optional[dict[str, Any]] = None) -> None:
    """Record AI-pipeline stage latency (question -> retrieval -> LLM)."""
    if not _initialized:
        return
    try:
        import sentry_sdk
        with sentry_sdk.configure_scope() as scope:
            scope.set_tag("stage", op)
            scope.set_tag("latency_ms", str(latency_ms))
            for k, v in (extra or {}).items():
                scope.set_tag(k, str(v)[:100])
        sentry_sdk.capture_message(f"stage {op} {latency_ms}ms", level="info")
    except Exception:
        pass
