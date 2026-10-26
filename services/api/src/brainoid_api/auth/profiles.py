"""Read the authenticated user's persisted global profile, with no implicit provisioning."""

from typing import Protocol

from brainoid_contracts import FirebaseUid
from brainoid_contracts.accounts import UserProfile as UserProfile
from brainoid_contracts.deletion import lifecycle_account_path
from google.api_core.exceptions import GoogleAPICallError, RetryError
from google.cloud.firestore_v1 import Client, FieldFilter
from pydantic import ValidationError

from brainoid_api.errors import APIError, ErrorCode


class ProfileReader(Protocol):
    def get(self, user_id: FirebaseUid) -> UserProfile: ...


class FirestoreProfiles:
    def __init__(self, client: Client) -> None:
        self._client = client

    def get(self, user_id: FirebaseUid) -> UserProfile:
        try:
            if self._client.document(lifecycle_account_path(user_id)).get(timeout=10).exists:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            # UIDs are opaque strings, including '/'. Never concatenate them into a path.
            # Equality lookup preserves legacy users/{uid} fixture documents and works
            # with safely assigned document keys for future profile provisioning.
            snapshots = list(
                self._client.collection("users")
                .where(filter=FieldFilter("user_id", "==", user_id))
                .limit(2)
                .stream(timeout=10)
            )
        except (GoogleAPICallError, RetryError):
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
        if not snapshots:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        if len(snapshots) != 1:
            raise APIError(ErrorCode.INTERNAL_ERROR)
        document = snapshots[0].to_dict() or {}
        try:
            profile = UserProfile.model_validate(
                {field: document.get(field) for field in UserProfile.model_fields}
            )
        except ValidationError:
            raise APIError(ErrorCode.INTERNAL_ERROR) from None
        if profile.user_id != user_id:
            raise APIError(ErrorCode.INTERNAL_ERROR)
        # Deleting accounts will get a separate status-only endpoint in BA-IAM-10.
        if profile.status != "active":
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        return profile
