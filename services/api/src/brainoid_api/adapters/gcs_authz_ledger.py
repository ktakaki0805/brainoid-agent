"""Official Storage SDK transport; no implicit project, bucket, or credential lookup."""

from brainoid_policy.control_ledger import LedgerError
from google.api_core.exceptions import GoogleAPICallError, NotFound, PreconditionFailed, RetryError
from google.cloud.storage import Bucket  # type: ignore[import-untyped]
from google.cloud.storage.exceptions import DataCorruption  # type: ignore[import-untyped]
from google.cloud.storage.retry import (  # type: ignore[import-untyped]
    DEFAULT_RETRY,
    DEFAULT_RETRY_IF_GENERATION_SPECIFIED,
    ConditionalRetryPolicy,
)
from requests.exceptions import RequestException

_FAILURES = (GoogleAPICallError, RetryError, DataCorruption, RequestException)
_CREATE_RETRY = ConditionalRetryPolicy(
    DEFAULT_RETRY.with_timeout(20),
    DEFAULT_RETRY_IF_GENERATION_SPECIFIED.conditional_predicate,
    DEFAULT_RETRY_IF_GENERATION_SPECIFIED.required_kwargs,
)


class GCSAuthzObjects:
    """The injected bucket must be independent of every restorable database.

    API credentials need object create/read/list, never overwrite/delete. Retention
    and IAM are deployed separately by OPS-02. SDK retries retain create-only
    generation=0; a 412 is resolved by comparing the immutable record in the core.
    """

    def __init__(self, bucket: Bucket) -> None:
        self._bucket = bucket

    def read(self, name: str) -> bytes | None:
        try:
            data = self._bucket.blob(name).download_as_bytes(
                timeout=10, retry=DEFAULT_RETRY.with_timeout(20)
            )
            if not isinstance(data, bytes):
                raise LedgerError()
            return data
        except NotFound:
            return None
        except _FAILURES:
            raise LedgerError() from None

    def create(self, name: str, data: bytes) -> bool:
        try:
            self._bucket.blob(name).upload_from_string(
                data,
                content_type="application/json",
                if_generation_match=0,
                timeout=10,
                retry=_CREATE_RETRY,
                checksum="crc32c",
            )
            return True
        except PreconditionFailed:
            return False
        except _FAILURES:
            # Includes an unknown write outcome. No replacement ID, no false success.
            raise LedgerError() from None

    def names(self, prefix: str) -> list[str]:
        try:
            return [
                blob.name
                for blob in self._bucket.list_blobs(
                    prefix=prefix, timeout=10, retry=DEFAULT_RETRY.with_timeout(20)
                )
            ]
        except _FAILURES:
            raise LedgerError() from None
