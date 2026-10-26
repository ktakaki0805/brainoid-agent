"""Fence every consumed result before model input, streaming and final output."""

from uuid import UUID, uuid5

from brainoid_agent_core.checkpoints import continuation_messages
from brainoid_agent_core.types import ModelMessage, ToolReply
from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Job
from brainoid_jobs.models import job_id
from brainoid_jobs.repository import path as job_path
from brainoid_policy.accounts import fingerprint
from brainoid_policy.scopes import PolicyDenied
from brainoid_runs.state import KIND, run_path
from brainoid_tools import Registry, ResultAuthorizer, ResultBinding, ResultConsumer, ToolRejected
from brainoid_tools.executor import Authorizer
from brainoid_tools.reuse import receipt, verify_result


class ResultReuse:
    def __init__(self, registry: Registry | None, authorizer: Authorizer | None) -> None:
        self.registry, self.authorizer = registry, authorizer

    def check(self, tx: Transaction, job: Job, run: Document, history: list[Document]) -> str:
        receipts = [self.current(tx, job, run)]
        for message in history:
            snapshots = message.get("model_messages")
            if snapshots:
                origin = ResourceId(message["run_id"])
                source = tx.get(run_path(job.tenant_id, origin))
                if (
                    source is None
                    or source.get("status") != "completed"
                    or source.get("final_message_id") != message["id"]
                ):
                    raise PolicyDenied()
                receipts.append(
                    self._group(tx, job, origin, snapshots[:-1], message.get("tool_bindings", []))
                )
            elif message.get("tool_bindings"):
                raise PolicyDenied()
        return fingerprint(receipts)

    def current(self, tx: Transaction, job: Job, run: Document) -> str:
        return self._group(
            tx, job, job.operation_id, run.get("continuation", []), run.get("tool_bindings", [])
        )

    def _group(
        self,
        tx: Transaction,
        consumer_job: Job,
        origin: ResourceId,
        snapshots: list[Document],
        records: list[Document],
    ) -> str:
        messages = continuation_messages(tuple(ModelMessage.model_validate(m) for m in snapshots))
        bindings = [ResultBinding.model_validate(record) for record in records]
        if not messages and not bindings:
            return fingerprint([])
        if self.registry is None or not isinstance(self.authorizer, ResultAuthorizer):
            raise PolicyDenied()
        origin_doc = tx.get(
            job_path(consumer_job.tenant_id, job_id(consumer_job.tenant_id, KIND, origin))
        )
        if origin_doc is None:
            raise PolicyDenied()
        origin_job = Job.model_validate(origin_doc)
        if origin_job.grant.ref != consumer_job.grant.ref:
            raise PolicyDenied()
        consumer = ResultConsumer(
            run_id=consumer_job.operation_id,
            context=consumer_job.grant.context,
            conversation_id=consumer_job.grant.ref.id,
            conversation_scope=consumer_job.grant.scope,
            conversation_owner_user_id=consumer_job.grant.owner_user_id,
        )
        receipts = []
        index = 0
        try:
            for step in range(len(messages) // 2):
                response, reply = messages[step * 2 : step * 2 + 2]
                calls = [p for p in response.parts if p.type == "tool_call"]
                for position, call in enumerate(calls):
                    if index >= len(bindings):
                        raise PolicyDenied()
                    binding = bindings[index]
                    index += 1
                    request, result = binding.request, binding.result
                    expected_reply = ToolReply(
                        call_key=call.call_key,
                        name=call.name,
                        result={
                            "status": result.status,
                            "result": result.result,
                            "error_code": result.error_code,
                        },
                        is_error=result.status == "failed",
                    )
                    if (
                        request.call != call
                        or request.context != origin_job.grant.context
                        or request.tool_call_id
                        != str(uuid5(UUID(origin), f"tool:{step}:{position}"))
                        or request.idempotency_key != f"{origin}:{step}:{position}"
                        or reply.parts[position] != expected_reply
                    ):
                        raise PolicyDenied()
                    arguments = verify_result(tx, self.registry, binding, origin)
                    current = self.authorizer.check_result(
                        tx,
                        request.model_copy(deep=True),
                        arguments.model_copy(deep=True),
                        result.model_copy(deep=True),
                        consumer=consumer.model_copy(deep=True),
                    )
                    receipts.append([binding.model_dump(mode="json"), receipt(current, consumer)])
            if index != len(bindings):
                raise PolicyDenied()
        except ToolRejected:
            raise PolicyDenied() from None
        return fingerprint(receipts)
