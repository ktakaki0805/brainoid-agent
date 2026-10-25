"""HTTP adapter with bounded bodies and content-free failures."""

import json

from brainoid_jobs import AuthorizationUnavailable
from brainoid_persistence.firestore import PersistenceUnavailable
from fastapi import FastAPI, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from brainoid_worker.entry import CallerDenied, Worker

MAX_DELIVERY_BYTES = 4096


def create_app(worker: Worker) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/tasks", status_code=204)
    async def execute(request: Request) -> Response:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_DELIVERY_BYTES:
                return Response(status_code=413)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            return Response(status_code=400)
        try:
            await run_in_threadpool(
                worker.handle, request.headers.get("authorization", ""), payload
            )
        except CallerDenied:
            return Response(status_code=401)
        except ValidationError:
            return Response(status_code=400)
        except (PersistenceUnavailable, AuthorizationUnavailable):
            return Response(status_code=503)
        return Response(status_code=204)

    return app
