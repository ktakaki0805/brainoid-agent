"""Fail-closed model reuse of canonical text, independent of UI history reads."""

import hashlib

from brainoid_contracts.transactions import Document


def direct_user(document: Document) -> bool:
    return (
        document.get("role") == "user"
        and document.get("source_refs", []) == []
        and not any(
            key in document
            for key in ("memory_records", "tool_bindings", "model_message", "model_messages")
        )
    )


def reusable_text(document: Document) -> str | None:
    """Only offsets bound to this exact version AND text can safely redact a span.

    Unknown/malformed suppression excludes the entire message. Suppression markers
    never expire, and no caller may reconstruct missing text from a derived record.
    """
    text = document.get("text")
    if (
        not isinstance(text, str)
        or document.get("deleted") is not False
        or document.get("suppress_reuse") is not False
        or type(document.get("deletion_epoch")) is not int
        or document["deletion_epoch"] != 0
    ):
        return None
    spans = document.get("suppressed_spans", [])
    if not isinstance(spans, list):
        return None
    ranges = []
    for span in spans:
        if (
            not isinstance(span, dict)
            or set(span) != {"start", "end", "version", "text_sha256"}
            or type(span["start"]) is not int
            or type(span["end"]) is not int
            or not 0 <= span["start"] < span["end"] <= len(text)
            or type(span["version"]) is not int
            or span["version"] != document.get("version")
            or span["text_sha256"] != hashlib.sha256(text.encode()).hexdigest()
        ):
            return None
        ranges.append((span["start"], span["end"]))
    cursor = 0
    parts = []
    for start, end in sorted(ranges):
        if start > cursor:
            parts.append(text[cursor:start])
        parts.append("[omitted]")
        cursor = max(cursor, end)
    parts.append(text[cursor:])
    return "".join(parts)
