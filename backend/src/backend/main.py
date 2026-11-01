"""FastAPI app. Run with `uvicorn backend.main:app`. The routes are in app.py."""

from backend.app import create_app
from backend.settings import load_api_settings
from shared.errors import init_errors
from shared.logs import LogMirror, configure_logging

# Loaded at import, which uvicorn does at startup, so a bad config stops the API
# with a readable error before it serves anything.
settings = load_api_settings()
mirror = LogMirror("api")  # its lines on the Logs page too (D49)
errors = init_errors(settings, "api")  # error lines and unhandled exceptions (D62)
configure_logging(settings.log_level, mirror=mirror, errors=errors)
errors.announce()
app = create_app(settings, mirror=mirror)
