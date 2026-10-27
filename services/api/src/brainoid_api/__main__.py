"""Run the API with content-free application logs and no URL-bearing access logs."""

import logging

import uvicorn

from brainoid_api.device_protocol import MAX_FRAME_BYTES
from brainoid_api.websocket_transport import DeviceWebSocketProtocol


def main() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("brainoid_api")
    logger.handlers = [handler]
    logger.propagate = False
    uvicorn.run(
        "brainoid_api.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        access_log=False,
        ws=DeviceWebSocketProtocol,
        ws_max_size=MAX_FRAME_BYTES,
        ws_max_queue=16,
        ws_per_message_deflate=False,
        proxy_headers=False,
        log_level="info",
        lifespan="on",
    )


if __name__ == "__main__":
    main()
