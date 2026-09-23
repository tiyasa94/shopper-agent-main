"""Small HTTP transport and credential-scoped IAM cache for WXO inference.

Sessions belong to one thread; tokens may be shared across threads in a worker.
No delegated user credentials or shopper context are retained here.
"""

import hashlib
import math
import threading
import time
from typing import Any

import requests

_THREAD = threading.local()
_TOKEN_LOCK = threading.Lock()
_TOKENS: dict[str, tuple[str, float]] = {}
_MAX_TOKEN_ENTRIES = 32


def post(url: str, **kwargs: Any) -> requests.Response:
    """Reuse a per-thread session with request-scoped auth and no cookie replay.

    Redirects are disabled so credentials stay on the configured endpoint.
    """
    session: requests.Session | None = getattr(_THREAD, "session", None)
    if session is None:
        session = requests.Session()
        _THREAD.session = session
    session.cookies.clear()
    return session.post(url, allow_redirects=False, **kwargs)


def iam_token(
    api_key: str,
    iam_url: str,
    *,
    instance_url: str = "",
    rejected_token: str | None = None,
) -> str:
    """Reuse a token until its refresh window; coalesce expiry and 401 refreshes.

    A late 401 must not evict a newer token another request already acquired.
    Fingerprints keep credential values out of cache keys and diagnostic reprs.
    Failed refreshes discard the stale token. The refresh margin is capped at
    ten percent of the lifetime for short-lived tokens, or sixty seconds.
    """
    key = hashlib.sha256(
        "\0".join((instance_url.rstrip("/"), iam_url, api_key)).encode()
    ).hexdigest()
    with _TOKEN_LOCK:
        cached = _TOKENS.get(key)
        if cached and time.monotonic() < cached[1] and cached[0] != rejected_token:
            return cached[0]
        _TOKENS.pop(key, None)
        started = time.monotonic()
        response = post(
            iam_url,
            headers={"Accept": "application/json"},
            data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": api_key},
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise ValueError("IAM response omitted access_token")
        lifetime = body.get("expires_in")
        if lifetime is None and isinstance(body.get("expiration"), int | float):
            lifetime = body["expiration"] - time.time()
        if (
            isinstance(lifetime, bool)
            or not isinstance(lifetime, int | float)
            or not math.isfinite(lifetime)
            or lifetime <= 0
        ):
            raise ValueError("IAM response omitted a valid token lifetime")
        refresh_at = started + lifetime - min(60.0, lifetime * 0.1)
        if len(_TOKENS) >= _MAX_TOKEN_ENTRIES:
            _TOKENS.pop(next(iter(_TOKENS)))
        _TOKENS[key] = (token, refresh_at)
        return token
