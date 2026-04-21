from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from fastapi import APIRouter, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.services.dhan_gateway import DhanGatewayError
from app.services.strategy_engine import StrategyEngine


class StateBroadcaster:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def push(self, snapshot) -> None:
        if not self.loop:
            return
        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast(snapshot.model_dump(mode="json"))))

    async def connect(self, socket: WebSocket) -> None:
        await socket.accept()
        self.connections.add(socket)

    async def disconnect(self, socket: WebSocket) -> None:
        with suppress(KeyError):
            self.connections.remove(socket)

    async def broadcast(self, payload: dict) -> None:
        stale: list[WebSocket] = []
        for socket in self.connections:
            try:
                await socket.send_text(json.dumps(payload))
            except Exception:
                stale.append(socket)
        for socket in stale:
            await self.disconnect(socket)


def build_router(engine: StrategyEngine, broadcaster: StateBroadcaster) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/state")
    def state():
        return engine.get_snapshot()

    @router.get("/option-chain/oi-change")
    def option_chain_oi_change(strike: int = Query(..., ge=1)):
        try:
            return engine.get_option_chain_oi_change(strike)
        except DhanGatewayError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.websocket("/ws/state")
    async def state_socket(socket: WebSocket) -> None:
        await broadcaster.connect(socket)
        await socket.send_text(engine.get_snapshot().model_dump_json())
        try:
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            await broadcaster.disconnect(socket)

    return router


def attach_routes(app: FastAPI, engine: StrategyEngine, broadcaster: StateBroadcaster) -> None:
    app.include_router(build_router(engine, broadcaster))
