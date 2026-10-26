"""Authenticated worker entry and the handler/checkpoint contract."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from brainoid_contracts.transactions import Transaction
from brainoid_jobs import (
    AuthorizationUnavailable,
    Delivery,
    Job,
    Jobs,
    Lease,
    LeaseLost,
    RetryableError,
)
from brainoid_jobs.models import SourceObsolete
from brainoid_persistence.firestore import PersistenceUnavailable
from pydantic import JsonValue


class CallerDenied(Exception):
    def __init__(self) -> None:
        super().__init__("worker_caller_denied")


class CallerVerifier(Protocol):
    def verify(self, authorization: str) -> None:
        """Verify signature, issuer, audience and the explicitly allowed service identity."""
        ...


@dataclass(frozen=True)
class Execution:
    jobs: Jobs
    lease: Lease

    def check(self, *, renew: bool = False) -> Job:
        return self.jobs.check(self.lease, renew=renew)

    def effect(
        self,
        key: str,
        invoke: Callable[[str], JsonValue],
        *,
        commit: Callable[[Transaction, Job, JsonValue], JsonValue] | None = None,
    ) -> JsonValue:
        """Invoke after a durable start; stable key is suitable for provider idempotency.

        Completed checkpoints replay their result. An unresolved start requires a
        later business-specific reconciler, never blind reexecution by this runner.
        """
        return self.jobs.effect(self.lease, key, invoke, commit=commit)


class Handler(Protocol):
    def run(self, execution: Execution) -> JsonValue:
        """External I/O MUST use execution.effect; renew/check between long work steps."""
        ...

    def commit(self, tx: Transaction, job: Job, result: JsonValue) -> None:
        """DB-only, retryable callback after current authorization and lease validation.

        Read all business records first; writes must use tx. No providers/queue calls.
        """
        ...


@runtime_checkable
class Reconciles(Protocol):
    def reconcile(self, job: Job) -> None: ...


class Worker:
    def __init__(
        self, jobs: Jobs, verifier: CallerVerifier, handlers: Mapping[str, Handler]
    ) -> None:
        self.jobs = jobs
        self.verifier = verifier
        self.handlers = dict(handlers)

    def handle(self, authorization: str, payload: object) -> None:
        self.verifier.verify(authorization)
        delivery = Delivery.model_validate(payload)
        lease = self.jobs.acquire(delivery, known_handler=self.handlers.__contains__)
        if lease is None:
            return
        execution = Execution(self.jobs, lease)
        try:
            self._execute(execution)
        except (AuthorizationUnavailable, PersistenceUnavailable) as error:
            # Includes failure bookkeeping and ambiguous commit responses. Reconcile
            # persisted state; infrastructure failure is not a permanent handler error.
            self.jobs.defer_unavailable(
                lease,
                reason=(
                    "authorization_unavailable"
                    if isinstance(error, AuthorizationUnavailable)
                    else "persistence_unavailable"
                ),
            )
            raise

    def _execute(self, execution: Execution) -> None:
        lease = execution.lease
        job = None
        handler = None
        try:
            job = execution.check()
            handler = self.handlers[job.kind]
            result = handler.run(execution)
            self.jobs.complete(lease, result, handler.commit)
        except LeaseLost:
            # Another generation or a terminal authorization denial owns the record now.
            return
        except SourceObsolete:
            self.jobs.obsolete(lease)
        except (AuthorizationUnavailable, PersistenceUnavailable):
            raise
        except RetryableError:
            self.jobs.fail(lease, retry=True)
        except Exception:
            # Persist a fixed reason only. Provider exception strings may contain secrets.
            self.jobs.fail(lease)
        finally:
            if job is not None and isinstance(handler, Reconciles):
                handler.reconcile(job)
