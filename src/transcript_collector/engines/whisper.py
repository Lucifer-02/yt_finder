"""Transcribe locally stored audio into timestamped transcript segments."""

import ctypes
import logging
import site
from pathlib import Path
from time import perf_counter

from ..contracts import Segment
from ..errors import TranscriptionError

logger = logging.getLogger(__name__)


class WhisperEngine:
    name = "whisper"

    @property
    def config(self) -> str:
        return self.model_name

    def __init__(self, model_name="medium", device="auto"):
        self.transcript_language = "und"
        self.model_name = model_name
        self.device = device
        self.model = None

    def _device(self):
        if self.device == "cpu":
            return "cpu"
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() == 0:
                raise RuntimeError("No CUDA device detected")
            libraries = (
                "cublas/lib/libcublasLt.so.12",
                "cublas/lib/libcublas.so.12",
                "cudnn/lib/libcudnn.so.9",
            )
            for lib in libraries:
                path = next(
                    Path(root) / "nvidia" / lib
                    for root in site.getsitepackages()
                    if (Path(root) / "nvidia" / lib).exists()
                )
                ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
            return "cuda"
        except (OSError, ImportError, StopIteration, RuntimeError) as exc:
            if self.device == "cuda":
                raise TranscriptionError(
                    f"CUDA unavailable: {exc}. Install the gpu extra or choose --device cpu."
                ) from exc
            logger.info(
                "CUDA unavailable; selecting CPU", extra={"phase": "device_select"}
            )
            return "cpu"

    def transcript(self, audio: str | Path) -> list[Segment]:
        started = perf_counter()
        logger.info(
            "Whisper inference started; model=%s requested_device=%s",
            self.model_name,
            self.device,
            extra={"phase": "transcribe", "model": self.model_name},
        )
        try:
            from faster_whisper import WhisperModel

            if self.model is None:
                device = self._device()
                logger.info(
                    "Transcription device: %s; model: %s",
                    device,
                    self.model_name,
                    extra={"phase": "device_select", "model": self.model_name},
                )
                load_started = perf_counter()
                self.model = WhisperModel(
                    self.model_name,
                    device=device,
                    compute_type="int8_float16" if device == "cuda" else "int8",
                )
                logger.info(
                    "Whisper model loaded; model=%s device=%s elapsed=%.3fs",
                    self.model_name,
                    device,
                    perf_counter() - load_started,
                    extra={
                        "phase": "model_load",
                        "model": self.model_name,
                        "elapsed_seconds": perf_counter() - load_started,
                    },
                )
            else:
                logger.debug(
                    "Whisper model reused; model=%s",
                    self.model_name,
                    extra={"phase": "model_load", "model": self.model_name},
                )
            parts, info = self.model.transcribe(str(audio), vad_filter=True)
            logger.info(
                "Whisper language detected; language=%s",
                info.language,
                extra={"phase": "language_detect"},
            )
            segments = []
            last_progress = perf_counter()
            for part in parts:
                if part.text.strip():
                    segments.append(
                        Segment(part.text.strip(), part.start, part.end - part.start)
                    )
                now = perf_counter()
                if now - last_progress >= 5:
                    logger.info(
                        "Whisper inference progress; model=%s segments=%d audio_seconds=%.1f elapsed=%.3fs",
                        self.model_name,
                        len(segments),
                        part.end,
                        now - started,
                        extra={
                            "phase": "transcribe",
                            "model": self.model_name,
                            "segments": len(segments),
                            "elapsed_seconds": now - started,
                        },
                    )
                    last_progress = now
            if not segments:
                raise TranscriptionError("No speech detected in audio.")
            self.transcript_language = info.language
            logger.info(
                "Whisper inference finished; model=%s language=%s segments=%d elapsed=%.3fs",
                self.model_name,
                self.transcript_language,
                len(segments),
                perf_counter() - started,
                extra={
                    "phase": "transcribe",
                    "model": self.model_name,
                    "language": self.transcript_language,
                    "segments": len(segments),
                    "elapsed_seconds": perf_counter() - started,
                },
            )
            return segments
        except TranscriptionError:
            raise
        except Exception as exc:
            logger.error(
                "Whisper inference failed; model=%s reason=%s elapsed=%.3fs",
                self.model_name,
                type(exc).__name__,
                perf_counter() - started,
                extra={
                    "phase": "transcribe",
                    "model": self.model_name,
                    "elapsed_seconds": perf_counter() - started,
                },
            )
            raise TranscriptionError(f"Audio transcription failed: {exc}") from exc
