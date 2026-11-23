"""Serves the built web app from the API (ADR-0016: one deployable unit, same origin; P10-06).

With `WEB_DIST_DIR` set (the production image sets it to the Vite build), `/assets/*` are the
hashed bundles (cached for a year) and every other `GET` that is not an API path answers with a
file of the build's top level or `index.html`, so the React router handles `/chat`,
`/console/...` and `/help/...`. API paths never fall back to the page: an unknown `/api/...`
stays a JSON 404. Paths are resolved inside the build folder only.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from hub.api.errors import ProblemError

# Paths the app answers itself; the page never replaces them. Matched as whole path segments, so
# `/healthcheck` or `/ready-x` are pages, while `/api/x` and `/docs/x` stay JSON 404s.
RESERVED = ("api", "health", "ready", "docs", "redoc", "openapi.json")


def is_reserved(path: str) -> bool:
    return any(path == name or path.startswith(f"{name}/") for name in RESERVED)


def mount_web(app: FastAPI, dist: Path) -> None:
    index = dist / "index.html"
    if not index.is_file():
        raise RuntimeError(f"WEB_DIST_DIR has no index.html: {dist}")
    root = dist.resolve()
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def web_page(path: str) -> FileResponse:
        if is_reserved(path):
            raise ProblemError(404, "not_found", "Not found.")
        if path and "/" not in path:
            candidate = (root / path).resolve()
            if candidate.parent == root and candidate.is_file():
                return FileResponse(candidate)
        # The page itself must not be cached: it names the current hashed bundles.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
