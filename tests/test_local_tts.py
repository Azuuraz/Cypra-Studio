from __future__ import annotations

import io
from pathlib import Path
import threading
import time
import tempfile
import unittest
import wave

from tts.sanitizer import sanitize_for_speech
import tts.service as service_module
from tts.service import LocalTTSService, TTSCancelled
from engine.vault import DEFAULT_SETTINGS, load_settings, save_settings


def _tiny_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        wav_file.writeframes(b"\x00\x00" * 80)
    return output.getvalue()


class LocalTTSTests(unittest.TestCase):
    def test_sanitizer_skips_code_urls_and_metadata(self) -> None:
        raw = """# Result
Speak this sentence.
```python
print('never speak this')
```
See https://example.com/private?q=1
tokens: 999 at 42 tok/s
Finish here.
"""
        spoken = sanitize_for_speech(raw, maximum=1000, skip_code=True, skip_urls=True)
        self.assertIn("Speak this sentence", spoken)
        self.assertIn("Finish here", spoken)
        self.assertNotIn("print", spoken)
        self.assertNotIn("https", spoken)
        self.assertNotIn("999", spoken)

    def test_sanitizer_truncates_at_sentence_boundary(self) -> None:
        spoken = sanitize_for_speech("First sentence. Second sentence is too long to retain.", maximum=20)
        self.assertEqual(spoken, "First sentence.")

    def test_edge_privacy_hardening_removes_private_material(self) -> None:
        raw = """Normal prose remains.
Internal: hidden directive
Tool call: secret output
Path: C:\\Users\\person\\private.txt
https://example.com/secret
```python
print('hidden')
```
"""
        spoken = sanitize_for_speech(raw, privacy_harden=True)
        self.assertIn("Normal prose remains", spoken)
        self.assertNotIn("directive", spoken)
        self.assertNotIn("Tool call", spoken)
        self.assertNotIn("Users", spoken)
        self.assertNotIn("https", spoken)
        self.assertNotIn("print", spoken)

    def test_stop_cancels_active_sentence_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LocalTTSService(Path(temp_dir))
            began = threading.Event()

            class SlowCPUVoice:
                active_providers = ["CPUExecutionProvider"]

                def synthesize(self, _text: str, *, speed: float = 1.0) -> bytes:
                    began.set()
                    time.sleep(0.12)
                    return _tiny_wav()

                def close(self) -> None:
                    return None

            service._engine_for = lambda _voice, _threads: SlowCPUVoice()  # type: ignore[method-assign]
            error: list[BaseException] = []

            def request() -> None:
                try:
                    service.synthesize("One sentence. Two sentence. Three sentence.", replace=False)
                except BaseException as exc:
                    error.append(exc)

            thread = threading.Thread(target=request)
            thread.start()
            self.assertTrue(began.wait(timeout=2))
            service.cancel(clear_queue=True)
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(error and isinstance(error[0], TTSCancelled))

    def test_service_queue_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LocalTTSService(Path(temp_dir))
            self.assertEqual(service.MAX_QUEUE, 3)
            self.assertEqual(service.status()["queue_limit"], 3)

    def test_edge_blocked_never_connects_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LocalTTSService(Path(temp_dir))
            called = []

            class FakeCPUVoice:
                active_providers = ["CPUExecutionProvider"]
                threads = 2
                def synthesize(self, _text: str, *, speed: float = 1.0) -> bytes:
                    called.append("piper")
                    return _tiny_wav()
                def close(self) -> None: return None

            class ForbiddenEdge:
                def __init__(self) -> None:
                    raise AssertionError("Edge connection attempted while blocked")

            original = service_module.EdgeEngine
            service_module.EdgeEngine = ForbiddenEdge  # type: ignore[assignment]
            service._engine_for = lambda _voice, _threads: FakeCPUVoice()  # type: ignore[method-assign]
            try:
                result = service.synthesize_result(
                    "Blocked online speech.", provider="edge", voice="en-US-AvaNeural",
                    online_allowed=False, fallback="piper", replace=False,
                )
            finally:
                service_module.EdgeEngine = original
            self.assertEqual(result.provider, "local")
            self.assertEqual(called, ["piper"])

    def test_edge_failure_falls_back_to_piper(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LocalTTSService(Path(temp_dir))

            class FakeCPUVoice:
                active_providers = ["CPUExecutionProvider"]
                threads = 2
                def synthesize(self, _text: str, *, speed: float = 1.0) -> bytes: return _tiny_wav()
                def close(self) -> None: return None

            class FailedEdge:
                async def synthesize(self, *_args, **_kwargs) -> bytes:
                    raise RuntimeError("simulated network loss")

            original = service_module.EdgeEngine
            service_module.EdgeEngine = FailedEdge  # type: ignore[assignment]
            service._engine_for = lambda _voice, _threads: FakeCPUVoice()  # type: ignore[method-assign]
            try:
                result = service.synthesize_result(
                    "Network failure test.", provider="edge", voice="en-US-AvaNeural",
                    online_allowed=True, fallback="piper", replace=False,
                )
            finally:
                service_module.EdgeEngine = original
            self.assertEqual(result.provider, "local")

    def test_phase_two_defaults_are_private_and_local(self) -> None:
        self.assertFalse(DEFAULT_SETTINGS["voice_output_enabled"])
        self.assertEqual(DEFAULT_SETTINGS["tts_provider"], "local")
        self.assertFalse(DEFAULT_SETTINGS["tts_allow_online"])
        self.assertEqual(DEFAULT_SETTINGS["tts_online_fallback"], "piper")

    def test_voice_settings_persist_with_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.json"
            settings = dict(DEFAULT_SETTINGS)
            settings.update({
                "voice_output_enabled": True,
                "tts_provider": "local",
                "tts_cpu_threads": 2,
                "tts_max_chars": 1200,
            })
            save_settings(settings_path, settings)
            loaded = load_settings(settings_path)
            self.assertTrue(loaded["voice_output_enabled"])
            self.assertEqual(loaded["tts_provider"], "local")
            self.assertEqual(loaded["tts_cpu_threads"], 2)
            self.assertEqual(loaded["tts_max_chars"], 1200)


if __name__ == "__main__":
    unittest.main()
