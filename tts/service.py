"""One lazy, bounded CPU synthesis worker for all Matrix voices."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
import asyncio
import io
import logging
from pathlib import Path
import queue
import re
import threading
import wave

from .engines.piper_engine import PiperEngine
from .engines.edge_engine import EdgeEngine, EdgeUnavailable
from .sanitizer import sanitize_for_online_tts, sanitize_for_speech


log = logging.getLogger("cypra.tts")


class TTSCancelled(RuntimeError):
    pass


@dataclass
class SynthesisResult:
    audio: bytes
    media_type: str
    provider: str


@dataclass
class _SpeechRequest:
    text: str
    voice: str
    speed: float
    threads: int
    generation: int
    provider: str
    fallback: str
    fallback_voice: str
    online_allowed: bool
    online_sanitizer_failed: bool
    future: Future[SynthesisResult]


def _set_worker_low_priority() -> None:
    """Lower only this worker thread on Windows; never lower the Matrix process."""
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetCurrentThread()
        ctypes.windll.kernel32.SetThreadPriority(handle, -1)
    except Exception:
        pass


class LocalTTSService:
    MAX_QUEUE = 3

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.voices_dir = (self.project_root / "MatrixFiles" / "Voice" / "Piper").resolve()
        self.voices_dir.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[_SpeechRequest | None] = queue.Queue(maxsize=self.MAX_QUEUE)
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._engine: PiperEngine | None = None
        self._engine_key: tuple[str, int] | None = None
        self._generation = 0
        self._active = False
        self._release_pending = False
        self._last_error = ""
        self._active_provider = ""
        self._edge_loop: asyncio.AbstractEventLoop | None = None
        self._edge_task: asyncio.Task[bytes] | None = None
        self._edge_voices: list[dict] | None = None

    def installed_voices(self) -> list[str]:
        return PiperEngine.installed_voices(self.voices_dir)

    def status(self) -> dict[str, object]:
        voices = self.installed_voices()
        with self._lock:
            return {
                "engine": "piper",
                "device": "CPU",
                "lazy": self._engine is None,
                "ready": bool(voices),
                "dependency": self._piper_available(),
                "voices": voices,
                "voices_dir": str(self.voices_dir.relative_to(self.project_root)),
                "queue_length": self._queue.qsize(),
                "queue_limit": self.MAX_QUEUE,
                "active": self._active,
                "active_provider": self._active_provider or None,
                "edge_dependency": self._edge_available(),
                "edge_voice_cache": len(self._edge_voices or []),
                "providers": list(self._engine.active_providers) if self._engine else [],
                "threads": self._engine.threads if self._engine else None,
                "last_error": self._last_error or None,
            }

    @staticmethod
    def _piper_available() -> bool:
        try:
            import importlib.util

            return importlib.util.find_spec("piper") is not None
        except Exception:
            return False

    @staticmethod
    def _edge_available() -> bool:
        try:
            import importlib.util

            return importlib.util.find_spec("edge_tts") is not None
        except Exception:
            return False

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run, name="cypra-tts-cpu", daemon=True)
            self._worker.start()

    def synthesize(
        self,
        text: str,
        *,
        voice: str = "en_US-lessac-medium",
        speed: float = 1.0,
        threads: int = 2,
        maximum: int = 1000,
        skip_code: bool = True,
        skip_urls: bool = True,
        replace: bool = True,
        timeout: float = 75.0,
    ) -> bytes:
        return self.synthesize_result(
            text,
            voice=voice,
            speed=speed,
            threads=threads,
            maximum=maximum,
            skip_code=skip_code,
            skip_urls=skip_urls,
            replace=replace,
            timeout=timeout,
            provider="local",
        ).audio

    def synthesize_result(
        self,
        text: str,
        *,
        provider: str = "local",
        voice: str = "en_US-lessac-medium",
        speed: float = 1.0,
        threads: int = 2,
        maximum: int = 1000,
        skip_code: bool = True,
        skip_urls: bool = True,
        replace: bool = True,
        timeout: float = 75.0,
        online_allowed: bool = False,
        fallback: str = "piper",
        fallback_voice: str = "en_US-lessac-medium",
    ) -> SynthesisResult:
        provider = str(provider or "local").strip().lower()
        if provider not in ("local", "edge"):
            raise ValueError("Unsupported centralized TTS provider")
        source_text = text
        online_sanitizer_failed = False
        if provider == "edge":
            try:
                # First pass preserves original token/key boundaries before the
                # general speech sanitizer flattens markdown and underscores.
                source_text = sanitize_for_online_tts(text)
            except Exception:
                # Fail closed for Edge. The request may still use the configured
                # local Piper fallback, but original text is never sent online.
                online_sanitizer_failed = True
                source_text = text
                log.warning("[TTS] Online sanitizer failed; Edge request blocked")
        spoken = sanitize_for_speech(
            source_text,
            maximum=maximum,
            skip_code=skip_code,
            skip_urls=skip_urls,
            privacy_harden=provider == "edge",
        )
        if not spoken:
            raise ValueError("Nothing speakable remains after sanitization")
        if replace:
            self.cancel(clear_queue=True)
        with self._lock:
            generation = self._generation
        future: Future[SynthesisResult] = Future()
        request = _SpeechRequest(
            text=spoken,
            voice=voice,
            speed=max(0.5, min(2.0, float(speed))),
            threads=max(1, min(4, int(threads))),
            generation=generation,
            provider=provider,
            fallback="piper" if str(fallback).lower() == "piper" else "none",
            fallback_voice=str(fallback_voice or "en_US-lessac-medium"),
            online_allowed=bool(online_allowed),
            online_sanitizer_failed=online_sanitizer_failed,
            future=future,
        )
        with self._lock:
            self._release_pending = False
        self._ensure_worker()
        try:
            self._queue.put_nowait(request)
        except queue.Full:
            self._drop_oldest()
            try:
                self._queue.put_nowait(request)
            except queue.Full as exc:
                raise RuntimeError("TTS queue is busy; stale speech was dropped") from exc
        return future.result(timeout=timeout)

    def _drop_oldest(self) -> None:
        try:
            stale = self._queue.get_nowait()
        except queue.Empty:
            return
        if stale is not None and not stale.future.done():
            stale.future.set_exception(TTSCancelled("Stale speech dropped"))
        self._queue.task_done()

    def cancel(self, *, clear_queue: bool = True, release: bool = False) -> None:
        with self._lock:
            self._generation += 1
            self._release_pending = self._release_pending or release
            edge_loop = self._edge_loop
            edge_task = self._edge_task
        if edge_loop and edge_task and not edge_task.done():
            try:
                edge_loop.call_soon_threadsafe(edge_task.cancel)
            except Exception:
                pass
        if clear_queue:
            while True:
                try:
                    stale = self._queue.get_nowait()
                except queue.Empty:
                    break
                if stale is not None and not stale.future.done():
                    stale.future.set_exception(TTSCancelled("Speech stopped"))
                self._queue.task_done()
        if release:
            with self._lock:
                if not self._active and self._engine:
                    self._engine.close()
                    self._engine = None
                    self._engine_key = None
                    self._release_pending = False

    def _cancelled(self, generation: int) -> bool:
        with self._lock:
            return generation != self._generation

    def _engine_for(self, voice: str, threads: int) -> PiperEngine:
        key = (voice, threads)
        if self._engine is None or self._engine_key != key:
            if self._engine:
                self._engine.close()
            self._engine = PiperEngine(self.voices_dir, voice=voice, threads=threads)
            self._engine_key = key
            log.info("[TTS] provider=local")
            log.info("[TTS] engine=piper")
            log.info("[TTS] device=CPU")
            log.info("[TTS] threads=%s", threads)
            self._engine.load()
            log.info("[TTS] ready")
        return self._engine

    def edge_voices(self) -> list[dict[str, str]]:
        """Discover Edge voices lazily; callers must enforce the online permission gate."""
        with self._lock:
            if self._edge_voices is not None:
                return list(self._edge_voices)
        raw = asyncio.run(EdgeEngine.list_voices())
        voices = [
            {
                "short_name": str(item.get("ShortName") or ""),
                "locale": str(item.get("Locale") or ""),
                "gender": str(item.get("Gender") or ""),
                "friendly_name": str(item.get("FriendlyName") or item.get("ShortName") or ""),
            }
            for item in raw
            if item.get("ShortName")
        ]
        with self._lock:
            self._edge_voices = voices
        return list(voices)

    def _piper_synthesis(self, request: _SpeechRequest, *, fallback: bool = False) -> SynthesisResult:
        voice = request.fallback_voice if fallback else request.voice
        engine = self._engine_for(voice, request.threads)
        audio_chunks: list[bytes] = []
        for sentence in self._sentences(request.text):
            if self._cancelled(request.generation):
                raise TTSCancelled("Speech stopped")
            audio_chunks.append(engine.synthesize(sentence, speed=request.speed))
        return SynthesisResult(self._join_waves(audio_chunks), "audio/wav", "local")

    def _edge_synthesis(self, request: _SpeechRequest) -> SynthesisResult:
        if request.online_sanitizer_failed:
            if request.fallback == "piper":
                return self._piper_synthesis(request, fallback=True)
            raise EdgeUnavailable("Online sanitizer failed; Edge request blocked")
        if not request.online_allowed:
            log.warning("[TTS] Edge blocked: online TTS disabled")
            if request.fallback == "piper":
                return self._piper_synthesis(request, fallback=True)
            raise EdgeUnavailable("Online TTS is disabled")
        try:
            # Final fail-closed boundary immediately before Edge. Edge receives
            # only this plain, twice-sanitized string and never a Matrix object.
            edge_text = sanitize_for_online_tts(request.text)
        except Exception as exc:
            log.warning("[TTS] Online sanitizer failed; Edge request blocked")
            if request.fallback == "piper":
                return self._piper_synthesis(request, fallback=True)
            raise EdgeUnavailable("Online sanitizer failed; Edge request blocked") from exc
        if not edge_text:
            log.warning("[TTS] Edge request blocked: no safe speech remains")
            if request.fallback == "piper":
                return self._piper_synthesis(request, fallback=True)
            raise EdgeUnavailable("No safe online speech remains")
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            task = loop.create_task(
                EdgeEngine().synthesize(
                    edge_text,
                    voice=request.voice or "en-US-AvaNeural",
                    speed=request.speed,
                    cancelled=lambda: self._cancelled(request.generation),
                )
            )
            with self._lock:
                self._edge_loop = loop
                self._edge_task = task
            audio = loop.run_until_complete(task)
            return SynthesisResult(audio, "audio/mpeg", "edge")
        finally:
            with self._lock:
                self._edge_task = None
                self._edge_loop = None
            asyncio.set_event_loop(None)
            loop.close()

    @staticmethod
    def _sentences(text: str) -> list[str]:
        parts = [part.strip() for part in re.findall(r"[^.!?]+(?:[.!?]+|$)", text) if part.strip()]
        return parts or [text]

    @staticmethod
    def _join_waves(chunks: list[bytes]) -> bytes:
        if not chunks:
            raise ValueError("No synthesized audio")
        output = io.BytesIO()
        params = None
        frames: list[bytes] = []
        for chunk in chunks:
            with wave.open(io.BytesIO(chunk), "rb") as wav_file:
                current = wav_file.getparams()
                signature = (current.nchannels, current.sampwidth, current.framerate, current.comptype)
                if params is None:
                    params = current
                else:
                    original = (params.nchannels, params.sampwidth, params.framerate, params.comptype)
                    if signature != original:
                        raise RuntimeError("Piper returned incompatible audio chunks")
                frames.append(wav_file.readframes(current.nframes))
        with wave.open(output, "wb") as joined:
            joined.setparams(params)
            for frame in frames:
                joined.writeframes(frame)
        return output.getvalue()

    def _run(self) -> None:
        _set_worker_low_priority()
        while True:
            request = self._queue.get()
            if request is None:
                self._queue.task_done()
                return
            try:
                if self._cancelled(request.generation):
                    raise TTSCancelled("Speech stopped")
                with self._lock:
                    self._active = True
                    self._active_provider = request.provider
                log.info("[TTS] synthesize chars=%s", len(request.text))
                if request.provider == "edge":
                    log.info("[TTS] provider=edge")
                    log.info("[TTS] online=%s", str(request.online_allowed).lower())
                    log.info("[TTS] voice=%s", request.voice)
                    try:
                        result = self._edge_synthesis(request)
                    except (TTSCancelled, asyncio.CancelledError):
                        raise TTSCancelled("Speech stopped")
                    except Exception:
                        if request.online_allowed and request.fallback == "piper":
                            log.warning("[TTS] Edge unavailable; falling back to Piper Local")
                            result = self._piper_synthesis(request, fallback=True)
                        else:
                            raise
                else:
                    result = self._piper_synthesis(request)
                if self._cancelled(request.generation):
                    raise TTSCancelled("Speech stopped")
                if not request.future.done():
                    request.future.set_result(result)
                self._last_error = ""
            except Exception as exc:
                if not isinstance(exc, TTSCancelled):
                    if request.provider == "edge":
                        # Never persist/log remote exception text because third-party
                        # errors are not guaranteed to exclude request details.
                        self._last_error = type(exc).__name__
                        log.warning("[TTS] Edge request failed: %s", type(exc).__name__)
                    else:
                        self._last_error = str(exc)
                        log.warning("[TTS] request failed: %s", exc)
                if not request.future.done():
                    request.future.set_exception(exc)
            finally:
                with self._lock:
                    self._active = False
                    self._active_provider = ""
                    if self._release_pending and self._engine:
                        self._engine.close()
                        self._engine = None
                        self._engine_key = None
                        self._release_pending = False
                self._queue.task_done()
