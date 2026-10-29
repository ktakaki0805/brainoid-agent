"""Write-only, deterministic sentence references in the text event transaction."""

from datetime import datetime, timedelta
from uuid import UUID, uuid5

from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Document, Transaction
from brainoid_policy.scopes import PolicyDenied

LIFETIME = timedelta(minutes=5)
MAX_SENTENCES = 64
MAX_SENTENCE_BYTES = 4000
PUNCTUATION = frozenset("。！？!?….「」『』“”\"'()（）[]【】")


def speech_path(tenant: ResourceId, speech: ResourceId) -> str:
    return f"tenants/{tenant}/speech_outputs/{speech}"


def sentence_ends(text: str, *, final: bool) -> list[int]:
    """Japanese terminal punctuation/newlines; ASCII dots require following whitespace.

    Offsets are code points in append-only public text. No provider rewrite or
    token boundary can retract a published sentence. A terminal remainder is
    published only by the successful final-message transaction.
    """
    ends = [
        i + 1
        for i, char in enumerate(text)
        if char in "。！？!?\n" or (char == "." and i + 1 < len(text) and text[i + 1].isspace())
    ]
    if final and text and (not ends or ends[-1] != len(text)):
        ends.append(len(text))
    return ends


def append_sentences(tx: Transaction, run: Document, now: datetime, *, final: bool) -> None:
    if run.get("output_mode") != "speech":
        return
    from brainoid_runs.events import Speech, SpeechPayload, append

    text = run.get("streamed_text", "")
    offset, index = run.get("speech_offset", 0), run.get("speech_count", 0)
    tail = text[offset:]
    for end in sentence_ends(tail, final=final):
        absolute = run.get("speech_offset", 0) + end
        sentence = text[offset:absolute]
        # A token may contain only the closing quote or repeated punctuation of
        # an already published sentence. Preserve it in text, never bill a new
        # synthesis for it. Emoji and other non-punctuation content remain valid.
        if not any(not char.isspace() and char not in PUNCTUATION for char in sentence):
            offset = absolute
            continue
        if index >= MAX_SENTENCES or len(sentence.encode("utf-8")) > MAX_SENTENCE_BYTES:
            raise PolicyDenied()
        identity = ResourceId(str(uuid5(UUID(run["run_id"]), f"speech:{index}")))
        expiry = now + LIFETIME
        tx.create(
            speech_path(ResourceId(run["tenant_id"]), identity),
            {
                "id": identity,
                "tenant_id": run["tenant_id"],
                "run_id": run["run_id"],
                "conversation_id": run["conversation_id"],
                "owner_user_id": run["speech_owner_user_id"],
                "visibility": run["speech_scope"]["visibility"],
                "display_policy": run["speech_scope"]["display_policy"],
                "sentence_index": index,
                "start": offset,
                "end": absolute,
                "created_at": now.isoformat(),
                "expires_at": expiry.isoformat(),
                "ttl_at": expiry,
                "purge_epoch": run["purge_epoch"],
                "deleted": False,
                "deletion_epoch": 0,
                "attempt_count": 0,
            },
        )
        append(
            tx,
            run,
            Speech(
                run_id=run["run_id"],
                seq=run.get("last_event_seq", 0) + 1,
                occurred_at=now,
                payload=SpeechPayload(speech_id=identity, sentence_index=index, expires_at=expiry),
            ),
        )
        index += 1
        offset = absolute
    run["speech_offset"], run["speech_count"] = offset, index
