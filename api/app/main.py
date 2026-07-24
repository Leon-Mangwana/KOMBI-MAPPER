from fastapi import FastAPI

from .routers import traces

app = FastAPI(title="Kombi Route Mapper API", version="0.1.0")
app.include_router(traces.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
