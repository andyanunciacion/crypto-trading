"""FastAPI app. Run with `uvicorn backend.main:app`."""

from fastapi import FastAPI

app = FastAPI(title="crypto-trading API")


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness check used by Docker. DB and Redis checks are added in P7-4."""
    return {"status": "ok"}
