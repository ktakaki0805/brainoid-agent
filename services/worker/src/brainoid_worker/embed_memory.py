"""Memory outbox activation and fenced, single-attempt embedding effects."""

from dataclasses import dataclass
from uuid import UUID, uuid5

from brainoid_contracts import ResourceId, TenantContext, normalize_utc
from brainoid_contracts.guards import GuardedTransactions
from brainoid_contracts.transactions import Transaction
from brainoid_jobs import AuthorizationUnavailable, Job, Jobs
from brainoid_jobs.models import SourceObsolete, job_id
from brainoid_jobs.repository import path
from brainoid_memory import CanonicalSources, MemoryRepository, VerifiedCaller
from brainoid_memory.models import Fact
from brainoid_memory.repository import FactTransaction
from brainoid_memory.vector_store import (
    Embeddings,
    VectorConfigurationError,
    VectorIndex,
    fact_text,
)
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_runs.memory_sources import SavedMessages
from pydantic import JsonValue, ValidationError

from brainoid_worker.dispatcher import Dispatcher, JobPages, Queue, QueueUnavailable, RecoveryPage
from brainoid_worker.entry import Execution

KIND = "memory_embed"


class _Caller:
    def __init__(self, jobs: Jobs, job: Job) -> None:
        self.jobs, self.job = jobs, job

    def authorize(self, tx: Transaction, context: TenantContext) -> VerifiedCaller:
        live = self.jobs._read(tx, self.job.tenant_id, self.job.id)
        if (
            live is None
            or live.status != "running"
            or live.lease_generation != self.job.lease_generation
            or live.lease_expires_at is None
            or live.lease_expires_at <= normalize_utc(self.jobs.now())
            or live.grant != self.job.grant
            or live.attributes != self.job.attributes
            or context != live.grant.context
            or self.jobs._invalid_reason(tx, live) is not None
        ):
            raise PolicyDenied()
        return VerifiedCaller(
            subject=live.grant.principal.subject,
            valid_from=live.created_at,
            valid_to=live.lease_expires_at,
        )


@dataclass(frozen=True)
class ActivationPage:
    inspected: int
    activated: int
    next_cursor: str | None


class EmbedMemory:
    def __init__(self, jobs: Jobs, embeddings: Embeddings, index: VectorIndex) -> None:
        if embeddings.config != index.config:
            raise VectorConfigurationError()
        if not isinstance(jobs.store, GuardedTransactions):
            jobs.store = GuardedTransactions(jobs.store)
        self.jobs, self.embeddings, self.index = jobs, embeddings, index

    def activate(self, tenant: ResourceId, identity: ResourceId) -> ResourceId | None:
        """Identifier-only scheduling. No source content crosses a provider boundary.

        Keep the original waiting event for explicit generation rebuilds. Each
        generation preserves the original (never expanded) authorization grant.
        Full canonical checks need a bounded acquired lease and happen in run.
        """

        def work(tx: Transaction) -> ResourceId | None:
            source = self.jobs._read(tx, tenant, identity)
            if source is None or source.kind != "memory_index" or source.status != "waiting":
                return None
            reason = self.jobs._invalid_reason(tx, source)
            if reason is not None:
                self.jobs._stop(tx, source, "obsolete", reason)
                return None
            operation = ResourceId(
                str(uuid5(UUID(source.grant.ref.id), f"embedding:{self.index.config.key}"))
            )
            target = job_id(tenant, KIND, operation)
            existing = self.jobs._read(tx, tenant, target)
            if existing is not None:
                return target
            now = normalize_utc(self.jobs.now())
            job = Job(
                id=target,
                tenant_id=tenant,
                operation_id=operation,
                kind=KIND,
                grant=source.grant,
                output=source.output,
                account_stamps=source.account_stamps,
                created_at=now,
                available_at=now,
                dispatch_after=now,
                attributes={
                    "embedding": self.index.config.model_dump(mode="json"),
                    "outbox_id": source.id,
                    "dispatch_queue": KIND,
                },
            )
            tx.create(path(tenant, target), job.model_dump(mode="json"))
            return target

        return self.jobs.store.run(work)

    def activate_page(
        self, pages: JobPages, tenant: ResourceId, *, after: str | None = None, limit: int = 100
    ) -> ActivationPage:
        if not 1 <= limit <= 100 or (after is not None and "/" in after):
            raise ValueError("Invalid activation page")
        documents = pages.page(f"tenants/{tenant}/jobs", after=after, limit=limit)
        activated = 0
        for location, document in documents:
            if document.get("kind") == "memory_index" and document.get("status") == "waiting":
                activated += (
                    self.activate(tenant, ResourceId(location.rsplit("/", 1)[-1])) is not None
                )
        return ActivationPage(
            len(documents),
            activated,
            documents[-1][0].rsplit("/", 1)[-1] if len(documents) == limit else None,
        )

    def _read(self, tx: Transaction, job: Job) -> tuple[Fact, FactTransaction]:
        if job.kind != KIND or job.attributes.get("embedding") != self.index.config.model_dump(
            mode="json"
        ):
            raise VectorConfigurationError()
        repository = MemoryRepository(
            self.jobs.store,
            authorizer=_Caller(self.jobs, job),
            sources=CanonicalSources(SavedMessages()),
            ledger=self.jobs.ledger,
            now=self.jobs.now,
            allow_untracked_bootstrap=self.jobs.allow_untracked,
            output=job.output,
        )
        access = repository.facts(tx, job.grant.context)
        fact = access.get(job.grant.ref.id, for_model=True)
        if (fact.version, fact.deletion_epoch) != (
            job.grant.resource.version,
            job.grant.resource.deletion_epoch,
        ):
            raise PolicyDenied()
        access.finish_records((fact,))
        return fact, access

    def run(self, execution: Execution) -> JsonValue:
        job = execution.check()

        def invoke(effect: str) -> JsonValue:
            fact, access = self.jobs.store.run(lambda tx: self._read(tx, job))
            text = fact_text(fact)
            access.finish_records((fact,))
            return list(self.index.config.vector(self.embeddings.embed(text, task="document")))

        def commit(tx: Transaction, current: Job, value: JsonValue) -> JsonValue:
            fact, access = self._read(tx, current)
            if not isinstance(value, list) or any(type(v) not in (int, float) for v in value):
                raise VectorConfigurationError()
            vector = self.index.config.vector(tuple(float(v) for v in value))  # type: ignore[arg-type]
            access.finish_records((fact,))
            self.index.save(tx, fact, vector)
            return {
                "index": self.index.config.key,
                "version": fact.version,
                "deletion_epoch": fact.deletion_epoch,
            }

        try:
            # Validate before even starting the effect, then again after the durable
            # start marker immediately before sending any canonical source text.
            fact, access = self.jobs.store.run(lambda tx: self._read(tx, job))
            access.finish_records((fact,))
            return execution.effect("embed." + self.index.config.key, invoke, commit=commit)
        except PolicyUnavailable:
            raise AuthorizationUnavailable() from None
        except (PolicyDenied, ValidationError):
            raise SourceObsolete() from None

    def commit(self, tx: Transaction, job: Job, result: JsonValue) -> None:
        try:
            fact, access = self._read(tx, job)
            if not self.index.current(tx, fact):
                raise SourceObsolete()
            if result != {
                "index": self.index.config.key,
                "version": fact.version,
                "deletion_epoch": fact.deletion_epoch,
            }:
                raise VectorConfigurationError()
            access.finish_records((fact,))
        except PolicyUnavailable:
            raise AuthorizationUnavailable() from None
        except (PolicyDenied, ValidationError):
            raise SourceObsolete() from None


class EmbeddingDispatcher:
    """A dedicated queue's bounded recovery for old/new waiting outbox and retries."""

    def __init__(self, handler: EmbedMemory, pages: JobPages, queue: Queue) -> None:
        self.handler, self.pages = handler, pages
        self.dispatcher = Dispatcher(handler.jobs, pages, queue, queue_name=KIND)

    def recover(
        self, tenant: ResourceId, *, after: str | None = None, limit: int = 100
    ) -> RecoveryPage:
        if not 1 <= limit <= 100 or (after is not None and "/" in after):
            raise ValueError("Invalid embedding recovery page")
        documents = self.pages.page(f"tenants/{tenant}/jobs", after=after, limit=limit)
        sent = errors = 0
        for location, document in documents:
            identity = ResourceId(location.rsplit("/", 1)[-1])
            if document.get("kind") == "memory_index" and document.get("status") == "waiting":
                target = self.handler.activate(tenant, identity)
            elif document.get("kind") == KIND and document.get("attributes", {}).get(
                "embedding"
            ) == self.handler.index.config.model_dump(mode="json"):
                target = identity
            else:
                continue
            if target is not None:
                try:
                    sent += self.dispatcher.dispatch(tenant, target)
                except QueueUnavailable:
                    errors += 1
        return RecoveryPage(
            len(documents),
            sent,
            errors,
            documents[-1][0].rsplit("/", 1)[-1] if len(documents) == limit else None,
        )
