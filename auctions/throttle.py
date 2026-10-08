"""A ceiling on guesses at the doors that take a secret.

Account passwords and team access codes (some are 4-digit PINs) could be tried
without limit, so a script could walk the whole space in minutes. Each client
gets a number of *failed* attempts per scope and window; past it, the door
answers "troppi tentativi" until the window runs out. The regia PIN has its own
process-wide lockout (see ``remote``).

Counts live in the Django cache: per process, which is what a single Daphne
process needs, and the failure state never touches the database.
"""
from django.conf import settings
from django.core.cache import cache

# scope -> (failed attempts allowed, window in seconds)
LIMITS = {
    "login": (10, 15 * 60),   # username/email + password
    "code": (10, 15 * 60),    # team access codes and PINs
}


def client_ip(request):
    """The client's address. Behind the reverse proxy the socket peer is the
    proxy itself, so the address the trusted proxies appended to
    X-Forwarded-For is used: ``TRUSTED_PROXY_HOPS`` entries from the right
    (the ones before come from the client and can lie). Without a declared
    proxy the header is ignored: anybody could write it."""
    hops = getattr(settings, "TRUSTED_PROXY_HOPS", 0)
    if hops > 0:
        forwarded = [a.strip() for a in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if a.strip()]
        if len(forwarded) >= hops:
            return forwarded[-hops]
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _key(request, scope):
    return f"fm-throttle:{scope}:{client_ip(request)}"


def blocked(request, scope):
    """True once this client has used up its failed attempts for ``scope``."""
    limit, _window = LIMITS[scope]
    return (cache.get(_key(request, scope)) or 0) >= limit


def failure(request, scope):
    """Count one failed attempt; the window starts at the first failure."""
    key = _key(request, scope)
    _limit, window = LIMITS[scope]
    if cache.add(key, 1, window):
        return
    try:
        cache.incr(key)
    except ValueError:          # expired between add() and incr()
        cache.set(key, 1, window)


MESSAGE = "Troppi tentativi falliti da questo dispositivo. Riprova tra qualche minuto."
