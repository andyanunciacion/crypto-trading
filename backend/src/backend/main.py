"""FastAPI app. Run with `uvicorn backend.main:app`. The routes are in app.py."""

from backend.app import create_app
from backend.settings import load_api_settings
from shared.logs import configure_logging

# Loaded at import, which uvicorn does at startup, so a bad config stops the API
# with a readable error before it serves anything.
settings = load_api_settings()
configure_logging(settings.log_level)
app = create_app(settings)
