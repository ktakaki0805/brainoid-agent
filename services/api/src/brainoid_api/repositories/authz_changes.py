"""Separate independent intent, DB application, confirmation, and activation phases."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from brainoid_policy.control_ledger import (
    Change,
    ControlLedger,
    Entry,
    LedgerConflict,
    LedgerError,
    MemberTarget,
)

from brainoid_api.common.transactions import Document, Transaction, TransactionStore
from brainoid_api.errors import APIError, ErrorCode


@dataclass(frozen=True)
class ReapplyResult:
    state: Literal["applied", "suspended", "missing"]
    operation_id: str | None


def _locate(tx: Transaction, change: Change) -> tuple[str, Document | None]:
    target = change.target
    if isinstance(target, MemberTarget):
        collection = f"tenants/{change.tenant_id}/memberships"
        matches = [
            (path, document)
            for path, document in tx.memberships(target.user_id)
            if path.rsplit("/", 1)[0] == collection
        ]
        if len(matches) > 1:
            raise LedgerConflict()
        if matches:
            path, document = matches[0]
        else:
            path, document = f"{collection}/{change.subject_key}", None
    else:
        path = f"tenants/{change.tenant_id}/devices/{target.device_id}"
        document = tx.get(path)
        if document is not None and document.get("id") != target.device_id:
            raise LedgerConflict()
    if document is not None and document.get("tenant_id") != change.tenant_id:
        raise LedgerConflict()
    return path, document


def _version(document: Document | None) -> int:
    if document is None:
        return 0
    version = document.get("authorization_version")
    if type(version) is not int or version < 1:
        raise LedgerConflict()
    return version


def _fields(change: Change, *, active: bool) -> Document:
    target = change.target.model_dump(mode="json", exclude={"kind"})
    if not isinstance(change.target, MemberTarget):
        target["id"] = target.pop("device_id")
    return {
        **target,
        "tenant_id": change.tenant_id,
        "status": change.target.status if active else "suspended",
        "authorization_version": change.authorization_version,
        "authorization_operation_id": change.operation_id,
        "authorization_change": change.model_dump(mode="json"),
    }


def _matches(document: Document | None, change: Change) -> bool:
    return document is not None and all(
        document.get(key) == value
        for key, value in _fields(change, active=False).items()
        if key != "status"
    )


class AuthzChanges:
    """Trusted service boundary, not a public generic PATCH API.

    authorize must re-read actor, tenant, target, request version and business rules
    in its transaction, including on retry. DTO validation never authorizes changes.
    Atomic owner transfer and endpoint integration belong to IAM-06/08.
    """

    def __init__(self, store: TransactionStore, ledger: ControlLedger) -> None:
        self._store = store
        self._ledger = ledger

    def _latest(self, change: Change) -> Entry:
        entry = self._ledger.latest(change.subject_key)
        if entry is None or entry.change != change:
            raise LedgerConflict()
        return entry

    def execute(self, change: Change, *, authorize: Callable[[Transaction, Change], None]) -> Entry:
        try:

            def validate(tx: Transaction) -> None:
                authorize(tx, change)
                _, document = _locate(tx, change)
                if _version(document) != change.expected_version and not _matches(document, change):
                    raise LedgerConflict()

            self._store.run(validate)
            self._ledger.prepare(change)
            self._latest(change)

            def apply(tx: Transaction) -> None:
                authorize(tx, change)
                path, document = _locate(tx, change)
                if _matches(document, change):
                    return
                if _version(document) != change.expected_version:
                    raise LedgerConflict()
                tx.set(path, {**(document or {}), **_fields(change, active=False)})

            self._store.run(apply)

            def confirm(tx: Transaction) -> None:
                _, document = _locate(tx, change)
                if not _matches(document, change):
                    raise LedgerConflict()

            self._store.run(confirm)
            self._ledger.commit(change)
            self._latest(change)

            def activate(tx: Transaction) -> None:
                path, document = _locate(tx, change)
                if not _matches(document, change):
                    raise LedgerConflict()
                assert document is not None
                updated = {**document, **_fields(change, active=True)}
                if updated != document:
                    tx.set(path, updated)

            self._store.run(activate)
            return self._latest(change)
        except LedgerConflict:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN) from None
        except LedgerError:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None

    def reapply(self, change: Change) -> ReapplyResult:
        """Isolated restore/repair only; accepts an identity hint, reads latest itself.

        Never commits a pending intent. It may advance an older DB to the latest
        committed target, or suspend it at the pending intent's version. The restore
        operator must stop all writers and enumerate every subject (OPS-07).
        """
        try:
            entry = self._ledger.latest(change.subject_key)
            current = change if entry is None else entry.change

            def repair(tx: Transaction) -> ReapplyResult:
                path, document = _locate(tx, current)
                if entry is None:
                    if document is not None and document.get("status") != "suspended":
                        tx.set(path, {**document, "status": "suspended"})
                    return ReapplyResult("missing", None)
                if _version(document) > current.authorization_version:
                    if document is not None and document.get("status") != "suspended":
                        tx.set(path, {**document, "status": "suspended"})
                    return ReapplyResult("suspended", current.operation_id)
                active = entry.state == "committed"
                updated = {**(document or {}), **_fields(current, active=active)}
                if document != updated:
                    tx.set(path, updated)
                return ReapplyResult("applied" if active else "suspended", current.operation_id)

            return self._store.run(repair)
        except LedgerError:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
