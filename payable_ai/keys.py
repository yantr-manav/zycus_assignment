"""API-key pool: parsing, rotation, pacing and redaction.

Provider keys may be supplied in several shapes because free-tier accounts issue
different credential formats (e.g. classic ``AIza...`` keys and newer ``AQ....``
keys). All of them are opaque strings to this module — it never validates or
reformats a key, it only splits pools, tracks per-key health, rotates on
failures and guarantees that key material never reaches a log line.

Supported env shapes (identical for GEMINI_* and GROQ_*):

    GEMINI_API_KEY=key1,key2;key3        # one value: comma / semicolon / whitespace separated
    GEMINI_API_KEYS=key1 key2            # explicit list value
    GEMINI_API_KEY_2=key2                # numbered extras (up to _9)

The first parse wins position order: GEMINI_API_KEY entries first, then
GEMINI_API_KEYS, then numbered extras. Duplicates are dropped.
"""
from __future__ import annotations

import os
import re
import threading
import time

_SPLIT = re.compile(r"[,\s;]+")

# Secret material can leak into exception strings (SDKs occasionally include
# request URLs). Every message that can reach a log goes through redact().
_SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{15,}"),
    re.compile(r"AQ\.[0-9A-Za-z_\-]{15,}"),
    re.compile(r"gsk_[0-9A-Za-z]{15,}"),
    re.compile(r"sk-[0-9A-Za-z]{15,}"),
)


def redact(text: str) -> str:
    """Replace any credential-like token with a short, non-reversible label."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(0)[:4]}…[REDACTED]", text)
    return text


def key_label(key: str) -> str:
    """A stable, non-secret fingerprint for diagnostics.

    Prefix + suffix + length is enough to tell keys apart in logs while
    exposing too little material to reconstruct or verify a credential.
    """
    return f"{key[:4]}…{key[-2:]}({len(key)})"


def parse_key_pool(*raw_values: str | None) -> list[str]:
    """Split any mixture of pool strings into unique, order-preserving keys."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in raw_values:
        if not raw:
            continue
        for part in _SPLIT.split(str(raw).strip()):
            part = part.strip().strip("'\"")
            if part and part not in seen:
                seen.add(part)
                out.append(part)
    return out


def keys_from_env(prefix: str) -> list[str]:
    """Collect a provider's keys from the documented env shapes."""
    values = [os.getenv(f"{prefix}_API_KEY", "")]
    values.append(os.getenv(f"{prefix}_API_KEYS", ""))
    for i in range(2, 10):
        values.append(os.getenv(f"{prefix}_API_KEY_{i}", ""))
    return parse_key_pool(*values)


class KeyPool:
    """Health-tracked rotation over one provider's keys.

    Failure classes:
      invalid   — 400/401/403 or "API key not valid": key is dropped for the session.
      quota     — 429: key sleeps until ``retry_after`` (default 65 s), others are tried.
      transient — 500/502/503/504/timeout: short cooldown, key retried later.
    """

    INVALID = "invalid"
    QUOTA = "quota"
    TRANSIENT = "transient"
    REQUEST = "request"   # the request itself is rejected (too large / malformed): no key is at fault

    def __init__(self, name: str, keys: list[str]):
        self.name = name
        self._keys = list(keys)
        self._dead: set[str] = set()
        self._cooldown_until: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._lock = threading.Lock()

    def __bool__(self) -> bool:
        return bool(self._keys)

    @property
    def size(self) -> int:
        return len(self._keys)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._keys)

    def labels(self) -> list[str]:
        with self._lock:
            return [key_label(k) for k in self._keys]

    def status(self) -> list[dict[str, str]]:
        now = time.monotonic()
        with self._lock:
            rows = []
            for key in self._keys:
                if key in self._dead:
                    state = "invalid"
                elif self._cooldown_until.get(key, 0.0) > now:
                    state = "cooling-down"
                else:
                    state = "ready"
                rows.append({"key": key_label(key), "state": state})
            return rows

    def next_key(self, wait: bool = True, max_wait: float = 90.0) -> str | None:
        """Return the next healthy key, waiting out short cooldowns when asked.

        Returns None when every key is permanently invalid, the pool is empty,
        or (with ``wait``) the soonest cooldown exceeds ``max_wait`` — a daily
        quota is not something a batch run should sleep through.
        """
        deadline = time.monotonic() + max_wait
        while True:
            now = time.monotonic()
            with self._lock:
                alive = [k for k in self._keys if k not in self._dead]
                if not alive:
                    return None
                ready = [k for k in alive if self._cooldown_until.get(k, 0.0) <= now]
                if ready:
                    return ready[0]
                soonest = min(self._cooldown_until.get(k, 0.0) for k in alive)
                delay = soonest - now
            if not wait or delay > max(0.0, deadline - now):
                return None
            time.sleep(max(0.25, min(delay, 15.0)))

    def note_success(self, key: str) -> None:
        with self._lock:
            self._cooldown_until.pop(key, None)
            self._failures.pop(key, None)

    def note_failure(self, key: str, kind: str, retry_after: float | None = None) -> None:
        with self._lock:
            self._failures[key] = self._failures.get(key, 0) + 1
            if kind == self.INVALID:
                self._dead.add(key)
                return
            if kind == self.QUOTA:
                delay = retry_after if retry_after and retry_after > 0 else 65.0
            else:  # transient
                delay = retry_after if retry_after and retry_after > 0 else min(30.0, 4.0 * self._failures[key])
            self._cooldown_until[key] = time.monotonic() + delay


class Pacer:
    """Minimum spacing between provider calls so free-tier RPM limits hold."""

    def __init__(self, min_interval_seconds: float):
        self.min_interval = max(0.0, float(min_interval_seconds))
        self._last_call = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self.min_interval - (now - self._last_call)
            if delay > 0:
                time.sleep(delay)
            self._last_call = time.monotonic()
