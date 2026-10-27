"""Pinned Uvicorn transport with content-free logs, including rejected upgrades."""

import asyncio
import logging
from typing import Any

from uvicorn.config import Config
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
from uvicorn.server import ServerState

from brainoid_api.errors import logger


class _TransportEvents(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # Never format record.msg/args, exception, URL, header or frame data.
        logger.log(record.levelno, '{"event":"device_transport"}')


class DeviceWebSocketProtocol(WebSocketsSansIOProtocol):
    def __init__(
        self,
        config: Config,
        server_state: ServerState,
        app_state: dict[str, Any],
        _loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        super().__init__(config, server_state, app_state, _loop)
        # Per-connection logger; never change process-wide Uvicorn configuration.
        safe = logging.Logger("brainoid_device_transport", level=logging.INFO)
        safe.addHandler(_TransportEvents())
        safe.propagate = False
        self.logger = safe
        self.conn.logger = safe
        self.conn.debug = False
