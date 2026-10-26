"""Synthetic direct-user summary provenance, shared by fake and Firestore tests."""

from brainoid_contracts.transactions import Document

from fakes.memory_repository import CONVERSATION, FACT, NOW, SOURCE, TENANT, USER, authority_data

BASE = f"tenants/{TENANT}/conversations/{CONVERSATION}"
MESSAGE = BASE + "/messages/" + SOURCE
ASSISTANT = BASE + "/messages/" + FACT


def summary_data() -> dict[str, Document]:
    return {
        **authority_data(),
        BASE: {
            "id": CONVERSATION,
            "tenant_id": TENANT,
            "owner_user_id": USER,
            "created_by": USER,
            "version": 1,
            "deletion_epoch": 0,
            "visibility": "private",
            "display_policy": "never",
            "deleted": False,
            "suppress_reuse": False,
        },
        MESSAGE: {
            "id": SOURCE,
            "tenant_id": TENANT,
            "conversation_id": CONVERSATION,
            "role": "user",
            "text": "DIRECT_USER_SENTINEL likes tea.",
            "version": 1,
            "deletion_epoch": 0,
            "deleted": False,
            "suppress_reuse": False,
            "sequence": 1,
            "run_id": FACT,
            "created_at": NOW.isoformat(),
            "source_refs": [],
        },
        ASSISTANT: {
            "id": FACT,
            "tenant_id": TENANT,
            "conversation_id": CONVERSATION,
            "role": "assistant",
            "text": "DERIVED_MEMORY_SENTINEL",
            "version": 1,
            "deletion_epoch": 0,
            "deleted": False,
            "suppress_reuse": False,
            "sequence": 2,
            "run_id": FACT,
            "created_at": NOW.isoformat(),
            "source_refs": [],
            "memory_records": [{"untrusted": "DERIVED_MEMORY_SENTINEL"}],
        },
    }
