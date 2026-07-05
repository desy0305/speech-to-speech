from __future__ import annotations

import base64
import io
import json
import wave
from queue import Queue
from threading import Event
from typing import Any

import httpx
import numpy as np

from speech_to_speech.arguments_classes.ani_voice_tts_arguments import AniVoiceTTSHandlerArguments
from speech_to_speech.arguments_classes.chat_tts_arguments import ChatTTSHandlerArguments
from speech_to_speech.arguments_classes.facebookmms_tts_arguments import FacebookMMSTTSHandlerArguments
from speech_to_speech.arguments_classes.kokoro_tts_arguments import KokoroTTSHandlerArguments
from speech_to_speech.arguments_classes.module_arguments import ModuleArguments
from speech_to_speech.arguments_classes.pocket_tts_arguments import PocketTTSHandlerArguments
from speech_to_speech.arguments_classes.qwen3_tts_arguments import Qwen3TTSHandlerArguments
from speech_to_speech.pipeline.cancel_scope import CancelScope
from speech_to_speech.pipeline.messages import TTSInput
from speech_to_speech.s2s_pipeline import get_tts_handler, rename_args
from speech_to_speech.TTS.ani_voice_tts_handler import AniVoiceTTSHandler


def _wav_bytes(sample_rate: int = 16000, sample_count: int = 32) -> bytes:
    pcm = (np.linspace(-0.25, 0.25, sample_count, dtype=np.float32) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _ndjson_audio_line(wav_payload: bytes) -> str:
    return json.dumps({"audio_base64": base64.b64encode(wav_payload).decode("ascii")})


def _new_handler(
    *,
    blocksize: int = 8,
    sample_rate: int = 16000,
    cancel_scope: CancelScope | None = None,
) -> AniVoiceTTSHandler:
    handler = object.__new__(AniVoiceTTSHandler)
    handler.setup(
        Event(),
        api_url="http://ani.test",
        style="F3",
        speed=1.25,
        timeout_s=30.0,
        blocksize=blocksize,
        sample_rate=sample_rate,
        cancel_scope=cancel_scope,
    )
    return handler


class _FakeResponse:
    def __init__(
        self,
        lines: list[str],
        *,
        status_code: int = 200,
        on_after_first_line: Any | None = None,
    ) -> None:
        self.lines = lines
        self.status_code = status_code
        self.on_after_first_line = on_after_first_line

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        request = httpx.Request("POST", "http://ani.test/api/v1/synthesize/stream")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError("boom", request=request, response=response)

    def iter_lines(self):
        for index, line in enumerate(self.lines):
            yield line
            if index == 0 and self.on_after_first_line is not None:
                self.on_after_first_line()


def _patch_httpx_client(monkeypatch, response: _FakeResponse) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, timeout: httpx.Timeout) -> None:
            captured["timeout"] = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs
            return response

    monkeypatch.setattr("speech_to_speech.TTS.ani_voice_tts_handler.httpx.Client", FakeClient)
    return captured


def test_get_tts_handler_constructs_ani_voice_handler():
    ani_args = AniVoiceTTSHandlerArguments(ani_voice_api_url="http://ani.local:8000", ani_voice_blocksize=256)
    rename_args(ani_args, "ani_voice")

    handler = get_tts_handler(
        ModuleArguments(tts="ani-voice"),
        Event(),
        Queue(),
        Queue(),
        Event(),
        ChatTTSHandlerArguments(),
        FacebookMMSTTSHandlerArguments(),
        PocketTTSHandlerArguments(),
        KokoroTTSHandlerArguments(),
        Qwen3TTSHandlerArguments(),
        ani_args,
    )

    assert isinstance(handler, AniVoiceTTSHandler)
    assert handler.api_url == "http://ani.local:8000"
    assert handler.blocksize == 256


def test_fake_ndjson_stream_produces_pcm16_blocks(monkeypatch):
    response = _FakeResponse([_ndjson_audio_line(_wav_bytes(sample_rate=16000, sample_count=20))])
    captured = _patch_httpx_client(monkeypatch, response)
    handler = _new_handler(blocksize=8)

    blocks = list(handler.process(TTSInput(text="Здравей, свят!")))

    assert captured["method"] == "POST"
    assert captured["url"] == "http://ani.test/api/v1/synthesize/stream"
    assert captured["kwargs"]["json"] == {"text": "Здравей, свят!", "voice_style": "F3", "speed": 1.25}
    assert [block.shape for block in blocks] == [(8,), (8,), (8,)]
    assert all(block.dtype == np.int16 for block in blocks)


def test_wav_sample_rate_conversion_to_16khz():
    handler = _new_handler(blocksize=512)
    audio, sample_rate = handler._decode_wav_bytes(_wav_bytes(sample_rate=24000, sample_count=2400))

    resampled = handler._resample_to_pipeline_rate(audio, sample_rate)

    assert sample_rate == 24000
    assert abs(resampled.size - 1600) <= 1


def test_api_error_and_empty_stream_do_not_crash(monkeypatch):
    handler = _new_handler(blocksize=8)

    _patch_httpx_client(monkeypatch, _FakeResponse([json.dumps({"error": "boom"})]))
    assert list(handler.process(TTSInput(text="Тест"))) == []

    _patch_httpx_client(monkeypatch, _FakeResponse([]))
    assert list(handler.process(TTSInput(text="Тест"))) == []


def test_http_error_does_not_crash_pipeline(monkeypatch):
    handler = _new_handler(blocksize=8)

    _patch_httpx_client(monkeypatch, _FakeResponse([], status_code=502))

    assert list(handler.process(TTSInput(text="Тест"))) == []


def test_cancellation_stops_streaming_after_current_chunk(monkeypatch):
    cancel_scope = CancelScope()
    response = _FakeResponse(
        [
            _ndjson_audio_line(_wav_bytes(sample_rate=16000, sample_count=8)),
            _ndjson_audio_line(_wav_bytes(sample_rate=16000, sample_count=8)),
        ],
        on_after_first_line=cancel_scope.cancel,
    )
    _patch_httpx_client(monkeypatch, response)
    handler = _new_handler(blocksize=8, cancel_scope=cancel_scope)

    blocks = list(handler.process(TTSInput(text="Тест", cancel_generation=0)))

    assert len(blocks) == 1
    assert blocks[0].shape == (8,)
