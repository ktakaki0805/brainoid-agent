"""A bounded run-local sentence queue. Only live speech.ready events enter it."""

from dataclasses import dataclass

from brainoid_contracts import ResourceId

from brainoid_device.cloud_models import CloudError


@dataclass(frozen=True)
class Sentence:
    identity: ResourceId
    index: int
    expires: float


class PlaybackQueue:
    def __init__(self) -> None:
        self.pending: dict[int, Sentence] = {}
        self.seen: dict[int, ResourceId] = {}
        self.next_index = 0

    def clear(self) -> None:
        self.pending.clear()
        self.seen.clear()
        self.next_index = 0

    def add(self, sentence: Sentence, now: float) -> None:
        if not 0 <= sentence.index < 64 or sentence.expires <= now:
            raise CloudError("audio_expired_or_invalid", 410)
        old = self.seen.get(sentence.index)
        if old is not None:
            if old != sentence.identity:
                raise CloudError("audio_sequence_invalid", 409)
            return
        if sentence.identity in self.seen.values():
            raise CloudError("audio_sequence_invalid", 409)
        self.seen[sentence.index] = sentence.identity
        self.pending[sentence.index] = sentence

    def take(self, now: float) -> Sentence | None:
        if any(item.expires <= now for item in self.pending.values()):
            raise CloudError("audio_expired", 410)
        result = self.pending.pop(self.next_index, None)
        if result:
            self.next_index += 1
        return result
