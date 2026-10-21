"""FastAPI app. Run with `uvicorn backend.main:app`."""

from fastapi import FastAPI

from shared.config import load_settings

# Loaded at import, which uvicorn does at startup, so a bad config stops the API
# with a readable error before it serves anything.
settings = load_settings()

app = FastAPI(title="crypto-trading API")
app.state.settings = settings


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness check used by Docker. DB and Redis checks are added in P7-4."""
    return {"status": "ok"}
