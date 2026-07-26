"""Phase 5 (gated, NOT active): LLM-based extraction of availability signals
from official club/PL news, applied as bounded adjustments to expected
minutes.

Gate, per IMPLEMENTATION_PLAN.md: this phase is meant to activate only
after Phases 1-4 are adopted AND live calibration has >= 8 stable
comparisons. Neither condition is met as of this writing -- Phase 1
(team-strength) and Phase 4 (recency-weighted minutes) were built and
backtested but NOT adopted (both are genuine negative results, disabled
via config -- see their status sections), and the 2026/27 season has not
started, so there are zero live forecast comparisons to calibrate against.

This module is scaffolding: implemented and tested with recorded fixtures,
but deliberately NOT wired into the live refresh pipeline. Built at
explicit user request to complete the plan's phases regardless of the
gate, so the infrastructure is ready whenever the gate is later met --
nothing here is called by project_players() or refresh.py.

The model is used only as a feature extractor over first-party news
(official club sites, the Premier League site) -- never as the projection
itself, which stays a fully transparent formula. Every extracted signal
carries the exact quote and source URL, the same provenance treatment
confirmed transfers already get, and any adjustment is bounded to a
fraction of the pre-adjustment estimate so a single misread headline
cannot swing a projection.

Provider-agnostic by design: this module does not assume any one LLM
vendor. Two callers are built in --

- "claude": the Claude Messages API (default, unchanged from the original
  implementation).
- "openai_compatible": any endpoint implementing the widely-used OpenAI
  Chat Completions shape -- covers most third-party model hosts, so a
  provider serving a Hermes model (or anything else) works without a
  code change, just environment variables (see _call_openai_compatible).

Both are raw HTTPS calls, no vendor SDK dependency, keeping this module
dependency-free like the rest of the project (only an API key is
required, no pip install). Select the provider via the
FPL_INTEL_LLM_PROVIDER environment variable, or the ``provider`` argument
to extract_availability_signals.
"""

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .transfers import is_trusted_https_url


_FIRST_PARTY_NEWS_DOMAINS = {
    "premierleague.com", "arsenal.com", "avfc.co.uk", "afcb.co.uk",
    "brentfordfc.com", "brightonandhovealbion.com", "burnleyfootballclub.com",
    "chelseafc.com", "cpfc.co.uk", "evertonfc.com", "fulhamfc.com",
    "leedsunited.com", "liverpoolfc.com", "mancity.com", "manutd.com",
    "nufc.co.uk", "nottinghamforest.co.uk", "safc.com", "tottenhamhotspur.com",
    "whufc.com", "wolves.co.uk",
}


_CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
_CLAUDE_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"

# Generic OpenAI Chat Completions-compatible provider: no vendor endpoint is
# hardcoded here, since this project doesn't guess at a third party's API
# details -- point it at whichever host serves the model you want (a Hermes
# host, or anything else implementing this API shape) via env vars.
_OPENAI_COMPATIBLE_API_BASE_ENV_VAR = "FPL_INTEL_LLM_API_BASE"
_OPENAI_COMPATIBLE_MODEL_ENV_VAR = "FPL_INTEL_LLM_MODEL"
_OPENAI_COMPATIBLE_API_KEY_ENV_VAR = "FPL_INTEL_LLM_API_KEY"

_PROVIDER_ENV_VAR = "FPL_INTEL_LLM_PROVIDER"
_DEFAULT_PROVIDER = "claude"

_MAX_ADJUSTMENT_FRACTION = 0.25

_EXTRACTION_SYSTEM_PROMPT = """You extract player availability signals from official football club and Premier League news for a fantasy football projection tool.

Read the provided news item and identify any explicit statements about a named player's injury status, expected return date, or role/rotation plans. Only extract signals with a direct textual basis in the provided text -- never infer or guess.

Respond with only a JSON array, no other text. Each element:
{
  "player": "<player name as written>",
  "availability_signal": "<one of: injured, doubtful, returning, rotation_risk, nailed_on>",
  "expected_return": "<free text, or null if not stated>",
  "role_hint": "<free text on role/position/set-pieces, or null>",
  "confidence": "<high|medium|low>",
  "exact_quote": "<the exact sentence(s) supporting this signal>"
}

If the text contains no such signal, respond with an empty JSON array: []
Never fabricate a quote. Never include a player not named in the text."""


def _read_api_key(env_var):
    return os.environ.get(env_var)


def _call_claude(news_text, api_key, timeout=30):
    """Raw HTTPS call to the Claude Messages API. Returns the response's
    text content, or None on any failure (network, auth, malformed
    response) -- callers must treat None as "no signals available", never
    raise into the refresh pipeline.
    """
    body = json.dumps({
        "model": _CLAUDE_MODEL,
        "max_tokens": 1024,
        "system": _EXTRACTION_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": news_text}],
    }).encode("utf-8")
    request = Request(
        _CLAUDE_API_URL,
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (URLError, HTTPError, TimeoutError, OSError, ValueError):
        return None
    content = payload.get("content") or []
    text_blocks = [block.get("text", "") for block in content if block.get("type") == "text"]
    return "".join(text_blocks) if text_blocks else None


def _call_openai_compatible(news_text, api_key, timeout=30):
    """Raw HTTPS call to any OpenAI Chat Completions-compatible endpoint.

    Nothing about the host is hardcoded -- this project doesn't guess at a
    third party's API details -- so both the base URL and model name come
    from environment variables (FPL_INTEL_LLM_API_BASE, FPL_INTEL_LLM_MODEL).
    Missing either, or any network/auth/parse failure, returns None -- same
    fail-safe contract as _call_claude.
    """
    api_base = os.environ.get(_OPENAI_COMPATIBLE_API_BASE_ENV_VAR)
    model = os.environ.get(_OPENAI_COMPATIBLE_MODEL_ENV_VAR)
    if not api_base or not model:
        return None
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": news_text},
        ],
    }).encode("utf-8")
    request = Request(
        api_base.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (URLError, HTTPError, TimeoutError, OSError, ValueError):
        return None
    choices = payload.get("choices") or []
    if not choices:
        return None
    return (choices[0].get("message") or {}).get("content")


# Each provider maps to the caller that speaks its API and the env var its
# key is read from. Add an entry here to support another provider without
# touching extract_availability_signals.
_PROVIDERS = {
    "claude": {"caller": _call_claude, "api_key_env_var": _CLAUDE_API_KEY_ENV_VAR},
    "openai_compatible": {"caller": _call_openai_compatible, "api_key_env_var": _OPENAI_COMPATIBLE_API_KEY_ENV_VAR},
}


def extract_availability_signals(news_item, api_key=None, caller=None, provider=None):
    """Extract structured availability signals from one first-party news item.

    ``news_item``: {"source_url": str, "text": str}. Returns a list of
    signal dicts, each carrying ``source_url`` for provenance. Returns an
    empty list on any failure -- no API key, network error, malformed or
    empty response, unrecognized provider, or a malformed individual item
    -- so a caller's pipeline runs identically with zero signals rather
    than raising or fabricating a claim.

    ``provider`` selects which LLM backend to call (see _PROVIDERS);
    defaults to the FPL_INTEL_LLM_PROVIDER env var, or "claude" if unset.
    ``caller`` overrides the provider's default caller -- used by tests
    (see tests/test_news_signals.py) so tests never make a live API call,
    and available to plug in a provider not yet in _PROVIDERS.
    """
    provider = provider or os.environ.get(_PROVIDER_ENV_VAR, _DEFAULT_PROVIDER)
    provider_config = _PROVIDERS.get(provider)
    if caller is None:
        if provider_config is None:
            return []
        caller = provider_config["caller"]
    if api_key is None and provider_config is not None:
        api_key = _read_api_key(provider_config["api_key_env_var"])
    if not api_key or not news_item.get("text"):
        return []
    raw_text = caller(news_item["text"], api_key)
    if not raw_text:
        return []
    try:
        parsed = json.loads(raw_text)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    signals = []
    for item in parsed:
        if not isinstance(item, dict) or not item.get("player") or not item.get("exact_quote"):
            continue
        signals.append({
            "player": item.get("player"),
            "availability_signal": item.get("availability_signal"),
            "expected_return": item.get("expected_return"),
            "role_hint": item.get("role_hint"),
            "confidence": item.get("confidence"),
            "exact_quote": item.get("exact_quote"),
            "source_url": news_item.get("source_url"),
        })
    return signals


def bounded_minutes_adjustment(expected_minutes, availability_signal, max_adjustment_fraction=_MAX_ADJUSTMENT_FRACTION):
    """Apply a bounded adjustment to an expected-minutes estimate.

    Never moves the estimate by more than ``max_adjustment_fraction`` of
    its original value, regardless of the signal -- a single news item
    should nudge a projection, never override it outright. Unrecognized or
    neutral signals ("rotation_risk" aside) leave the estimate unchanged.
    """
    direction = {
        "injured": -1.0,
        "doubtful": -0.5,
        "rotation_risk": -0.3,
        "returning": 0.4,
        "nailed_on": 0.3,
    }.get(availability_signal, 0.0)
    if direction == 0.0 or expected_minutes <= 0:
        return round(expected_minutes, 1)
    adjustment = direction * max_adjustment_fraction * expected_minutes
    adjusted = expected_minutes + adjustment
    return round(max(0.0, min(90.0, adjusted)), 1)


def fetch_news_item(url, timeout=30):
    """Fetch a first-party news page's raw text for signal extraction.

    Scope note: this fetches a single already-known URL's HTML and returns
    a crude tag-stripped text extraction. It is not a site-specific
    scraper for every Premier League club's news section -- building a
    real per-club news index (consistent with the first-party-only source
    rule the transfer collector already follows) is future work; what's
    validated here is the extractor, given some first-party text.
    """
    if not is_trusted_https_url(url, _FIRST_PARTY_NEWS_DOMAINS):
        raise ValueError("News URL must use HTTPS on an allowlisted first-party domain")
    request = Request(url, headers={"User-Agent": "fpl-intelligence/1.0"})
    with urlopen(request, timeout=timeout) as response:
        html = response.read().decode("utf-8", errors="ignore")
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return {"source_url": url, "text": text}
