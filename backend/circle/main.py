"""Circle FastAPI application: startup sequence, routers, static frontend."""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from circle.api.context import get_context, set_context, AppContext
from circle.api.auth import access_key_middleware
from circle.api.routes_core import router as core_router
from circle.api.routes_imports import router as imports_router
from circle.config import get_settings


def _configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Privacy: this app must never log raw imported content (spec §32).
    logging.getLogger("circle").setLevel(logging.INFO)


def create_app() -> FastAPI:
    _configure_logging()
    log = logging.getLogger("circle.main")
    settings = get_settings()

    # Optional observability (disabled unless configured)
    try:
        from circle.observability.sentry import init_sentry
        init_sentry()
    except Exception as e:  # pragma: no cover
        log.warning("sentry init skipped: %s", e)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = get_context()
        try:
            health = ctx.startup()
            log.info("startup health: db=%s llm=%s watcher=%s",
                     health.get("database", {}).get("connected"),
                     health.get("llm", {}).get("available"),
                     health.get("watcher", {}).get("running"))
        except Exception as e:
            log.error("startup failed: %s", e, exc_info=True)
        yield
        try:
            get_context().shutdown()
        except Exception:
            pass

    app = FastAPI(
        title="Circle",
        description="Private, local-first relationship intelligence.",
        version="0.1.0",
        docs_url="/api/docs" if settings.enable_docs else None,
        openapi_url="/api/openapi.json" if settings.enable_docs else None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        # A hosted UI (static host, tunnel) is an extra origin, configured via
        # CORS_ORIGINS. Never "*": the API serves private message content.
        allow_origins=settings.cors_origin_list(),
        allow_credentials=False,
        allow_methods=["*"],
        # The access key travels in a custom header, so it must be allowed.
        allow_headers=["*"],
    )

    # Order matters: CORS runs outermost so a rejected request still carries
    # the headers a browser needs to read the 401 body.
    app.middleware("http")(access_key_middleware)

    app.include_router(core_router)
    app.include_router(imports_router)

    # Serve the built frontend when present (production mode)
    dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    if dist.exists():
        # Client-side routes (/, /imports, /settings, /person/<id>) are not real
        # files on disk. Without this fallback a refresh or a pasted link hits
        # a 404 instead of the app.
        index = dist / "index.html"

        @app.get("/{spa_path:path}", include_in_schema=False)
        def spa_fallback(spa_path: str):
            if spa_path.startswith("api/"):
                raise HTTPException(404, "Not Found")
            candidate = (dist / spa_path).resolve()
            try:
                candidate.relative_to(dist.resolve())
            except ValueError:
                return FileResponse(index)
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index)

        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    s = get_settings()
    uvicorn.run("circle.main:app", host=s.api_host, port=s.api_port,
                reload=False, log_level=s.log_level.lower())
