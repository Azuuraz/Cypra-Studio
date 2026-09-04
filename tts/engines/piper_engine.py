"""Piper adapter with an explicit CPU-only execution contract."""

from __future__ import annotations

from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import wave


class PiperUnavailable(RuntimeError):
    """Piper or a project-local voice is not available."""


@contextmanager
def _temporary_thread_limits(threads: int):
    limits = {"OMP_NUM_THREADS": str(threads), "MKL_NUM_THREADS": str(threads)}
    previous = {key: os.environ.get(key) for key in limits}
    try:
        os.environ.update(limits)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class PiperEngine:
    name = "piper"
    device = "CPU"

    def __init__(self, voices_dir: Path, *, voice: str, threads: int = 2) -> None:
        self.voices_dir = voices_dir.resolve()
        self.voice_name = self._safe_voice_name(voice)
        self.threads = max(1, min(4, int(threads)))
        self._voice = None
        self.active_providers: list[str] = []

    @staticmethod
    def _safe_voice_name(value: str) -> str:
        name = str(value or "en_US-lessac-medium").strip()
        if not name or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in name):
            raise PiperUnavailable("Invalid local voice name")
        return name

    @classmethod
    def installed_voices(cls, voices_dir: Path) -> list[str]:
        root = voices_dir.resolve()
        if not root.exists():
            return []
        voices: list[str] = []
        for model in root.glob("*.onnx"):
            try:
                resolved = model.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if model.with_suffix(model.suffix + ".json").is_file():
                voices.append(model.stem)
        return sorted(set(voices))

    def _model_paths(self) -> tuple[Path, Path]:
        model = (self.voices_dir / f"{self.voice_name}.onnx").resolve()
        config = (self.voices_dir / f"{self.voice_name}.onnx.json").resolve()
        try:
            model.relative_to(self.voices_dir)
            config.relative_to(self.voices_dir)
        except ValueError as exc:
            raise PiperUnavailable("Voice path escaped the project voice directory") from exc
        if not model.is_file() or not config.is_file():
            raise PiperUnavailable(
                f"Local voice '{self.voice_name}' is not installed in {self.voices_dir}"
            )
        return model, config

    def load(self) -> None:
        if self._voice is not None:
            return
        model, config = self._model_paths()
        try:
            with _temporary_thread_limits(self.threads):
                import onnxruntime
                from piper import PiperVoice
                from piper.config import PiperConfig
                from piper.voice import ESPEAK_DATA_DIR

                with config.open("r", encoding="utf-8") as config_file:
                    voice_config = PiperConfig.from_dict(json.load(config_file))
                session_options = onnxruntime.SessionOptions()
                session_options.intra_op_num_threads = self.threads
                session_options.inter_op_num_threads = 1
                session = onnxruntime.InferenceSession(
                    str(model),
                    sess_options=session_options,
                    providers=["CPUExecutionProvider"],
                )
                voice = PiperVoice(
                    config=voice_config,
                    session=session,
                    espeak_data_dir=Path(ESPEAK_DATA_DIR),
                    download_dir=self.voices_dir,
                )
        except ImportError as exc:
            raise PiperUnavailable("piper-tts is not installed") from exc
        except Exception as exc:
            raise PiperUnavailable(f"Piper voice failed to load: {exc}") from exc

        session = getattr(voice, "session", None)
        providers = list(session.get_providers()) if session and hasattr(session, "get_providers") else []
        unsafe = ("CUDA", "TENSORRT", "ROCM", "DML")
        if any(any(marker in provider.upper() for marker in unsafe) for provider in providers):
            raise PiperUnavailable(f"GPU execution provider unexpectedly active: {providers}")
        if providers and not any("CPU" in provider.upper() for provider in providers):
            raise PiperUnavailable(f"CPU execution provider is not active: {providers}")
        self.active_providers = providers or ["CPU (Piper use_cuda=False)"]
        self._voice = voice

    def synthesize(self, text: str, *, speed: float = 1.0) -> bytes:
        self.load()
        speed = max(0.5, min(2.0, float(speed)))
        try:
            from piper.config import SynthesisConfig
        except ImportError:
            try:
                from piper import SynthesisConfig
            except ImportError:
                SynthesisConfig = None  # type: ignore[assignment,misc]

        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            kwargs = {}
            if SynthesisConfig is not None:
                kwargs["syn_config"] = SynthesisConfig(length_scale=1.0 / speed)
            self._voice.synthesize_wav(text, wav_file, **kwargs)
        return output.getvalue()

    def close(self) -> None:
        self._voice = None
        self.active_providers = []
