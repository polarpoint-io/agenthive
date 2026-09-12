"""
auth.py - per-user tokens and an in-memory rate limiter.

v0 of this service had one shared API key per team (flagged in ADR.md as
"a placeholder, not a design"). This module replaces that with:

- a per-user token (role: admin | member), hashed at rest with SHA-256.
  Tokens are high-entropy random values (32 bytes from `secrets`), so a
  fast hash is appropriate here - this is not a low-entropy password.
- a simple sliding-window rate limiter, per API key (falls back to the
  client's remote address for the one route that has no key yet:
  POST /teams). In-memory and per-process: correct for a single node,
  and documented as a known limitation for a multi-node deployment in
  DEPLOYMENT.md (needs a shared store such as Redis there).
"""
import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque

from config import CONFIG

TOKEN_PREFIX = "ah-"


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_hex(24)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RateLimiter:
    """Sliding-window limiter: at most `limit` calls per `window` seconds,
    per key. Thread-safe (the server is a ThreadingHTTPServer).

    In-memory and per-process - correct for a single replica. Running
    several replicas behind a load balancer means each one enforces this
    limit independently (a soft guard, not a hard multi-tenant quota).
    Set REDIS_URL to get a limit shared across replicas - see
    RedisRateLimiter below, and DEPLOYMENT.md "Known limitations"."""

    kind = "in-memory"

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        if self.limit <= 0:
            return True, 0
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            cutoff = now - self.window
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.limit:
                retry_after = int(self.window - (now - q[0])) + 1
                return False, max(retry_after, 1)
            q.append(now)
            return True, 0


class RedisRateLimiter:
    """Fixed-window limiter backed by Redis, shared across every replica
    that points at the same REDIS_URL. Simpler and cheaper than a true
    sliding window (one INCR + one conditional EXPIRE per call); the
    tradeoff is up to 2x burst at a window boundary, which is an
    acceptable approximation for a request-rate guard. See
    tests/test_rate_limit.py for both limiters exercised."""

    kind = "redis"

    def __init__(self, redis_url: str, limit: int, window_seconds: int):
        import redis  # only imported when actually selected

        self.limit = limit
        self.window = window_seconds
        self._redis = redis.from_url(redis_url, socket_timeout=2, socket_connect_timeout=2)

    def allow(self, key: str) -> tuple[bool, int]:
        if self.limit <= 0:
            return True, 0
        bucket = int(time.time() // self.window)
        redis_key = f"agenthive:ratelimit:{key}:{bucket}"
        try:
            count = self._redis.incr(redis_key)
            if count == 1:
                self._redis.expire(redis_key, self.window)
            if count > self.limit:
                ttl = self._redis.ttl(redis_key)
                return False, max(int(ttl) if ttl and ttl > 0 else self.window, 1)
            return True, 0
        except Exception:
            # Redis being unreachable should degrade to "allow", not take
            # the whole service down - rate limiting is a guard, not a
            # correctness requirement.
            return True, 0


def make_rate_limiter(redis_url: str, limit: int, window_seconds: int):
    if redis_url:
        try:
            return RedisRateLimiter(redis_url, limit, window_seconds)
        except Exception:
            pass  # fall through to in-memory if redis/redis-py is unavailable
    return RateLimiter(limit, window_seconds)


RATE_LIMITER = make_rate_limiter(
    CONFIG.redis_url, CONFIG.rate_limit_per_minute, CONFIG.rate_limit_window_seconds
)


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def require_role(user: dict, role: str):
    """role='admin' means admin-only; role='member' means any active user
    of the team (admin included) is fine."""
    if role == "admin" and user.get("role") != "admin":
        raise AuthError(403, "this action requires the admin role")
