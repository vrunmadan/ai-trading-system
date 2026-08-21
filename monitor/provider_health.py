"""
LLM provider reachability + quota checks.

Why this exists: the OpenAI account ran out of quota and the QC fact-checker
failed on every call for an unknown stretch. `/status` reported "ok" the whole
time, because it only ever checked whether keys were *set* — never whether
they *worked*. A key that is present, valid, and out of money looks identical
to a healthy one until you actually call the API.

Design notes:

- The probe is a real (tiny) API call, not a metadata call. `models.list()`
  succeeds fine on an account with zero quota, which is exactly the failure we
  need to catch, so it is useless as a health signal.

- Results are cached (PROVIDER_HEALTH_TTL_SECONDS, default 300) so hitting
  /status repeatedly does not spend tokens or rate-limit the account.

- The error CLASS is reported, never the key. Every message is passed through
  _sanitize() which strips anything shaped like a credential.
"""

import logging
import os
import re
import time

log = logging.getLogger(__name__)

TTL = int(os.getenv("PROVIDER_HEALTH_TTL_SECONDS", "300"))

# {provider: (checked_at_monotonic, result_dict)}
_cache: dict[str, tuple[float, dict]] = {}

# Anything credential-shaped, redacted before a result is ever returned.
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"AIza[A-Za-z0-9_\-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}", re.I),
    re.compile(r"api[_-]?key['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9._\-]{8,}", re.I),
]


def _sanitize(text: str, limit: int = 200) -> str:
    """Strip anything credential-shaped, then truncate."""
    out = str(text)
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out[:limit]


def _classify(exc: Exception) -> str:
    """
    Map an exception to a short, stable error class. Deliberately coarse — the
    point is to distinguish "out of money" from "bad key" from "provider down"
    at a glance, not to reproduce the provider's taxonomy.
    """
    status = getattr(exc, "status_code", None)
    blob = f"{type(exc).__name__} {exc}".lower()

    if "insufficient_quota" in blob or "exceeded your current quota" in blob:
        return "insufficient_quota"
    if "credit balance is too low" in blob or "billing" in blob:
        return "billing"
    if status == 401 or "invalid_api_key" in blob or "unauthorized" in blob:
        return "invalid_api_key"
    if status == 403 or "permission" in blob:
        return "forbidden"
    if status == 404 or "model_not_found" in blob or "does not exist" in blob:
        return "model_not_found"
    if status == 429 or "rate limit" in blob or "rate_limit" in blob:
        return "rate_limited"
    if "timeout" in blob or "timed out" in blob:
        return "timeout"
    if status and 500 <= int(status) < 600:
        return "provider_error"
    if "connection" in blob or "network" in blob or "dns" in blob:
        return "unreachable"
    return "unknown_error"


def _result(ok: bool, *, configured: bool = True, error_class: str = None,
            detail: str = "", note: str = "") -> dict:
    r = {"ok": ok, "configured": configured}
    if error_class:
        r["error_class"] = error_class
    if detail:
        r["detail"] = _sanitize(detail)
    if note:
        r["note"] = note
    return r


# ---------------------------------------------------------------------------
# Per-provider probes. Each makes the smallest possible real call.
# ---------------------------------------------------------------------------

def _probe_openai() -> dict:
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        return _result(False, configured=False, error_class="not_configured",
                       note="OPENAI_API_KEY not set — QC cannot run")
    try:
        from openai import OpenAI

        client = OpenAI(api_key=key, max_retries=0, timeout=15.0)
        client.chat.completions.create(
            model=os.getenv("QC_MODEL", "gpt-5.5"),
            # gpt-5.5 requires max_completion_tokens, not max_tokens (else 400).
            max_completion_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        return _result(True)
    except Exception as e:
        return _result(False, error_class=_classify(e), detail=str(e))


def _probe_anthropic() -> dict:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return _result(False, configured=False, error_class="not_configured",
                       note="ANTHROPIC_API_KEY not set — Researcher cannot run")
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=15.0)
        client.messages.create(
            model=os.getenv("RESEARCHER_MODEL", "claude-sonnet-5"),
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        return _result(True)
    except Exception as e:
        return _result(False, error_class=_classify(e), detail=str(e))


def _probe_google() -> dict:
    key = os.getenv("GOOGLE_API_KEY", "")
    if not key:
        return _result(False, configured=False, error_class="not_configured",
                       note="GOOGLE_API_KEY not set — weekly Auditor cannot run")
    try:
        import google.generativeai as genai

        genai.configure(api_key=key)
        # list_models is the cheapest call that still exercises auth here.
        next(iter(genai.list_models()), None)
        return _result(True)
    except Exception as e:
        return _result(False, error_class=_classify(e), detail=str(e))


_PROBES = {
    "openai": _probe_openai,
    "anthropic": _probe_anthropic,
    "google": _probe_google,
}

# Which pipeline stage each provider powers, and whether losing it stops trading.
_ROLES = {
    "openai": ("QC fact-checker", True),
    "anthropic": ("Researcher", True),
    "google": ("weekly Auditor", False),
}


def check_provider(name: str, force: bool = False) -> dict:
    """One provider's health, cached for TTL seconds."""
    now = time.monotonic()
    if not force:
        cached = _cache.get(name)
        if cached and (now - cached[0]) < TTL:
            out = dict(cached[1])
            out["cached"] = True
            out["age_seconds"] = int(now - cached[0])
            return out

    try:
        result = _PROBES[name]()
    except Exception as e:            # a probe must never take down /status
        result = _result(False, error_class="probe_failed", detail=str(e))

    role, critical = _ROLES[name]
    result["role"] = role
    result["critical"] = critical
    _cache[name] = (now, result)

    out = dict(result)
    out["cached"] = False
    return out


def check_all_providers(force: bool = False) -> dict:
    """
    Health for every provider plus an overall verdict.

    Overall is "degraded" when either critical provider is down: OpenAI powers
    QC (nothing ships without it) and Anthropic powers the Researcher (nothing
    is generated without it). Google only powers the weekly Auditor, so its
    loss is reported but does not degrade the trading path.
    """
    providers = {name: check_provider(name, force=force) for name in _PROBES}

    degraded_by = [
        name for name, r in providers.items()
        if r.get("critical") and not r.get("ok")
    ]

    # The persisted QC streak is free and reflects what actually happened in
    # the pipeline, rather than what a synthetic probe sees right now.
    try:
        from ledger.db import get_qc_error_streak

        streak = get_qc_error_streak()
    except Exception:
        streak = 0

    return {
        "providers": providers,
        "qc_consecutive_errors": streak,
        "ok": not degraded_by and streak == 0,
        "degraded_by": degraded_by,
    }


def reset_cache() -> None:
    _cache.clear()
