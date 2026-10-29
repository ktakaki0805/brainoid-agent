"""App-owned SDK lifetimes and request dependencies; blocking I/O runs in worker threads."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import uuid4

import firebase_admin  # type: ignore[import-untyped]
from brainoid_jobs import Jobs
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.vector_store import VectorSearch
from brainoid_policy.control_ledger import ControlLedger, Objects
from fastapi import Depends, Request
from google.auth.credentials import AnonymousCredentials, with_scopes_if_required
from google.auth.transport.requests import Request as GoogleRequest
from google.cloud import storage
from google.cloud.firestore_v1 import Client
from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool

from brainoid_api.adapters.gcs_authz_ledger import GCSAuthzObjects
from brainoid_api.adapters.google_speech import GoogleSpeech, SpeechSettings
from brainoid_api.adapters.local_authz_ledger import LocalAuthzObjects
from brainoid_api.audio_inputs import AudioInputs
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.device_tokens import DeviceIdentity, DeviceTokens
from brainoid_api.auth.firebase_identity import (
    FirebaseIdentityVerifier,
    IdentityVerifier,
    validate_sdk_environment,
)
from brainoid_api.auth.profiles import FirestoreProfiles, ProfileReader
from brainoid_api.common.firestore import FirestoreTransactions
from brainoid_api.common.pagination import Cursors
from brainoid_api.device_command_watch import command_gateway
from brainoid_api.device_commands import DeviceCommands
from brainoid_api.device_gateway import Delivery, DeviceGateway
from brainoid_api.device_leases import DeviceLeases
from brainoid_api.device_notifications import DeviceNotifications
from brainoid_api.device_snapshot import DeviceSnapshots
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.home_assistant_entities import HomeAssistantEntities
from brainoid_api.repositories.agents import Agents
from brainoid_api.repositories.conversations import Conversations
from brainoid_api.repositories.device_credentials import DeviceCredentialRepository
from brainoid_api.repositories.devices import Devices
from brainoid_api.repositories.entities import Entities
from brainoid_api.repositories.invitations import Invitations
from brainoid_api.repositories.memberships import Memberships
from brainoid_api.repositories.memories import Memories
from brainoid_api.repositories.tenants import Tenants
from brainoid_api.services.calendar_authorization import CalendarAuthorization
from brainoid_api.services.calendar_disconnect import CalendarDisconnect
from brainoid_api.services.calendar_events import CalendarEvents
from brainoid_api.services.deletion_requests import DeletionRequests
from brainoid_api.services.export_requests import ExportRequests
from brainoid_api.services.private_sessions import PrivateSessions
from brainoid_api.settings import Settings


@dataclass(frozen=True)
class AuthServices:
    verifier: IdentityVerifier | None = None
    profiles: ProfileReader | None = None
    tenants: Tenants | None = None
    invitations: Invitations | None = None
    memberships: Memberships | None = None
    devices: DeviceCredentialRepository | None = None
    device_management: Devices | None = None
    private_sessions: PrivateSessions | None = None
    agents: Agents | None = None
    conversations: Conversations | None = None
    entities: Entities | None = None
    memories: Memories | None = None
    deletion_requests: DeletionRequests | None = None
    exports: ExportRequests | None = None
    calendar: CalendarAuthorization | None = None
    calendar_events: CalendarEvents | None = None
    calendar_write_enabled: bool = False
    calendar_disconnect: CalendarDisconnect | None = None
    device_gateway: DeviceGateway | None = None
    device_snapshots: DeviceSnapshots | None = None
    home_assistant: HomeAssistantEntities | None = None
    audio_inputs: AudioInputs | None = None


VectorSearchFactory = Callable[[Client], VectorSearch]


@asynccontextmanager
async def authentication_lifespan(
    settings: Settings, *, vector_search_factory: VectorSearchFactory | None = None
) -> AsyncIterator[AuthServices]:
    validate_sdk_environment(settings)
    if settings.app_env == "local-fake":
        # Health and non-auth fake development remain available; no dummy bearer is accepted.
        yield AuthServices()
        return
    assert settings.device_token_keyring_file is not None
    assert settings.device_credential_epoch is not None
    device_tokens = DeviceTokens.from_file(
        settings.device_token_keyring_file,
        project=settings.gcp_project_id,
        credential_epoch=settings.device_credential_epoch,
        test_only=settings.device_token_provider == "local-test",
    )
    app = firebase_admin.initialize_app(
        options={"projectId": settings.gcp_project_id, "httpTimeout": 10},
        name="brainoid-api-" + str(uuid4()),
    )
    client: Client | None = None
    storage_client = None
    speech: GoogleSpeech | None = None
    try:
        credential = (
            AnonymousCredentials()  # type: ignore[no-untyped-call]
            if settings.app_env == "local-emulator"
            else await run_in_threadpool(app.credential.get_credential)
        )
        client = Client(
            project=settings.gcp_project_id,
            database=settings.firestore_database_id,
            credentials=credential,
        )
        assert settings.cursor_signing_key is not None
        cursors = Cursors(
            settings.cursor_signing_key.get_secret_value().encode("utf-8"),
            now=lambda: datetime.now(UTC),
        )
        objects: Objects
        if settings.app_env == "local-emulator":
            assert settings.authz_ledger_directory is not None
            objects = LocalAuthzObjects(settings.authz_ledger_directory)
            ledger = ControlLedger(objects)
        else:
            assert settings.authz_ledger_bucket is not None
            storage_client = storage.Client(project=settings.gcp_project_id, credentials=credential)
            objects = GCSAuthzObjects(storage_client.bucket(settings.authz_ledger_bucket))
            ledger = ControlLedger(objects)
        store = FirestoreTransactions(client)
        tenants = Tenants(store, cursors, ledger=ledger)
        devices = DeviceCredentialRepository(store, tenants, ledger, device_tokens)
        management = Devices(store, tenants, ledger, cursors)
        home_assistant = HomeAssistantEntities(store, management, devices, ledger=ledger)
        sessions = PrivateSessions(store, tenants, ledger, devices)
        memories = Memories(
            store,
            tenants,
            cursors,
            ledger=ledger,
            credentials=devices,
            allow_untracked_bootstrap=True,
            vector_search=(vector_search_factory(client) if vector_search_factory else None),
            deletion_ledger=DeletionLedger(objects),
        )
        conversations = Conversations(
            store,
            tenants,
            cursors,
            Jobs(store, ledger, allow_untracked_bootstrap=settings.app_env == "local-emulator"),
            credentials=devices,
            sessions=sessions,
        )
        audio_inputs = None
        if settings.stt_provider == "google-v2":
            assert settings.stt_location and settings.stt_model and settings.stt_recognizer

            speech_credential = with_scopes_if_required(  # type: ignore[no-untyped-call]
                credential, ["https://www.googleapis.com/auth/cloud-platform"]
            )
            token_lock = asyncio.Lock()

            async def token() -> SecretStr:
                def refresh() -> SecretStr:
                    if not speech_credential.valid:
                        speech_credential.refresh(GoogleRequest())
                    if not speech_credential.token:
                        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
                    return SecretStr(speech_credential.token)

                async with token_lock:
                    return await run_in_threadpool(refresh)

            speech = GoogleSpeech(
                SpeechSettings(
                    project=settings.gcp_project_id,
                    location=settings.stt_location,
                    recognizer=settings.stt_recognizer,
                    model=settings.stt_model,
                ),
                token,
            )
            audio_inputs = AudioInputs(conversations, speech)
            conversations.audio_inputs = audio_inputs
        snapshots = DeviceSnapshots(conversations, memories, sessions)
        notifications = DeviceNotifications(snapshots)
        gateway = command_gateway(DeviceLeases(store, devices), client, DeviceCommands(memories))
        gateway.hooks += (notifications,)
        gateway.handlers["notification_ack"] = lambda payload: Delivery(
            "notification", lambda tx, binding: notifications.ack(tx, binding, payload)
        )
        yield AuthServices(
            FirebaseIdentityVerifier(app, settings),
            FirestoreProfiles(client),
            tenants,
            Invitations(
                store,
                tenants,
                ledger,
                code_key=settings.cursor_signing_key.get_secret_value().encode(),
            ),
            Memberships(store, tenants, ledger, cursors),
            devices,
            management,
            sessions,
            Agents(store, tenants, cursors, ledger=ledger, credentials=devices),
            conversations,
            Entities(
                store,
                tenants,
                cursors,
                ledger=ledger,
                credentials=devices,
                inventory=home_assistant.inventory,
                # Tenants.create/pairing bootstrap records are server-owned and
                # initially untracked, as in the other HTTP repositories. Tracked
                # records and pending ledger denies still require their fence.
                allow_untracked_bootstrap=True,
            ),
            memories,
            # Lifecycle provider finalization is explicitly composed by local tests.
            # No production admission until its provider/cache/export worker is wired.
            DeletionRequests(store, DeletionLedger(objects), ledger)
            if settings.app_env == "local-emulator"
            else None,
            device_gateway=gateway,
            device_snapshots=snapshots,
            home_assistant=home_assistant,
            audio_inputs=audio_inputs,
        )
    finally:
        try:
            if speech is not None:
                await speech.close()
            if storage_client is not None:
                await run_in_threadpool(storage_client.close)
            if client is not None:
                await run_in_threadpool(client.close)
        finally:
            await run_in_threadpool(firebase_admin.delete_app, app)


def get_auth_services(request: Request) -> AuthServices:
    services = cast(AuthServices | None, request.app.state.auth_services)
    if services is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services


async def authenticated_identity(
    request: Request, services: Annotated[AuthServices, Depends(get_auth_services)]
) -> AuthenticatedIdentity:
    headers = request.headers.getlist("authorization")
    if len(headers) != 1 or services.verifier is None:
        raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
    scheme, separator, token = headers[0].partition(" ")
    if (
        scheme.lower() != "bearer"
        or not separator
        or not token
        or len(token) > 16384
        or any(character.isspace() for character in token)
    ):
        raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
    return await run_in_threadpool(services.verifier.verify, token)


async def authenticated_device(
    request: Request, services: Annotated[AuthServices, Depends(get_auth_services)]
) -> DeviceIdentity:
    """Dedicated device dependency; never accepts or returns a Firebase user identity."""
    headers = request.headers.getlist("authorization")
    if len(headers) != 1 or services.devices is None:
        raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
    scheme, separator, token = headers[0].partition(" ")
    if scheme.lower() != "bearer" or not separator or not token or any(c.isspace() for c in token):
        raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
    return await run_in_threadpool(services.devices.authenticate, token)
