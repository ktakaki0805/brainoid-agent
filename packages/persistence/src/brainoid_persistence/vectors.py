"""Pinned official Firestore Vector/FindNearest adapter, with explicit index contract."""

from typing import cast

from brainoid_contracts import ResourceId, Scope
from brainoid_contracts.transactions import Document, Transaction
from brainoid_memory.models import Fact
from brainoid_memory.retrieval import Candidate
from brainoid_memory.vector_store import (
    EmbeddingConfig,
    VectorConfigurationError,
    VectorUnavailable,
    scope_key,
)
from google.api_core.exceptions import GoogleAPICallError, RetryError
from google.cloud.firestore_v1 import Client, FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector
from google.cloud.firestore_v1.vector_query import VectorQuery
from pydantic import ValidationError


def index_spec(config: EmbeddingConfig) -> Document:
    """Deployable Firebase index JSON, generated per explicitly chosen generation."""
    return {
        "indexes": [
            {
                "collectionGroup": config.collection,
                "queryScope": "COLLECTION",
                "fields": [
                    {"fieldPath": "scope_key", "order": "ASCENDING"},
                    {"fieldPath": "status", "order": "ASCENDING"},
                    {
                        "fieldPath": "embedding",
                        "vectorConfig": {"dimension": config.dimensions, "flat": {}},
                    },
                ],
            }
        ],
        "fieldOverrides": [
            {"collectionGroup": config.collection, "fieldPath": "embedding", "indexes": []}
        ],
    }


class FirestoreVectorIndex:
    def __init__(self, client: Client, config: EmbeddingConfig, *, manifest: Document) -> None:
        self.config = EmbeddingConfig.model_validate(config.model_dump())
        if manifest != index_spec(self.config):
            raise VectorConfigurationError()
        self.client = client

    def current(self, tx: Transaction, fact: Fact) -> bool:
        saved = tx.get(f"tenants/{fact.tenant_id}/{self.config.collection}/{fact.id}")
        scope = Scope(visibility=fact.visibility, display_policy=fact.display_policy)
        expected = {
            "id": fact.id,
            "tenant_id": fact.tenant_id,
            "version": fact.version,
            "deletion_epoch": fact.deletion_epoch,
            "scope_key": scope_key(
                fact.tenant_id, scope, fact.owner_user_id if fact.visibility == "private" else None
            ),
            "status": "ready",
            "configuration": self.config.model_dump(mode="json"),
        }
        if saved is None or any(saved.get(key) != value for key, value in expected.items()):
            return False
        embedding = saved.get("embedding")
        if not isinstance(embedding, Vector):
            return False
        try:
            self.config.vector(tuple(embedding))
        except VectorConfigurationError:
            return False
        return True

    def nearest(
        self, tenant: ResourceId, partition: str, vector: tuple[float, ...], *, limit: int
    ) -> tuple[Candidate, ...]:
        if not 1 <= limit <= 40:
            raise ValueError("Invalid vector limit")
        vector = self.config.vector(vector)
        collection = f"tenants/{tenant}/{self.config.collection}"
        query = (
            self.client.collection(collection)
            .where(filter=FieldFilter("scope_key", "==", partition))
            .where(filter=FieldFilter("status", "==", "ready"))
            .find_nearest("embedding", Vector(vector), limit, DistanceMeasure.EUCLIDEAN)
        )
        try:
            results = []
            for snapshot in cast(VectorQuery, query).stream(retry=None, timeout=10):
                value = snapshot.to_dict() or {}
                if (
                    snapshot.reference.path != f"{collection}/{value.get('id')}"
                    or value.get("tenant_id") != tenant
                    or value.get("scope_key") != partition
                    or value.get("status") != "ready"
                    or value.get("configuration") != self.config.model_dump(mode="json")
                ):
                    raise VectorConfigurationError()
                embedding = value.get("embedding")
                if not isinstance(embedding, Vector):
                    raise VectorConfigurationError()
                self.config.vector(tuple(embedding))
                results.append(
                    Candidate.model_validate(
                        {k: value.get(k) for k in ("id", "version", "deletion_epoch")}
                    )
                )
            return tuple(results)
        except ValidationError:
            raise VectorConfigurationError() from None
        except (GoogleAPICallError, RetryError):
            # Includes missing/mismatched deployed indexes; never empty/fallback.
            raise VectorUnavailable() from None

    def save(self, tx: Transaction, fact: Fact, vector: tuple[float, ...]) -> None:
        scope = Scope(visibility=fact.visibility, display_policy=fact.display_policy)
        tx.set(
            f"tenants/{fact.tenant_id}/{self.config.collection}/{fact.id}",
            {
                "id": fact.id,
                "tenant_id": fact.tenant_id,
                "version": fact.version,
                "deletion_epoch": fact.deletion_epoch,
                "scope_key": scope_key(
                    fact.tenant_id,
                    scope,
                    fact.owner_user_id if fact.visibility == "private" else None,
                ),
                "status": "ready",
                "configuration": self.config.model_dump(mode="json"),
                "embedding": Vector(self.config.vector(vector)),
            },
        )
