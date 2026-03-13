from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, leaderboard, market, portfolio, tournament, trade, websocket


def create_app() -> FastAPI:
    app = FastAPI(title="LineUp API", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(market.router, prefix="/api/market", tags=["market"])
    app.include_router(trade.router, prefix="/api/trade", tags=["trade"])
    app.include_router(portfolio.router, prefix="/api/portfolio", tags=["portfolio"])
    app.include_router(leaderboard.router, prefix="/api/leaderboard", tags=["leaderboard"])
    app.include_router(tournament.router, prefix="/api/tournaments", tags=["tournaments"])
    app.include_router(websocket.router, tags=["websocket"])

    @app.get("/health", tags=["health"])
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
