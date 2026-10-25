"""Speech conversion, without device playback, microphone, or SDK objects."""

from typing import Annotated, Literal

from pydantic import Field

from brainoid_agent_core.types.common import CoreValue, NonEmptyText, TerminalEvent


class AudioFormat(CoreValue):
    encoding: Literal["pcm_s16le", "wav", "opus"]
    sample_rate_hz: Annotated[int, Field(strict=True, gt=0)]
    channels: Literal[1, 2] = 1


class SpeechRecognition(CoreValue):
    audio: bytes
    format: AudioFormat
    locale: NonEmptyText


class SpeechSynthesis(CoreValue):
    text: NonEmptyText
    format: AudioFormat
    locale: NonEmptyText
    voice: NonEmptyText


class Transcript(CoreValue):
    type: Literal["transcript"] = "transcript"
    text: str
    is_final: bool


class AudioChunk(CoreValue):
    type: Literal["audio_chunk"] = "audio_chunk"
    audio: bytes
    format: AudioFormat


type RecognitionEvent = Annotated[Transcript | TerminalEvent, Field(discriminator="type")]
type SynthesisEvent = Annotated[AudioChunk | TerminalEvent, Field(discriminator="type")]
