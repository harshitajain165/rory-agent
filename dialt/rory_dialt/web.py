"""The benchmark's health probe and PCM16/24 kHz mono voice_ws endpoint."""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketState

from rory_tools.session import require_credentials

from .agent import run_voice_ws_bot, validate_config


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    require_credentials("STRIPE_API_KEY", "DIALT_API_KEY")
    validate_config()
    yield


app = FastAPI(title="rory-dialt", version="0.1.0", lifespan=_lifespan)
logger = logging.getLogger("uvicorn.error")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/voice")
async def voice(websocket: WebSocket) -> None:
    await websocket.accept()
    started = time.monotonic()
    logger.info("Dialt /voice connection opened")
    try:
        await run_voice_ws_bot(websocket)
    except Exception as exc:
        # Preserve stack locations and exception class, never the exception's
        # payload (which may contain a provider response or a credential).
        logger.exception("Dialt /voice handler failed", exc_info=(
            RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__,
        ))
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1011, reason="Candidate voice handler failed")
        raise RuntimeError("Candidate voice handler failed") from None
    else:
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1000)
    finally:
        logger.info("Dialt /voice connection closed duration_s=%.3f", time.monotonic() - started)
