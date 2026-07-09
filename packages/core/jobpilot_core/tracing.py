"""OpenTelemetry tracing + correlation-id logging (Phase 8a — observability).

Local-first, self-hosted: traces export to an OTLP collector when an endpoint is
configured (``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``settings.otel_exporter_otlp_endpoint``),
otherwise to the console for dev. No trace data leaves the box unless you point it at a
collector you run — which fits JobPilot's strict no-PII / GDPR posture (chosen over a
hosted trace service).

OTel is an **optional import**: if the SDK isn't installed (the dependency-light MCP
images) or tracing is disabled, every helper here is a no-op, so ``jobpilot_core`` never
hard-requires opentelemetry. This mirrors the embeddings-torch fallback.

Correlation: a contextvar carries ``{user_id, pipeline_id, stage}``. Spans attach it and
a logging filter injects it into every log line, so an agent run → its MCP tool calls →
its LLM calls stitch into one connected trace (in-process parent/child) correlated across
the Celery boundary by ``pipeline_id``.

**PII BOUNDARY (GDPR).** Only IDS, COUNTS, MODEL NAMES, STATUS LABELS and NUMBERS ever
become span attributes or log fields — never resume / CV / job / query CONTENT. String
attributes are truncated to :data:`_MAX_ATTR_LEN`, a safety net; the real guarantee is
call-site discipline (we pass counts, never text). ``token/cost`` accounting is exactly
the non-PII data we *want* on every LLM span.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
from typing import Any, Iterator

_MAX_ATTR_LEN = 200  # truncate string attributes — ids/labels/models only, never content

# {user_id, pipeline_id, stage} for the current logical unit of work.
_CORRELATION: contextvars.ContextVar[dict] = contextvars.ContextVar("jobpilot_correlation", default={})

# --- optional OTel import (no-op without it) ---------------------------------- #
try:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )

    _OTEL_AVAILABLE = True
except Exception:  # noqa: BLE001 - degrade to no-op tracing
    _OTEL_AVAILABLE = False

logger = logging.getLogger("jobpilot.tracing")

_provider = None   # our own TracerProvider (not the global) — avoids double-set warnings
_tracer = None
_enabled = False


# --------------------------------------------------------------------------- #
# Correlation ids
# --------------------------------------------------------------------------- #
def set_correlation(**fields: Any) -> contextvars.Token:
    """Merge ``user_id`` / ``pipeline_id`` / ``stage`` into the correlation context.
    Returns a token — pass it to :func:`reset_correlation` to restore."""
    current = dict(_CORRELATION.get())
    for k, v in fields.items():
        if v is None:
            current.pop(k, None)
        else:
            current[k] = str(v)
    return _CORRELATION.set(current)


def reset_correlation(token: contextvars.Token) -> None:
    try:
        _CORRELATION.reset(token)
    except (ValueError, LookupError):  # token from a different context (async hop)
        _CORRELATION.set({})


def get_correlation() -> dict:
    return dict(_CORRELATION.get())


# --------------------------------------------------------------------------- #
# Attribute coercion (the PII safety net)
# --------------------------------------------------------------------------- #
def _coerce(value: Any) -> Any | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    s = str(value)
    return s[:_MAX_ATTR_LEN]


# --------------------------------------------------------------------------- #
# Span handles — a thin wrapper so ``.set()`` works whether or not OTel is live
# --------------------------------------------------------------------------- #
class _NoopSpan:
    def set(self, **attrs: Any) -> "_NoopSpan":
        return self

    def record_llm(self, usage: Any) -> "_NoopSpan":
        return self


class _SpanHandle:
    def __init__(self, span: Any) -> None:
        self._span = span

    def set(self, **attrs: Any) -> "_SpanHandle":
        for k, v in attrs.items():
            cv = _coerce(v)
            if cv is not None:
                self._span.set_attribute(k, cv)
        return self

    def record_llm(self, usage: Any) -> "_SpanHandle":
        """Attach an :class:`~jobpilot_core.llm.LLMUsage`'s NON-PII accounting."""
        if usage is None:
            return self
        self.set(
            model=getattr(usage, "model", None),
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            batch=bool(getattr(usage, "batch", False)),
            est_cost_usd=round(getattr(usage, "cost_usd", 0.0), 6),
        )
        return self


# --------------------------------------------------------------------------- #
# Init
# --------------------------------------------------------------------------- #
def init_tracing(settings: Any = None, *, service_name: str = "jobpilot", exporter: Any = None) -> bool:
    """Configure tracing once per process. Returns whether tracing is live.

    Exporter precedence: an explicitly-passed ``exporter`` (tests inject an in-memory
    one) → OTLP if an endpoint is configured → console (dev). A no-op if OTel isn't
    installed or ``settings.tracing_enabled`` is False.
    """
    global _provider, _tracer, _enabled

    enabled = True if settings is None else bool(getattr(settings, "tracing_enabled", True))
    if not enabled or not _OTEL_AVAILABLE:
        _enabled = False
        return False

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        endpoint = (getattr(settings, "otel_exporter_otlp_endpoint", "") if settings else "") \
            or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            except Exception as exc:  # noqa: BLE001 - fall back to console rather than crash
                logger.warning("tracing: OTLP exporter unavailable (%s); using console", exc)
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        elif settings is not None and getattr(settings, "tracing_console_export", False):
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        # else: enabled but no exporter → spans are created (correlation still works)
        # but not exported. Keeps dev quiet until you point at a collector.

    _provider = provider
    _tracer = provider.get_tracer("jobpilot")
    _enabled = True
    return True


def is_enabled() -> bool:
    return _enabled and _tracer is not None


def shutdown() -> None:
    global _provider, _tracer, _enabled
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception:  # noqa: BLE001
            pass
    _provider, _tracer, _enabled = None, None, False


# --------------------------------------------------------------------------- #
# The span context manager
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    """Start a span (child of the current one, in-process). No-op if tracing is off.
    Correlation ids are attached automatically; ``outcome``/``error.type`` on exception."""
    if not is_enabled():
        yield _NoopSpan()
        return
    with _tracer.start_as_current_span(name) as otel_span:
        handle = _SpanHandle(otel_span)
        handle.set(**get_correlation())
        handle.set(**attrs)
        try:
            yield handle
            otel_span.set_attribute("outcome", "ok")
        except Exception as exc:  # noqa: BLE001 - record + re-raise
            otel_span.set_attribute("outcome", "error")
            otel_span.set_attribute("error.type", type(exc).__name__)
            raise


def traced_stage(stage: str):
    """Decorate an async runner ``fn(user_id, *args, pipeline_id=None, **kw)`` so its
    whole body runs inside a ``stage.<name>`` span with correlation ids set — MCP + LLM
    spans inside become its children, and logs during it carry the same ids. Result
    counts (status/new/qualified/…) are lifted onto the span (non-PII)."""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(user_id, *args, **kwargs):
            pid = kwargs.get("pipeline_id")
            token = set_correlation(user_id=user_id, pipeline_id=pid, stage=stage)
            try:
                with span(f"stage.{stage}", stage=stage, user_id=user_id, pipeline_id=pid) as sp:
                    result = await fn(user_id, *args, **kwargs)
                    if isinstance(result, dict):
                        for k in ("status", "new", "deduped", "fetched", "scored",
                                  "qualified", "disqualified", "analyzed", "embedded"):
                            if k in result:
                                sp.set(**{k: result[k]})
                    return result
            finally:
                reset_correlation(token)

        return wrapper

    return deco


def annotate_current_llm(usage: Any) -> None:
    """Attach LLM usage to whatever span is current (used by the llm.py choke point)."""
    if not is_enabled():
        return
    try:
        from opentelemetry import trace as _ot_trace

        _SpanHandle(_ot_trace.get_current_span()).record_llm(usage)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Structured logging with correlation ids (no PII — ids/counts only)
# --------------------------------------------------------------------------- #
class CorrelationFilter(logging.Filter):
    """Inject the current correlation ids onto every record so logs are correlatable
    to a trace. Never adds content — only user_id/pipeline_id/stage."""

    def filter(self, record: logging.LogRecord) -> bool:
        c = get_correlation()
        record.user_id = c.get("user_id", "-")
        record.pipeline_id = c.get("pipeline_id", "-")
        record.stage = c.get("stage", "-")
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Root logging with correlation ids in the format. Idempotent. Call once at
    process start (backend app startup, Celery worker init)."""
    root = logging.getLogger()
    fmt = ("%(asctime)s %(levelname)s %(name)s "
           "[user=%(user_id)s pipeline=%(pipeline_id)s stage=%(stage)s] %(message)s")
    filt = CorrelationFilter()
    if getattr(root, "_jobpilot_correlated", False):
        for h in root.handlers:
            h.addFilter(filt)
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(filt)
    root.addHandler(handler)
    root.setLevel(level)
    root._jobpilot_correlated = True  # type: ignore[attr-defined]
