from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import StateBroadcaster, attach_routes
from app.core.config import get_settings
from app.services.strategy_engine import StrategyEngine

settings = get_settings()
engine = StrategyEngine(settings)
broadcaster = StateBroadcaster()


@asynccontextmanager
async def lifespan(_: FastAPI):
    broadcaster.bind_loop(asyncio.get_running_loop())
    engine.set_notifier(broadcaster.push)
    engine.startup()
    yield
    engine.shutdown()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

attach_routes(app, engine, broadcaster)
