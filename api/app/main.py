from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import gtfs, network, traces
from .security import IngestGuardMiddleware

app = FastAPI(title="Kombi Route Mapper API", version="0.1.0")

# The capture PWA is served from its own origin, so trace uploads are
# cross-origin. No cookies or auth headers are involved — the browser only
# needs the preflight to pass — so credentials stay off.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
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
