"""Bounded Firestore hierarchy discovery, including missing ancestor documents."""

from google.api_core.exceptions import GoogleAPICallError, RetryError
from google.cloud.firestore_v1 import Client

from brainoid_persistence.firestore import PersistenceUnavailable


class FirestorePurgePages:
    def __init__(self, client: Client) -> None:
        self.client = client

    def children(
        self, parent: str, *, collections: bool, token: str = "", limit: int = 50
    ) -> tuple[list[str], str]:
        if not 1 <= limit <= 100 or not parent or parent.startswith("/"):
            raise ValueError("Invalid hierarchy page")
        prefix = self.client._database_string + "/documents/"
        try:
            if collections:
                pager = self.client._firestore_api.list_collection_ids(
                    request={"parent": prefix + parent, "page_size": limit, "page_token": token},
                    metadata=self.client._rpc_metadata,
                    retry=None,
                    timeout=10,
                )
                page = next(pager.pages)
                return [parent + "/" + name for name in page.collection_ids], page.next_page_token
            owner, name = parent.rsplit("/", 1)
            documents = self.client._firestore_api.list_documents(
                request={
                    "parent": prefix + owner,
                    "collection_id": name,
                    "page_size": limit,
                    "page_token": token,
                    "show_missing": True,
                    "mask": {"field_paths": ["__name__"]},
                },
                metadata=self.client._rpc_metadata,
                retry=None,
                timeout=10,
            )
            document_page = next(documents.pages)
            return [d.name.removeprefix(prefix) for d in document_page.documents], (
                document_page.next_page_token
            )
        except (GoogleAPICallError, RetryError):
            raise PersistenceUnavailable() from None
