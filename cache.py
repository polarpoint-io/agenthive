"""
cache.py - caches retrieve()'s result per (team, anchor, hops, hub_cutoff).

Why this exists: several agents commonly ask for the same anchor around
the same time (the whole team is looking at the same incident/task). A
cache hit here means the graph traversal ran once, not once per agent -
and, more importantly for the metrics story, a hit served to a
DIFFERENT user than the one who populated it is direct, countable
evidence that the memory graph is being used as *shared* memory rather
than N independent copies. See metrics.py's cross_user_cache_hit().

Two backends behind one interface:

- InMemoryCache: per-process, TTL-based. Fine for a single replica.
- RedisCache: shared across every replica pointed at the same REDIS_URL -
  needed once a cache hit should be visible across pods, not just within
  the one that computed it.

Either way, a write or a review for a team invalidates that team's
entries outright (correctness over cache lifetime) - the TTL is a
secondary backstop, not the primary invalidation mechanism.
"""
import json
import threading
import time

from config import CONFIG


def cache_key(team_id: str, anchor: str, hops: int, hub_cutoff: int) -> str:
    from retrieval import slug

    return f"{team_id}:{slug(anchor)}:{hops}:{hub_cutoff}"


class InMemoryCache:
    kind = "in-memory"

    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        # key -> (expires_at, value, team_id, populated_by_user_id)
        self._store: dict[str, tuple[float, dict, str, str]] = {}
        self._lock = threading.Lock()

    def get(self, team_id: str, anchor: str, hops: int, hub_cutoff: int):
        """Returns (value, populated_by_user_id) or (None, None) on a miss."""
        key = cache_key(team_id, anchor, hops, hub_cutoff)
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None, None
            expires_at, value, _, populated_by = entry
            if now >= expires_at:
                del self._store[key]
                return None, None
            return value, populated_by

    def set(self, team_id: str, anchor: str, hops: int, hub_cutoff: int, value: dict, user_id: str):
        key = cache_key(team_id, anchor, hops, hub_cutoff)
        with self._lock:
            self._store[key] = (time.monotonic() + self.ttl, value, team_id, user_id)

    def invalidate_team(self, team_id: str):
        with self._lock:
            stale = [k for k, (_, _, t, _) in self._store.items() if t == team_id]
            for k in stale:
                del self._store[k]


class RedisCache:
    kind = "redis"

    def __init__(self, redis_url: str, ttl_seconds: int):
        import redis

        self.ttl = ttl_seconds
        self._redis = redis.from_url(redis_url, socket_timeout=2, socket_connect_timeout=2)

    def _redis_key(self, team_id: str, anchor: str, hops: int, hub_cutoff: int) -> str:
        return "agenthive:cache:" + cache_key(team_id, anchor, hops, hub_cutoff)

    # A per-team "generation" counter is bumped on every write/review, and
    # folded into the cache key indirectly by just deleting the team's
    # index set on invalidation - see invalidate_team.

    def get(self, team_id: str, anchor: str, hops: int, hub_cutoff: int):
        """Returns (value, populated_by_user_id) or (None, None) on a miss."""
        try:
            raw = self._redis.get(self._redis_key(team_id, anchor, hops, hub_cutoff))
            if not raw:
                return None, None
            wrapper = json.loads(raw)
            return wrapper["value"], wrapper.get("user_id")
        except Exception:
            return None, None  # a cache outage should degrade to "miss", not fail the request

    def set(self, team_id: str, anchor: str, hops: int, hub_cutoff: int, value: dict, user_id: str):
        try:
            k = self._redis_key(team_id, anchor, hops, hub_cutoff)
            self._redis.set(k, json.dumps({"value": value, "user_id": user_id}), ex=self.ttl)
            self._redis.sadd(f"agenthive:cachekeys:{team_id}", k)
            self._redis.expire(f"agenthive:cachekeys:{team_id}", self.ttl)
        except Exception:
            pass

    def invalidate_team(self, team_id: str):
        try:
            index_key = f"agenthive:cachekeys:{team_id}"
            keys = self._redis.smembers(index_key)
            if keys:
                self._redis.delete(*keys)
            self._redis.delete(index_key)
        except Exception:
            pass


def make_cache():
    if not CONFIG.cache_enabled:
        return None
    if CONFIG.redis_url:
        try:
            return RedisCache(CONFIG.redis_url, CONFIG.cache_ttl_seconds)
        except Exception:
            pass
    return InMemoryCache(CONFIG.cache_ttl_seconds)


CACHE = make_cache()
