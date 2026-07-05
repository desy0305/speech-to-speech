from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import wave
from collections.abc import Iterator
from math import gcd
from threading import Event
from time import perf_counter
from typing import Any

import httpx
import numpy as np
from rich.console import Console
from scipy.signal import resample_poly

from speech_to_speech.baseHandler import BaseHandler
from speech_to_speech.pipeline.cancel_scope import CancelScope
from speech_to_speech.pipeline.handler_types import TTSIn, TTSOut
from speech_to_speech.pipeline.messages import AUDIO_RESPONSE_DONE, EndOfResponse
from speech_to_speech.pipeline.speculative_turns import SpeculativeTurnTracker

logger = logging.getLogger(__name__)
console = Console()


class AniVoiceTTSHandler(BaseHandler[TTSIn, TTSOut]):
    """TTS handler for a Bulgarian Ani-Voice-API sidecar."""

    def setup(
        self,
        should_listen: Event,
        api_url: str = "http://ani-voice-api:8000",
        style: str = "F5",
        speed: float = 1.6,
        timeout_s: float = 120.0,
        blocksize: int = 512,
        sample_rate: int = 16000,
        gen_kwargs: dict[str, Any] | None = None,
        cancel_scope: CancelScope | None = None,
        speculative_turns: SpeculativeTurnTracker | None = None,
    ) -> None:
        self.should_listen = should_listen
        self.api_url = api_url.rstrip("/")
        self.voice_style = style
        self.speed = speed
        self.timeout_s = timeout_s
        if blocksize <= 0:
            raise ValueError(f"ani-voice blocksize must be positive, got {blocksize}")
        if sample_rate <= 0:
            raise ValueError(f"ani-voice sample_rate must be positive, got {sample_rate}")
        self.blocksize = blocksize
        self.sample_rate = sample_rate
        self.cancel_scope = cancel_scope
        self.speculative_turns = speculative_turns
        self.gen_kwargs = gen_kwargs or {}

    @property
    def min_time_to_debug(self) -> float:
        return 0.1

    @property
    def stream_url(self) -> str:
        return f"{self.api_url}/api/v1/synthesize/stream"

    def _is_cancelled(self, generation: int | None) -> bool:
        return generation is not None and self.cancel_scope is not None and self.cancel_scope.is_stale(generation)

    def process(self, tts_input: TTSIn) -> Iterator[TTSOut]:
        speculative_turns = getattr(self, "speculative_turns", None)
        if isinstance(tts_input, EndOfResponse):
            if speculative_turns and not speculative_turns.is_latest_after_reopen_grace(
                tts_input.turn_id,
                tts_input.turn_revision,
            ):
                return
            yield AUDIO_RESPONSE_DONE
            return

        if speculative_turns and not speculative_turns.is_latest_after_reopen_grace(
            tts_input.turn_id,
            tts_input.turn_revision,
        ):
            logger.debug("Dropping stale TTS input for turn=%s rev=%s", tts_input.turn_id, tts_input.turn_revision)
            return
        if speculative_turns:
            speculative_turns.commit(tts_input.turn_id, tts_input.turn_revision)

        generation = tts_input.cancel_generation
        if generation is None and self.cancel_scope is not None:
            generation = self.cancel_scope.generation

        text = tts_input.text.strip()
        if not text:
            return

        console.print(f"[green]ASSISTANT: {text}")
        logger.debug("Generating Ani Voice audio for: %s...", text[:80])

        started_at = perf_counter()
        first_chunk = True
        for block in self._iter_pcm_blocks(text, generation):
            if first_chunk:
                logger.debug("Ani Voice time to first audio: %.3fs", perf_counter() - started_at)
                first_chunk = False
            yield block

    def _iter_pcm_blocks(self, text: str, generation: int | None) -> Iterator[np.ndarray]:
        payload = {
            "text": text,
            "voice_style": self.voice_style,
            "speed": self.speed,
        }
        timeout = httpx.Timeout(self.timeout_s, connect=min(10.0, self.timeout_s))

        try:
            with httpx.Client(timeout=timeout) as client:
                with client.stream(
                    "POST",
                    self.stream_url,
                    json=payload,
                    headers={"Accept": "application/x-ndjson"},
                ) as response:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        logger.error("Ani Voice TTS API returned HTTP %s: %s", exc.response.status_code, exc)
                        return

                    wav_chunks = self._iter_wav_chunks_from_ndjson_lines(response.iter_lines(), generation)
                    yield from self._iter_pcm_blocks_from_wav_chunks(wav_chunks, generation)
        except httpx.HTTPError as exc:
            logger.error("Ani Voice TTS request failed: %s", exc)

    def _iter_wav_chunks_from_ndjson_lines(
        self,
        lines: Iterator[str | bytes],
        generation: int | None,
    ) -> Iterator[bytes]:
        for raw_line in lines:
            if self._is_cancelled(generation):
                logger.info("Ani Voice TTS generation cancelled")
                return

            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Ignoring malformed Ani Voice NDJSON line: %r", line[:120])
                continue

            if event.get("error"):
                logger.error("Ani Voice TTS API error: %s", event["error"])
                return

            audio_base64 = event.get("audio_base64")
            if not audio_base64:
                continue

            try:
                yield base64.b64decode(audio_base64, validate=True)
            except (TypeError, ValueError, binascii.Error) as exc:
                logger.warning("Ignoring malformed Ani Voice audio_base64 chunk: %s", exc)

    def _iter_pcm_blocks_from_wav_chunks(
        self,
        wav_chunks: Iterator[bytes] | list[bytes],
        generation: int | None,
    ) -> Iterator[np.ndarray]:
        leftover = np.empty(0, dtype=np.int16)
        for wav_bytes in wav_chunks:
            if self._is_cancelled(generation):
                logger.info("Ani Voice TTS generation cancelled")
                return

            try:
                audio, sample_rate = self._decode_wav_bytes(wav_bytes)
                audio = self._resample_to_pipeline_rate(audio, sample_rate)
                pcm = self._float_to_pcm16(audio)
            except (EOFError, ValueError, wave.Error) as exc:
                logger.warning("Ignoring malformed Ani Voice WAV chunk: %s", exc)
                continue

            if pcm.size == 0:
                continue

            combined = np.concatenate((leftover, pcm)) if leftover.size else pcm
            usable = (combined.size // self.blocksize) * self.blocksize
            for start in range(0, usable, self.blocksize):
                if self._is_cancelled(generation):
                    logger.info("Ani Voice TTS generation cancelled")
                    return
                yield combined[start : start + self.blocksize].copy()
            leftover = combined[usable:]

        if leftover.size and not self._is_cancelled(generation):
            yield np.pad(leftover, (0, self.blocksize - leftover.size)).astype(np.int16, copy=False)

    @staticmethod
    def _decode_wav_bytes(wav_bytes: bytes) -> tuple[np.ndarray, int]:
        with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frame_count = reader.getnframes()
            frames = reader.readframes(frame_count)

        if not frames:
            return np.empty(0, dtype=np.float32), sample_rate

        if sample_width == 1:
            raw = np.frombuffer(frames, dtype=np.uint8).astype(np.float32)
            audio = (raw - 128.0) / 128.0
        elif sample_width == 2:
            raw = np.frombuffer(frames, dtype="<i2").astype(np.float32)
            audio = raw / 32768.0
        elif sample_width == 3:
            raw = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            values = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
            values = np.where(values & 0x800000, values | ~0xFFFFFF, values)
            audio = values.astype(np.float32) / 8388608.0
        elif sample_width == 4:
            raw = np.frombuffer(frames, dtype="<i4").astype(np.float32)
            audio = raw / 2147483648.0
        else:
            raise ValueError(f"unsupported WAV sample width: {sample_width}")

        if channels > 1:
            sample_count = (audio.size // channels) * channels
            audio = audio[:sample_count].reshape(-1, channels).mean(axis=1)

        return audio.astype(np.float32, copy=False), sample_rate

    def _resample_to_pipeline_rate(self, audio: np.ndarray, source_rate: int) -> np.ndarray:
        if source_rate == self.sample_rate or audio.size == 0:
            return audio.astype(np.float32, copy=False)
        if source_rate <= 0:
            raise ValueError(f"invalid source sample rate: {source_rate}")

        factor = gcd(self.sample_rate, source_rate)
        return resample_poly(audio, self.sample_rate // factor, source_rate // factor).astype(
            np.float32,
            copy=False,
        )

    @staticmethod
    def _float_to_pcm16(audio: np.ndarray) -> np.ndarray:
        scaled = np.clip(audio * 32768.0, -32768, 32767)
        return scaled.astype(np.int16)
