"""One-use Calendar OAuth orchestration; provider and secret I/O stay outside DB tx."""

import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from brainoid_contracts import DisplayPolicy, FirebaseUid, ResourceId, Scope, Visibility
from brainoid_contracts.access import AuthorizedScope, fingerprint
from brainoid_contracts.guards import GuardedTransactions, protect
from brainoid_contracts.idempotency import Idempotency, MutationResult
from brainoid_contracts.transactions import Document, Transaction
from brainoid_memory.calendar_oauth import (
    SCOPES,
    IntegrationPage,
    IntegrationView,
    OAuthProvider,
    OAuthStart,
    OAuthStartResponse,
    SecretStore,
    integration_view,
)
from pydantic import JsonValue, TypeAdapter

from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.common.pagination import page_limit
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.tenants import Tenants


def path(tenant: ResourceId, identity: ResourceId) -> str:
    return f"tenants/{tenant}/calendar_authorizations/{identity}"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class CalendarAuthorization:
    def __init__(
        self,
        tenants: Tenants,
        provider: OAuthProvider,
        secret_store: SecretStore,
        *,
        returns: dict[str, str],
    ) -> None:
        if set(returns) != {"mobile", "web"}:
            raise ValueError("Both registered OAuth return targets required")
        for value in returns.values():
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.query
                or parsed.fragment
                or any(c.isspace() for c in value)
            ):
                raise ValueError("Fixed HTTPS return targets required")
        self.tenants, self.provider, self.secrets, self.returns = (
            tenants,
            provider,
            secret_store,
            dict(returns),
        )
        self.store, self.now = GuardedTransactions(tenants._store), tenants._now
        self.idempotency = Idempotency(self.store, now=self.now)

    def authority(
        self, tx: Transaction, identity: AuthenticatedIdentity, tenant: ResourceId
    ) -> AuthorizedScope:
        user = self.tenants.authorize_user(tx, identity)
        principal = self.tenants._principal(tx, identity, tenant)
        result = AuthorizedScope(
            context=principal.context,
            scope=Scope(visibility=Visibility.PRIVATE, display_policy=DisplayPolicy.NEVER),
            owner_user_id=identity.user_id,
            authorization_stamp=fingerprint([principal.stamp, user.authorization_stamp]),
        )
        protect(tx, lambda: user.ensure_live(self.now()))
        return result

    def current(
        self,
        tx: Transaction,
        tenant: ResourceId,
        identity: ResourceId,
        *,
        origin: AuthenticatedIdentity | None = None,
    ) -> Document:
        saved = tx.get(path(tenant, identity))
        if saved is None or saved.get("tenant_id") != tenant or saved.get("id") != identity:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        actor = AuthenticatedIdentity.model_validate(saved["origin"])
        if saved.get("owner_user_id") != actor.user_id or (
            origin is not None and origin.user_id != actor.user_id
        ):
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        scope = self.authority(tx, origin or actor, tenant)
        if saved["binding"] != scope.binding() or (
            saved.get("secret_binding") is not None
            and saved["secret_binding"] != self.secret_binding(saved)
        ):
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        return saved

    @staticmethod
    def secret_binding(saved: Document) -> str:
        return fingerprint(
            [
                saved["binding"],
                saved["request"],
                saved["expires_at"].isoformat(),
                saved["id"],
                saved["tenant_id"],
                saved["owner_user_id"],
                saved["transient_slot"],
                saved["token_slot"],
                saved["integration_id"],
                saved["generation"],
            ]
        )

    def start(
        self, identity: AuthenticatedIdentity, tenant: ResourceId, body: OAuthStart, key: str | None
    ) -> OAuthStartResponse:
        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            scope = self.authority(tx, identity, tenant)
            if replay is not None:
                assert replay.resource_id is not None
                self.current(tx, tenant, replay.resource_id, origin=identity)
            return scope

        def reserve(tx: Transaction, scope: AuthorizedScope) -> MutationResult:
            auth = ResourceId(str(uuid4()))
            now = self.now()
            tx.create(
                path(tenant, auth),
                {
                    "id": auth,
                    "tenant_id": tenant,
                    "owner_user_id": identity.user_id,
                    "visibility": "private",
                    "origin": identity.model_dump(mode="json"),
                    "binding": scope.binding(),
                    "generation": scope.context.actor.authorization_version,
                    "request": body.model_dump(mode="json"),
                    "expires_at": min(now + timedelta(minutes=10), identity.expires_at),
                    "status": "preparing",
                    "transient_slot": str(uuid4()),
                    "token_slot": str(uuid4()),
                    "integration_id": str(uuid4()),
                    "created_at": now,
                    "cleanup_pending": True,
                },
            )
            return MutationResult(status_code=201, resource_id=auth)

        receipt = self.idempotency.execute(
            operation="calendar.authorize",
            key=key,
            body=body.model_dump(mode="json"),
            authorize=authorize,
            mutate=reserve,
        )
        assert receipt.resource_id is not None
        auth = receipt.resource_id
        saved = self.store.run(lambda tx: self.current(tx, tenant, auth, origin=identity))
        if saved["status"] not in {"preparing", "pending"} or self.now() >= saved["expires_at"]:
            raise APIError(ErrorCode.CONFLICT)
        secret_binding = self.secret_binding(saved)
        nonce = secrets.token_urlsafe(32)
        content = json.dumps(
            {
                "state": f"{tenant}.{auth}.{nonce}",
                "nonce": nonce,
                "verifier": secrets.token_urlsafe(48),
                "binding": secret_binding,
            }
        ).encode()
        try:
            reference = self.secrets.put(
                ResourceId(saved["transient_slot"]),
                tenant=tenant,
                user=identity.user_id,
                binding=secret_binding,
                content=content,
                expires_at=saved["expires_at"],
                generation=saved["generation"],
            )
            transient = json.loads(self.secrets.get(reference))
            if transient["binding"] != secret_binding:
                raise ValueError("Invalid secret binding")
            url = self.provider.authorization_url(
                state=transient["state"],
                verifier=transient["verifier"],
                scopes=SCOPES[body.scope_profile],
            )

            def ready(tx: Transaction) -> None:
                current = self.current(tx, tenant, auth, origin=identity)
                if (
                    current["status"] not in {"preparing", "pending"}
                    or self.now() >= current["expires_at"]
                ):
                    raise APIError(ErrorCode.CONFLICT)
                protect(tx, lambda: self.deadline(current))
                current.update(
                    status="pending",
                    state_hash=digest(transient["state"]),
                    nonce_hash=digest(transient["nonce"]),
                    transient_ref=reference,
                    secret_binding=secret_binding,
                )
                tx.set(path(tenant, auth), current)

            self.store.run(ready)
            return OAuthStartResponse(authorization_url=url, expires_at=saved["expires_at"])
        except Exception:
            # Reserved slots remain discoverable even when create's response is lost.
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None

    def callback(self, state: str, code: str | None, error: str | None) -> str:
        if (code is None) == (error is None):
            raise APIError(ErrorCode.INVALID_REQUEST)
        try:
            tenant_text, auth_text, _ = state.split(".")
            tenant, auth = (
                TypeAdapter(ResourceId).validate_python(tenant_text),
                TypeAdapter(ResourceId).validate_python(auth_text),
            )
        except (ValueError, TypeError):
            raise APIError(ErrorCode.INVALID_REQUEST) from None

        def consume(tx: Transaction) -> Document:
            saved = self.current(tx, tenant, auth)
            if (
                saved["status"] != "pending"
                or self.now() >= saved["expires_at"]
                or not hmac.compare_digest(saved.get("state_hash", ""), digest(state))
            ):
                raise APIError(ErrorCode.INVALID_REQUEST)
            protect(tx, lambda: self.deadline(saved))
            saved["status"] = "exchanging"
            tx.set(path(tenant, auth), saved)
            return saved

        saved = self.store.run(consume)
        try:
            if error is not None:
                self.finish_failed(tenant, auth)
                return self.returns[saved["request"]["return_target"]]
            transient = json.loads(self.secrets.get(saved["transient_ref"]))
            if (
                transient["binding"] != saved["secret_binding"]
                or transient["state"] != state
                or digest(transient["nonce"]) != saved["nonce_hash"]
            ):
                raise ValueError("Invalid secret binding")
            # Revalidate after secret I/O and immediately before the irreversible exchange.
            self.store.run(lambda tx: self.live_exchange(tx, tenant, auth))
            assert code is not None
            grant = self.provider.exchange(code=code, verifier=transient["verifier"])
            # Only affirmative provider evidence is recorded. Missing or unrelated scopes fail.
            scopes = sorted(grant.scopes.intersection(SCOPES[saved["request"]["scope_profile"]]))
            if (
                not scopes
                or not grant.access_token.get_secret_value()
                or not grant.refresh_token.get_secret_value()
            ):
                raise ValueError("Calendar grant missing")
            self.store.run(lambda tx: self.live_exchange(tx, tenant, auth))
            payload = json.dumps(
                {
                    "access_token": grant.access_token.get_secret_value(),
                    "refresh_token": grant.refresh_token.get_secret_value(),
                }
            ).encode()
            token_ref = self.secrets.put(
                ResourceId(saved["token_slot"]),
                tenant=tenant,
                user=FirebaseUid(saved["owner_user_id"]),
                binding=saved["secret_binding"],
                content=payload,
                expires_at=None,
                generation=saved["generation"],
            )

            def commit(tx: Transaction) -> None:
                current = self.live_exchange(tx, tenant, auth)
                now = self.now()
                tx.create(
                    f"tenants/{tenant}/integrations/{saved['integration_id']}",
                    {
                        "id": saved["integration_id"],
                        "tenant_id": tenant,
                        "version": 1,
                        "created_at": now,
                        "updated_at": now,
                        "created_by": saved["owner_user_id"],
                        "owner_user_id": saved["owner_user_id"],
                        "visibility": "private",
                        "display_policy": "never",
                        "deletion_epoch": 0,
                        "deleted": False,
                        "provider": "google_calendar",
                        "status": "active",
                        "granted_scopes": scopes,
                        "secret_ref": token_ref,
                        "authorization_id": auth,
                        "binding": saved["binding"],
                    },
                )
                current.update(status="connected", secret_ref=token_ref)
                tx.set(path(tenant, auth), current)

            self.store.run(commit)
            self.cleanup(tenant, auth)
            return self.returns[saved["request"]["return_target"]]
        except Exception:
            # This may be a committed write with a lost response: read its durable
            # state before cleanup. Never exchange again or erase confirmed tokens.
            try:
                self.finish_failed(tenant, auth)
            except Exception:
                pass  # Durable reservation remains cleanup_pending for recovery.
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None

    def deadline(self, saved: Document) -> None:
        if self.now() >= saved["expires_at"]:
            raise APIError(ErrorCode.INVALID_REQUEST)

    def live_exchange(self, tx: Transaction, tenant: ResourceId, auth: ResourceId) -> Document:
        saved = self.current(tx, tenant, auth)
        protect(tx, lambda: self.deadline(saved))
        if saved["status"] != "exchanging":
            raise APIError(ErrorCode.INVALID_REQUEST)
        return saved

    def finish_failed(self, tenant: ResourceId, auth: ResourceId) -> None:
        def failed(tx: Transaction) -> None:
            saved = tx.get(path(tenant, auth))
            if saved is not None and saved["status"] != "connected":
                saved.update(status="failed", cleanup_pending=True)
                tx.set(path(tenant, auth), saved)

        self.store.run(failed)
        self.cleanup(tenant, auth)

    def cleanup(self, tenant: ResourceId, auth: ResourceId) -> None:
        saved = self.store.run(lambda tx: tx.get(path(tenant, auth)))
        if saved is None:
            return  # Accepted lifecycle hook owns every scoped slot after canonical purge.
        if saved["status"] not in {"connected", "failed"}:
            raise APIError(ErrorCode.CONFLICT)
        self.secrets.revoke(ResourceId(saved["transient_slot"]))
        if saved["status"] == "failed":
            self.secrets.revoke(ResourceId(saved["token_slot"]))

        def done(tx: Transaction) -> None:
            current = tx.get(path(tenant, auth))
            if current is not None and current["status"] == saved["status"]:
                current["cleanup_pending"] = False
                tx.set(path(tenant, auth), current)

        self.store.run(done)

    def recover(self, tenant: ResourceId, auth: ResourceId) -> None:
        """Operator/worker entry, never an unauthenticated HTTP endpoint.

        Only terminal or expired attempts may be recovered. An uncertain in-flight
        exchange is aborted after its deadline; durable slot fences reject late writes.
        """
        saved = self.store.run(lambda tx: tx.get(path(tenant, auth)))
        if saved is None:
            return
        if saved["status"] in {"connected", "failed"}:
            self.cleanup(tenant, auth)
        elif self.now() >= saved["expires_at"]:
            self.finish_failed(tenant, auth)

    def integrations(
        self, identity: AuthenticatedIdentity, tenant: ResourceId
    ) -> list[IntegrationView]:
        def read(tx: Transaction) -> list[IntegrationView]:
            scope = self.authority(tx, identity, tenant)
            return [
                integration_view(record)
                for _, record in tx.query(
                    f"tenants/{tenant}/integrations",
                    {"owner_user_id": identity.user_id, "provider": "google_calendar"},
                )
                if record.get("binding") == scope.binding()
                and record.get("visibility") == "private"
            ]

        return self.store.run(read)

    def list(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> IntegrationPage:
        limit = page_limit(limit)

        def read(tx: Transaction) -> IntegrationPage:
            scope = self.authority(tx, identity, tenant)
            records = [
                integration_view(record)
                for _, record in tx.query(
                    f"tenants/{tenant}/integrations",
                    {"owner_user_id": identity.user_id, "provider": "google_calendar"},
                )
                if record.get("binding") == scope.binding()
                and record.get("visibility") == "private"
            ]
            query: JsonValue = {"operation": "calendar.integrations", "limit": limit}
            position = (
                ""
                if cursor is None
                else self.tenants._cursors.read(cursor, query=query, authorize=lambda: scope)
            )
            remaining = sorted(
                (record for record in records if record.id > position), key=lambda r: r.id
            )
            items = remaining[:limit]
            next_cursor = (
                None
                if len(remaining) <= limit
                else self.tenants._cursors.issue(
                    position=items[-1].id, query=query, authorize=lambda: scope
                )
            )
            return IntegrationPage(items=items, next_cursor=next_cursor)

        return self.store.run(read)
