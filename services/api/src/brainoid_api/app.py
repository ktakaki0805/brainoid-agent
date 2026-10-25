"""FastAPI factory with startup validation and owned dependency lifetime."""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from brainoid_api.errors import RequestContextMiddleware, install_error_handlers, logger
from brainoid_api.health import ReadinessProbe, RuntimeState, router
from brainoid_api.settings import Settings, load_settings

DependencyLifespan = Callable[[Settings], AbstractAsyncContextManager[Sequence[ReadinessProbe]]]


@asynccontextmanager
async def no_dependencies(settings: Settings) -> AsyncIterator[Sequence[ReadinessProbe]]:
    # This foundation serves health only. Real adapters are added by their own tickets.
    yield ()


def create_app(
    *,
    settings_loader: Callable[[], Settings] = load_settings,
    dependency_lifespan: DependencyLifespan = no_dependencies,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = RuntimeState()
        app.state.runtime = runtime
        phase = "startup"
        try:
            runtime.settings = settings_loader()
            logger.setLevel(runtime.settings.log_level)
            async with dependency_lifespan(runtime.settings) as probes:
                runtime.probes = tuple(probes)
                runtime.started = True
                logger.info('{"event":"api_started"}')
                try:
                    yield
                finally:
                    phase = "shutdown"
                    runtime.started = False
        except Exception:
            # ASGI servers print lifespan failures; never propagate adapter/config input values.
            logger.error('{"event":"api_%s_failed"}', phase)
            raise RuntimeError(
                "API " + phase + " failed; check configuration and dependencies"
            ) from None
        finally:
            runtime.started = False
            runtime.settings = None
            runtime.probes = ()
            logger.info('{"event":"api_stopped"}')

    app = FastAPI(
        title="Brainoid Agent API",
        version="0.2.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    app.state.runtime = RuntimeState()
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(router)
    return app
