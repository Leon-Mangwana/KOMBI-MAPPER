from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import gtfs, network, traces
from .security import IngestGuardMiddleware

app = FastAPI(title="Kombi Route Mapper API", version="0.1.0")

# Both front ends are served from their own origins, so every call they make is
# cross-origin and has to survive a preflight.
#
# PATCH and X-Admin-Token are here for the web map's operator mode, which renames
# routes and stops. Both are needed: PATCH is not a simple method, and a request
# carrying a header outside the CORS-safelisted set is not a simple request, so
# the browser preflights and refuses unless the response names each of them. A
# 400 on that preflight is what an operator sees as a rename that silently does
# nothing.
#
# Credentials stay off regardless. The operator token travels in its own header,
# not a cookie, so `allow_credentials` buys nothing and would force the stricter
# rules that come with it (no wildcard origin, `Vary: Origin` handling).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)

# Registered after CORS so that a rejected upload still carries the headers the
# browser needs to read the response — without them the capture PWA sees an
# opaque network failure and re-queues, instead of the 429 telling it to wait.
# Starlette applies middleware in reverse registration order, so the later
# registration is the inner one.
app.add_middleware(IngestGuardMiddleware)

app.include_router(traces.router)
app.include_router(network.router)
app.include_router(gtfs.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
