"""A fixed-window Redis rate limiter as a SINGLE atomic Lua op.

Why atomic: INCR-then-EXPIRE as two separate commands has a real failure
mode — if the process dies (or the connection drops) between the two
calls, the key is left without a TTL and never expires, locking that
scope out forever. Running both inside one Lua script closes that gap
completely: Redis executes a script to completion, uninterrupted by any
other client, as a single operation from the caller's point of view. One
round trip, not two, and no window where a crash can leave bad state.
"""

from redis.asyncio import Redis

_INCR_WITH_TTL = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
local ttl_ms = redis.call('PTTL', KEYS[1])
return {current, ttl_ms}
"""


async def hit(redis: Redis, key: str, *, window_seconds: int) -> tuple[int, int]:
    """Atomically increments `key`, setting a `window_seconds` TTL only the
    moment the counter is first created. Returns (count_after_increment,
    remaining_ttl_ms). Raises redis.exceptions.RedisError on a Redis
    failure — the caller decides fail-open vs fail-closed.

    register_script() is deliberately called fresh every time rather than
    cached on a module global: it's a local, network-free operation (just
    computes a SHA1 and stores the script text on whichever client
    instance you hand it), so caching it would buy nothing at runtime —
    and it would actively break tests that swap in a different Redis
    double for a single request, since a cached Script stays bound to
    whichever client first created it. redis-py's Script.__call__ itself
    handles EVALSHA with a transparent fallback to EVAL on a NOSCRIPT miss.
    """
    script = redis.register_script(_INCR_WITH_TTL)
    count, ttl_ms = await script(keys=[key], args=[window_seconds * 1000], client=redis)
    return int(count), int(ttl_ms)
