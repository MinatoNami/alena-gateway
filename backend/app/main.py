"""alena-gateway status service.

Serves the status page at / and the JSON behind it at /api/status. nginx puts
this at the root of the tailnet origin; every other route on that origin belongs
to one of the applications in the registry.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .probes import Prober, to_json

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STARTED_AT = time.time()
VERSION = os.environ.get("GATEWAY_VERSION", "dev")

registry = config.load()
prober = Prober(registry)

app = FastAPI(
    title="alena-gateway",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/api/status")
async def status(response: Response) -> JSONResponse:
    results, checked_at = await prober.status()

    # The page is a live view; a cached copy of it is worse than useless because
    # it looks current. The probe cache inside the process is what stops the
    # polling from reaching the upstreams.
    return JSONResponse(
        {
            "generated_at": datetime.fromtimestamp(checked_at, timezone.utc).isoformat(),
            "origin": registry.origin,
            "gateway": {
                "version": VERSION,
                "uptime_seconds": int(time.time() - STARTED_AT),
            },
            "services": to_json(results),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/healthz")
async def healthz() -> dict:
    """Liveness for this container only.

    Deliberately does not probe the upstreams: a healthcheck that fails when a
    *different* application is down would have docker restart the gateway, which
    takes the status page offline at the one moment it is worth reading.
    """
    return {"status": "ok", "version": VERSION}


@app.exception_handler(404)
async def unknown_path(request: Request, exc) -> Response:
    """Render the status page for a mistyped URL, still saying 404.

    The gateway is the origin root, so a wrong path here is usually someone
    reaching for one of the applications. The page lists all of them, which is a
    more useful answer than nginx's default body — but the status code stays 404
    rather than becoming a soft 200 that tells a crawler this page exists.

    API paths keep the JSON error: a client calling /api/ wants a parseable
    response, not HTML.
    """
    if request.url.path.startswith("/api/") or "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return FileResponse(STATIC_DIR / "index.html", status_code=404)


# Mounted last so the API routes above win. html=True serves index.html for the
# bare root; there is no client-side router here, so nothing else needs it.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
