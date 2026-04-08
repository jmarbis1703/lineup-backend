import logging
import os
import re

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api import auth, fixtures, leaderboard, market, portfolio, tournament, trade, waitlist, watchlist, webhooks, websocket
from app.core.rate_limit import limiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SEC-14: CORS origin validation
# ---------------------------------------------------------------------------
_ORIGIN_RE = re.compile(
    r"^https?://[a-zA-Z0-9][a-zA-Z0-9\-\.]*\.[a-zA-Z]{2,}(:\d+)?$"
)
_LOCALHOST_RE = re.compile(r"^https?://localhost(:\d+)?$")


def validate_origins(raw: str) -> list[str]:
    """Parse the ALLOWED_ORIGINS env var and discard any malformed entries."""
    validated: list[str] = []
    for origin in (o.strip() for o in raw.split(",")):
        if not origin:
            continue
        if _ORIGIN_RE.match(origin) or _LOCALHOST_RE.match(origin):
            validated.append(origin)
        else:
            logger.warning("CORS: ignoring invalid origin %r", origin)
    if not validated:
        logger.error(
            "CORS: no valid origins in ALLOWED_ORIGINS — falling back to http://localhost:3000"
        )
        return ["http://localhost:3000"]
    return validated


# ALLOWED_ORIGINS must be set to the real frontend domain(s) in production.
# Example: ALLOWED_ORIGINS=https://lineup.yourdomain.com
# Multiple origins: ALLOWED_ORIGINS=https://lineup.yourdomain.com,https://www.lineup.yourdomain.com
ALLOWED_ORIGINS = validate_origins(os.getenv("ALLOWED_ORIGINS", "http://localhost:3000"))

# INF-1: /docs and /redoc are only enabled when DEBUG=true (development).
# They are disabled in production (DEBUG absent or false).
_debug = os.getenv("DEBUG", "false").lower() == "true"


async def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests. Please slow down."},
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="LineUp API",
        version="1.0.0",
        docs_url="/docs" if _debug else None,
        redoc_url="/redoc" if _debug else None,
        swagger_ui_parameters={"persistAuthorization": True} if _debug else None,
        openapi_extra={
            "components": {
                "securitySchemes": {
                    "BearerAuth": {
                        "type": "http",
                        "scheme": "bearer",
                        "bearerFormat": "JWT",
                    }
                }
            },
            "security": [{"BearerAuth": []}],
        },
    )

    # SEC-09: Rate limiting via SlowAPI (Redis-backed, survives restarts)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.include_router(waitlist.router, prefix="/api/waitlist", tags=["waitlist"])
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(market.router, prefix="/api/market", tags=["market"])
    app.include_router(trade.router, prefix="/api/trade", tags=["trade"])
    app.include_router(portfolio.router, prefix="/api/portfolio", tags=["portfolio"])
    app.include_router(watchlist.router, prefix="/api/portfolio/watchlist", tags=["watchlist"])
    app.include_router(leaderboard.router, prefix="/api/leaderboard", tags=["leaderboard"])
    app.include_router(tournament.router, prefix="/api/tournaments", tags=["tournaments"])
    app.include_router(webhooks.router)
    app.include_router(fixtures.router, prefix="/api/fixtures", tags=["fixtures"])
    app.include_router(websocket.router, tags=["websocket"])
    app.include_router(websocket.http_router, prefix="/api/ws", tags=["websocket"])

    @app.get("/health", tags=["health"])
    async def health() -> dict:
        return {"status": "ok"}

    # SEC-16: Global handler for any unhandled exception.
    # Logs the full traceback server-side; returns a sanitised message to the client.
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error(
            "Unhandled exception on %s %s",
            request.method,
            request.url.path,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal error occurred."},
        )

    return app


app = create_app()
