"""Vercel entrypoint.

Vercel's Python runtime looks for a top-level `app` in one of a few known
filenames (app.py, index.py, main.py, ...). It re-exports the real FastAPI
application so the deployed app is identical to the one run locally with
`uvicorn backend.main:app`.
"""

from backend.main import app

__all__ = ["app"]
