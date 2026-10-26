"""FastAPI factory with startup validation and owned dependency lifetime."""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from brainoid_api.auth.runtime import authentication_lifespan
from brainoid_api.errors import RequestContextMiddleware, install_error_handlers, logger
from brainoid_api.health import ReadinessProbe, RuntimeState, router
from brainoid_api.routes.agents import router as agents_router
from brainoid_api.routes.conversations import router as conversations_router
from brainoid_api.routes.device_pairings import router as device_pairings_router
from brainoid_api.routes.devices import router as devices_router
from brainoid_api.routes.invitations import router as invitations_router
from brainoid_api.routes.memberships import router as memberships_router
from brainoid_api.routes.private_sessions import router as private_sessions_router
from brainoid_api.routes.run_events import router as run_events_router
from brainoid_api.routes.tenants import router as tenants_router
from brainoid_api.routes.users import router as users_router
from brainoid_api.settings import Settings, load_settings

DependencyLifespan = Callable[[Settings], AbstractAsyncContextManager[Sequence[ReadinessProbe]]]


@asynccontextmanager
async def no_dependencies(settings: Settings) -> AsyncIterator[Sequence[ReadinessProbe]]:
    # Additional business dependency lifetimes are added by their own tickets.
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
            async with (
                authentication_lifespan(runtime.settings) as auth_services,
                dependency_lifespan(runtime.settings) as probes,
            ):
                app.state.auth_services = auth_services
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
            app.state.auth_services = None
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
    app.state.auth_services = None
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(router)
    app.include_router(users_router, prefix="/v1")
    app.include_router(tenants_router, prefix="/v1")
    app.include_router(invitations_router, prefix="/v1")
    app.include_router(memberships_router, prefix="/v1")
    app.include_router(device_pairings_router, prefix="/v1")
    app.include_router(devices_router, prefix="/v1")
    app.include_router(private_sessions_router, prefix="/v1")
    app.include_router(agents_router, prefix="/v1")
    app.include_router(conversations_router, prefix="/v1")
    app.include_router(run_events_router, prefix="/v1")
    return app
