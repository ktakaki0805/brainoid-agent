"""Authenticated global registration and atomic ownership/generation transfer."""

from uuid import uuid4

from brainoid_contracts import ResourceId
from brainoid_contracts.access import AuthorizedUserScope
from brainoid_contracts.idempotency import MutationResult
from brainoid_contracts.transactions import Transaction
from brainoid_memory.push_installations import (
    MAX_INSTALLATIONS,
    Installation,
    PushCreate,
    PushPatch,
    PushView,
    claim,
    claim_path,
    installation_path,
    view,
)

from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.common.versioning import require_if_match
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.tenants import Tenants


class PushInstallations:
    def __init__(self, tenants: Tenants) -> None:
        self.tenants = tenants
        self.store, self.now = tenants._store, tenants._now
        self.idempotency = tenants._idempotency

    def mutate(
        self,
        identity: AuthenticatedIdentity,
        body: PushCreate | PushPatch | None,
        key: str | None,
        *,
        installation: ResourceId | None = None,
        if_match: str | None = None,
    ) -> PushView | None:
        expected = None if installation is None else require_if_match(if_match)
        created = installation or ResourceId(str(uuid4()))
        old: Installation | None = None

        def read(tx: Transaction, resource: ResourceId, scope: AuthorizedUserScope) -> Installation:
            raw = tx.get(installation_path(resource))
            if raw is None:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            result = Installation.model_validate(raw)
            if (
                result.user_id != identity.user_id
                or result.account_stamp != scope.authorization_stamp
            ):
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            return result

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedUserScope:
            nonlocal old
            scope = self.tenants.authorize_user(tx, identity)
            if installation is not None:
                old = read(tx, installation, scope)
            if replay is not None:
                assert replay.resource_id is not None
                saved = read(tx, replay.resource_id, scope)
                if body is not None and saved.status != "active":
                    raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            return scope

        def mutate(tx: Transaction, scope: AuthorizedUserScope) -> MutationResult:
            if old is not None and old.version != expected:
                raise APIError(ErrorCode.VERSION_CONFLICT)
            if body is None:
                assert old is not None
                prior = None if old.fcm_token is None else tx.get(claim_path(old.fcm_token))
                if prior == claim(old) and old.fcm_token is not None:
                    tx.delete(claim_path(old.fcm_token))
                tx.set(
                    installation_path(old.id),
                    old.model_copy(
                        update={
                            "version": old.version + 1,
                            "status": "removed",
                            "fcm_token": None,
                            "generation": str(uuid4()),
                        }
                    ).model_dump(mode="json"),
                )
                return MutationResult(status_code=204, resource_id=old.id)
            token = body.fcm_token
            new_claim = tx.get(claim_path(token))
            displaced = None
            if new_claim is not None and new_claim["id"] != created:
                raw = tx.get(installation_path(ResourceId(new_claim["id"])))
                displaced = None if raw is None else Installation.model_validate(raw)
            previous_claim = (
                None if old is None or old.fcm_token is None else tx.get(claim_path(old.fcm_token))
            )
            active = tx.query(
                "push_installations", {"user_id": identity.user_id, "status": "active"}
            )
            retained = [
                d
                for _, d in active
                if d["id"] not in {created, None if displaced is None else displaced.id}
            ]
            if len(retained) >= MAX_INSTALLATIONS:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            record = Installation(
                id=created,
                platform=body.platform if isinstance(body, PushCreate) else old.platform,  # type: ignore[union-attr]
                version=1 if old is None else old.version + 1,
                created_at=self.now() if old is None else old.created_at,
                user_id=identity.user_id,
                account_stamp=scope.authorization_stamp,
                generation=ResourceId(str(uuid4())),
                status="active",
                fcm_token=token,
                app_version=body.app_version if body.app_version is not None else old.app_version,  # type: ignore[union-attr]
            )
            # All authority/ownership reads precede writes, including the former token.
            if displaced is not None and new_claim == claim(displaced):
                tx.set(
                    installation_path(displaced.id),
                    displaced.model_copy(
                        update={
                            "version": displaced.version + 1,
                            "status": "disabled",
                            "fcm_token": None,
                            "generation": str(uuid4()),
                        }
                    ).model_dump(mode="json"),
                )
            if (
                old is not None
                and old.fcm_token is not None
                and old.fcm_token != token
                and previous_claim == claim(old)
            ):
                tx.delete(claim_path(old.fcm_token))
            tx.set(installation_path(created), record.model_dump(mode="json"))
            tx.set(claim_path(token), claim(record))
            return MutationResult(
                status_code=201 if old is None else 200, resource_id=created, version=record.version
            )

        result = self.idempotency.execute_user(
            operation="push.register"
            if installation is None
            else f"push.{'remove' if body is None else 'refresh'}:{installation}",
            key=key,
            body={
                "body": None if body is None else body.model_dump(mode="json", exclude_none=True),
                "expected": expected,
            },
            authorize=authorize,
            mutate=mutate,
        )
        if body is None:
            return None
        assert result.resource_id is not None
        result_id = result.resource_id

        def fetch(tx: Transaction) -> PushView:
            scope = self.tenants.authorize_user(tx, identity)
            saved = read(tx, result_id, scope)
            if saved.status != "active":
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            return view(saved)

        return self.store.run(fetch)
