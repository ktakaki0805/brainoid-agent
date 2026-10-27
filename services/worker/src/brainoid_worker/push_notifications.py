"""Bounded per-installation send evidence; an unknown POST is never retried."""

from typing import Literal, Protocol

from brainoid_contracts import ResourceId
from brainoid_contracts.access import fingerprint
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Jobs
from brainoid_jobs.models import Job, OutcomeUnknown, job_id
from brainoid_memory.notifications import KIND, Notification, notification_path
from brainoid_memory.push_installations import (
    MAX_INSTALLATIONS,
    Installation,
    claim_path,
    current_installation,
    installation_path,
)
from brainoid_policy.scopes import PolicyDenied
from pydantic import JsonValue, TypeAdapter

from brainoid_worker.notifications import DispatchNotification, Receipt

GENERIC_BODY = "新しい通知があります。アプリで確認してください。"
PushResult = Literal["accepted", "invalid", "rejected", "unknown"]


class PushTransport(Protocol):
    def send(self, token: str, data: dict[str, str]) -> PushResult: ...


class PushNotifications:
    """One explicitly configured tenant, injected transport, no automatic SDK startup."""

    def __init__(self, jobs: Jobs, tenant: ResourceId, transport: PushTransport) -> None:
        self.jobs, self.tenant, self.transport = jobs, tenant, transport

    def _job(self, tx: Transaction, key: str, *, sending: bool) -> tuple[Job, Notification]:
        identity, separator, step = key.partition(":")
        if not separator or step != "deliver":
            raise PolicyDenied()
        job = self.jobs._read(tx, self.tenant, TypeAdapter(ResourceId).validate_python(identity))
        if job is None or self.jobs._invalid_reason(tx, job) is not None:
            raise PolicyDenied()
        record, delivery, _, _ = DispatchNotification(self.jobs, self)._current(tx, job)
        if record.target.kind != "user" or job.id != job_id(self.tenant, KIND, record.id):
            raise PolicyDenied()
        if sending and delivery.acknowledged_at is not None:
            raise PolicyDenied()
        if sending and (
            job.status != "running"
            or job.lease_expires_at is None
            or job.lease_expires_at <= self.jobs.now()
        ):
            raise PolicyDenied()
        return job, record

    @staticmethod
    def _path(record: Notification, key: str) -> str:
        return notification_path(record.tenant_id, record.id) + "/push_batches/" + fingerprint(key)

    def send(self, key: str, payload: dict[str, JsonValue]) -> Receipt:
        def prepare(tx: Transaction) -> tuple[Notification, Document]:
            job, record = self._job(tx, key, sending=True)
            if any(
                payload.get(k) != v
                for k, v in {
                    "tenant_id": record.tenant_id,
                    "notification_id": record.id,
                    "target_kind": "user",
                    "job_lease_generation": job.lease_generation,
                    "target_id": record.user_id,
                }.items()
            ):
                raise PolicyDenied()
            path = self._path(record, key)
            saved = tx.get(path)
            if saved is not None:
                return record, saved
            installations = [
                Installation.model_validate(d)
                for _, d in tx.query(
                    "push_installations", {"user_id": record.user_id, "status": "active"}
                )
            ]
            if len(installations) > MAX_INSTALLATIONS:
                raise PolicyDenied()
            targets = []
            for item in installations:
                if item.account_stamp == record.recipient_account_stamp and current_installation(
                    tx, item
                ):
                    targets.append(
                        {"id": item.id, "generation": item.generation, "state": "pending"}
                    )
            saved = {
                "lease_generation": job.lease_generation,
                "targets": targets,
                "complete": False,
            }
            tx.create(path, saved)
            return record, saved

        record, batch = self.jobs.store.run(prepare)
        data = {
            "notification_id": str(record.id),
            "tenant_id": str(record.tenant_id),
            "body": GENERIC_BODY,
        }
        for index in range(len(batch["targets"])):

            def start(tx: Transaction, index: int = index) -> str | None:
                job, current = self._job(tx, key, sending=True)
                path = self._path(current, key)
                saved = tx.get(path)
                if saved is None or saved["lease_generation"] != job.lease_generation:
                    raise PolicyDenied()
                target = saved["targets"][index]
                if target["state"] != "pending":
                    return None
                raw = tx.get(installation_path(ResourceId(target["id"])))
                item = None if raw is None else Installation.model_validate(raw)
                valid = (
                    item is not None
                    and item.user_id == current.user_id
                    and item.generation == target["generation"]
                    and item.account_stamp == current.recipient_account_stamp
                    and current_installation(tx, item)
                )
                target["state"] = "started" if valid else "skipped"
                self.jobs._write(
                    tx, job, lease_expires_at=self.jobs.now() + self.jobs.lease_duration
                )
                tx.set(path, saved)
                return item.fcm_token if valid and item is not None else None

            token = self.jobs.store.run(start)
            if token is None:
                continue
            # No provider exception/value (potentially containing the token) escapes.
            try:
                result = self.transport.send(token, data)
                if result not in {"accepted", "invalid", "rejected", "unknown"}:
                    result = "unknown"
            except Exception:
                result = "unknown"

            def finish(tx: Transaction, index: int = index, result: PushResult = result) -> None:
                job, current = self._job(tx, key, sending=True)
                path = self._path(current, key)
                saved = tx.get(path)
                if saved is None or saved["lease_generation"] != job.lease_generation:
                    raise PolicyDenied()
                target = saved["targets"][index]
                raw = tx.get(installation_path(ResourceId(target["id"])))
                item = None if raw is None else Installation.model_validate(raw)
                valid = (
                    item is not None
                    and item.user_id == current.user_id
                    and item.generation == target["generation"]
                    and item.account_stamp == current.recipient_account_stamp
                    and current_installation(tx, item)
                )
                target["state"] = result
                if valid and item is not None and result == "invalid":
                    assert item.fcm_token is not None
                    tx.delete(claim_path(item.fcm_token))
                    tx.set(
                        installation_path(item.id),
                        item.model_copy(
                            update={
                                "status": "disabled",
                                "fcm_token": None,
                            }
                        ).model_dump(mode="json"),
                    )
                tx.set(path, saved)

            self.jobs.store.run(finish)

        def complete(tx: Transaction) -> Receipt | None:
            _, current = self._job(tx, key, sending=True)
            path = self._path(current, key)
            saved = tx.get(path)
            assert saved is not None
            saved["complete"] = True
            result = self._receipt(saved)
            tx.set(path, saved)
            return result

        receipt = self.jobs.store.run(complete)
        if receipt is None:
            raise OutcomeUnknown()
        return receipt

    @staticmethod
    def _receipt(batch: Document) -> Receipt | None:
        states = {item["state"] for item in batch["targets"]}
        if not batch["complete"] or not states <= {"accepted", "invalid", "rejected", "skipped"}:
            return None
        return "accepted" if "accepted" in states else "offline"

    def lookup(self, key: str) -> Receipt | None:
        # FCM has no lookup by our effect key. Only our confirmed local evidence counts.
        def read(tx: Transaction) -> Receipt | None:
            _, current = self._job(tx, key, sending=False)
            saved = tx.get(self._path(current, key))
            return None if saved is None else self._receipt(saved)

        try:
            return self.jobs.store.run(read)
        except PolicyDenied:
            return None
