"""Audio transcription engine selection, with lazy optional dependencies."""

from ..contracts import TranscriptionEngine
from ..errors import TranscriptionError as TranscriptionError

ENGINE_NAMES = ("whisper",)


def create_engine(name: str, model: str, device: str) -> TranscriptionEngine:
    if name == "whisper":
        import importlib.util

        if importlib.util.find_spec("faster_whisper") is None:
            raise TranscriptionError(
                "Install the whisper extra for audio transcription."
            )
        from .whisper import WhisperEngine

        return WhisperEngine(model, device)
    raise ValueError(f"Unknown transcription engine: {name}")
