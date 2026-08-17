"""Admin authentication and the limits that guard open ingest.

Two separate concerns, both of which only start to matter once the API is
reachable from the internet:

**Admin authentication.** Most of this API is meant to be public — the routes
and stops are the product. `GET /traces/{id}` is not: it returns a named
contributor's complete timestamped movement history, and trace ids are
sequential, so an unauthenticated version can be walked from 1 upwards to
reconstruct where individual people travelled and when. That is a safety
problem for the riders contributing, so it is operator-only.

**Ingest limits.** `POST /traces` stays open, because requiring an account
would cost more traces than it saved. Open is not unbounded: without a size cap
and a rate limit, one person with curl can fill the database, and clustering
quietly degrades as the junk accumulates.
"""

import time
from collections import deque
from collections.abc import Awaitable, Callable
from secrets import compare_digest

from fastapi import Header, HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from .config import settings


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """Reject anything without the operator token. A FastAPI dependency.

    Returns 503 rather than 401 when no token is configured: the endpoint is
    genuinely unavailable in that deployment, and answering 401 would invite a
    caller to keep guessing at a credential that does not exist.

    The comparison is constant-time. The timing signal on a short shared secret
    is not much of an attack, but the fix costs one function call.
    """
    # Both sides are stripped. A token pasted into a hosting dashboard's text
    # box arrives with a trailing newline surprisingly often, and the resulting
    # failure is a plain 401 that looks exactly like a wrong token — there is
    # nothing in the UI to see, because the whitespace is invisible. No security
    # is given up: the entropy is in the token body, and nobody chooses a secret
    # whose meaning depends on a leading space.
    expected = settings.admin_token.strip()
    supplied = x_admin_token.strip() if x_admin_token is not None else None

    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "This endpoint is disabled because ADMIN_TOKEN is not configured. "
                "Set it in the API's environment to enable operator access."
            ),
        )

    if supplied is None or not compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid X-Admin-Token header is required.",
            headers={"WWW-Authenticate": "X-Admin-Token"},
        )


def client_key(request: Request) -> str:
    """Who to count a request against.

    Behind a platform proxy the socket address is the proxy's, so every client
    would share one bucket. `X-Forwarded-For` carries the chain as
    `client, proxy1, proxy2`, where each proxy *appends* the address it received
    the request from. A client can therefore put anything at the front of that
    list, but it cannot control what the last trusted hop appended — so the
    rightmost entry is the one to trust, not the leftmost.

    That holds for exactly one trusted proxy in front of this app, which is the
    Render and Fly deployment. Behind two, the second-from-right is the client
    and this undercounts by grouping everyone behind the inner proxy together;
    behind none, `trust_proxy_headers` should be off, because the header is
    then written by whoever is calling.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
            if hops:
                return hops[-1]

    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Fixed set of sliding windows per client, held in memory.

    In memory on purpose: the alternative is Redis, which is another service to
    run and pay for, and this is protection against casual abuse rather than a
    distributed adversary. Two consequences worth knowing — the counters reset
    when the process restarts, and each instance counts separately, so N
    instances allow N times the limit. Both are acceptable while this runs as a
    single small service; neither is acceptable as a billing control.
    """

    def __init__(self, limits: tuple[tuple[int, float], ...], max_clients: int = 10_000) -> None:
        """
        Args:
            limits: `(requests, window_seconds)` pairs, all enforced together.
            max_clients: Cap on tracked clients, so a flood of distinct
                addresses cannot grow the table without bound.
        """
        self._limits = limits
        self._max_clients = max_clients
        self._seen: dict[str, deque[float]] = {}

    @property
    def window(self) -> float:
        """The longest window, i.e. how far back history is worth keeping."""
        return max((window for _, window in self._limits), default=0.0)

    def check(self, key: str, now: float | None = None) -> int | None:
        """Record a request. Returns seconds to wait if it should be refused.

        A refused request is *not* recorded. Counting rejections would let a
        client that keeps hammering extend its own lockout indefinitely, which
        turns a rate limit into a ban.
        """
        now = time.monotonic() if now is None else now
        history = self._seen.get(key)

        if history is None:
            if len(self._seen) >= self._max_clients:
                self._evict(now)
            history = self._seen.setdefault(key, deque())

        cutoff = now - self.window
        while history and history[0] <= cutoff:
            history.popleft()

        for allowed, window in self._limits:
            start = now - window
            used = sum(1 for stamp in history if stamp > start)
            if used >= allowed:
                # The oldest request inside this window is the one whose
                # expiry frees a slot.
                oldest = next(stamp for stamp in history if stamp > start)
                return max(1, int(oldest + window - now) + 1)

        history.append(now)
        return None

    def reset(self) -> None:
        """Forget every client. For tests, which must not inherit each other's counts."""
        self._seen.clear()

    def _evict(self, now: float) -> None:
        """Drop clients with nothing left inside the longest window."""
        cutoff = now - self.window
        stale = [key for key, history in self._seen.items() if not history or history[-1] <= cutoff]
        for key in stale:
            del self._seen[key]

        if len(self._seen) >= self._max_clients:
            # Everyone tracked is still active. Drop the least recently seen,
            # which is the closest thing to a fair choice available.
            oldest = min(self._seen, key=lambda key: self._seen[key][-1])
            del self._seen[oldest]


#: The limiter the running app uses. A module-level singleton rather than
#: something built inside the middleware, so that it can be reached without
#: digging through Starlette's middleware stack — the test suite clears it
#: between tests, and an operator can inspect it from a shell.
ingest_limiter = RateLimiter(
    (
        (settings.uploads_per_minute, 60.0),
        (settings.uploads_per_hour, 3600.0),
    )
)


class IngestGuardMiddleware(BaseHTTPMiddleware):
    """Size and rate limits on trace uploads, applied before the body is read.

    Both checks have to happen here rather than in the route: by the time
    FastAPI has a `TraceIn` to validate, it has already read and parsed the
    whole body, which is exactly what an oversized upload is trying to make it
    do.
    """

    #: Only uploads are guarded. Reads are cheap, cacheable and public.
    PATH = "/traces"

    def __init__(self, app, limiter: RateLimiter | None = None) -> None:
        super().__init__(app)
        self.limiter = limiter or ingest_limiter

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method != "POST" or request.url.path.rstrip("/") != self.PATH:
            return await call_next(request)

        oversized = self._length_problem(request)
        if oversized is not None:
            return oversized

        retry_after = self.limiter.check(client_key(request))
        if retry_after is not None:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Too many uploads. Try again shortly."},
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)

    def _length_problem(self, request: Request) -> Response | None:
        """413 if the body is too large, 411 if it declines to say how large."""
        raw = request.headers.get("content-length")
        if raw is None:
            # A chunked upload never declares its length, so a size cap cannot
            # be applied before reading it. Browsers always set the header for
            # a JSON body, so requiring it costs the capture PWA nothing and
            # closes the obvious way around the limit.
            return JSONResponse(
                status_code=status.HTTP_411_LENGTH_REQUIRED,
                content={"detail": "A Content-Length header is required to upload a trace."},
            )

        try:
            length = int(raw)
        except ValueError:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": "Malformed Content-Length header."},
            )

        if length > settings.max_upload_bytes:
            return JSONResponse(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                content={
                    "detail": (
                        f"Trace upload is too large "
                        f"({length} bytes; the limit is {settings.max_upload_bytes}). "
                        "Split a very long recording into separate rides."
                    )
                },
            )

        return None
