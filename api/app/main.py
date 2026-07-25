from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import traces

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

app.include_router(traces.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
