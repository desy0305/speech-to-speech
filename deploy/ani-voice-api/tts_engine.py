import io
import os
import re
import sys
import tempfile
import threading
import time
import wave

import numpy as np
import torch

# Add BgTTS to sys.path so its local imports keep working after the Dockerfile
# overwrites Ani's original tts_engine.py with this optimized engine.
sys.path.append(os.path.join(os.path.dirname(__file__), "BgTTS"))

from codec import CodecV6
from config import CODEC_FRAME_RATE, CODEC_SAMPLE_RATE
from inference import _split_text, generate
from model import load_for_inference
from normalizer import normalize_text
from tokenizer import TTSTokenizer


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


class TTSEngine:
    def __init__(self):
        started_at = time.perf_counter()
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.preload = _bool_env("ANI_VOICE_PRELOAD", True)
        self.cache_speaker = _bool_env("ANI_VOICE_CACHE_SPEAKER", True)
        self.timing_logs = _bool_env("ANI_VOICE_TIMING_LOGS", True)
        self.warmup_text = os.environ.get("ANI_VOICE_WARMUP_TEXT", "Здравейте.").strip()
        self.reference_text = os.environ.get(
            "ANI_VOICE_REFERENCE_TEXT",
            "Здравейте, радвам се да ви помогна.",
        ).strip()
        self.default_style = os.environ.get("ANI_VOICE_STYLE", "F5").strip() or "F5"
        self.default_speed = _float_env("ANI_VOICE_SPEED", 1.6)
        self.supertonic_steps = _int_env("ANI_VOICE_SUPERTONIC_STEPS", 8)
        self.supertonic_sample_rate = _int_env("ANI_VOICE_SUPERTONIC_SAMPLE_RATE", 44100)
        self.max_tokens = _int_env("ANI_VOICE_MAX_TOKENS", 512)
        self.temperature = _float_env("ANI_VOICE_TEMPERATURE", 0.7)
        self.top_k = _int_env("ANI_VOICE_TOP_K", 250)
        self.top_p = _float_env("ANI_VOICE_TOP_P", 0.95)
        self.rep_penalty = _float_env("ANI_VOICE_REP_PENALTY", 1.1)

        self.bgtts_checkpoint = os.path.join(
            os.path.dirname(__file__),
            "BgTTS",
            "checkpoint_inference.pt",
        )
        self.lock = threading.Lock()
        self.model = None
        self.tokenizer = None
        self.codec = None
        self.supertonic = None
        self.voice_styles = {}
        self.speaker_embeddings = {}

        self._log(f"starting Ani cached TTS engine on {self.device}")
        self._ensure_supertonic()
        if self.preload:
            self._ensure_bgtts_loaded()
            if self.cache_speaker:
                self._speaker_embedding(self.reference_text, self.default_style, self.default_speed)
            if self.warmup_text:
                self.generate_chunk(self.warmup_text, self.default_style, self.default_speed)
        self._log(f"engine startup complete in {time.perf_counter() - started_at:.3f}s")

    def _log(self, message: str) -> None:
        if self.timing_logs:
            print(f"[ani-cache] {message}", flush=True)

    def _ensure_supertonic(self):
        if self.supertonic is not None:
            return self.supertonic
        started_at = time.perf_counter()
        from supertonic import TTS

        self.supertonic = TTS(auto_download=True)
        self._log(f"Supertonic loaded in {time.perf_counter() - started_at:.3f}s")
        return self.supertonic

    def _ensure_bgtts_loaded(self) -> None:
        if self.model is not None and self.tokenizer is not None and self.codec is not None:
            return
        started_at = time.perf_counter()
        self.model = load_for_inference(self.bgtts_checkpoint, device=self.device)
        self.tokenizer = TTSTokenizer()
        self.codec = CodecV6(device=self.device)
        self._log(f"BgTTS model/tokenizer/MioCodec loaded in {time.perf_counter() - started_at:.3f}s")

    def _voice_style(self, voice_style: str):
        style_name = voice_style.strip() if isinstance(voice_style, str) else self.default_style
        style_name = style_name or self.default_style
        if style_name not in self.voice_styles:
            started_at = time.perf_counter()
            self.voice_styles[style_name] = self._ensure_supertonic().get_voice_style(voice_name=style_name)
            self._log(f"Supertonic style {style_name} resolved in {time.perf_counter() - started_at:.3f}s")
        return style_name, self.voice_styles[style_name]

    def split_text_for_tts(self, text: str) -> list[str]:
        text = text.strip()
        if not text:
            return []
        raw = re.split(r"(?<=[\.\!\?…])\s+|\n+", text)
        chunks = []
        buf = ""
        for part in raw:
            part = part.strip()
            if not part:
                continue
            if not buf or len(buf) < 80 or len(buf) + len(part) + 1 <= 200:
                buf = (buf + " " + part).strip()
            else:
                chunks.append(buf)
                buf = part
        if buf:
            chunks.append(buf)
        return chunks

    @staticmethod
    def _clean_text(text: str) -> str:
        return (
            text.replace('"', "")
            .replace("„", "")
            .replace("“", "")
            .replace("’", "'")
            .replace("–", "-")
            .replace("—", "-")
            .replace("*", "")
            .strip()
        )

    def _supertonic_reference_waveform(self, text: str, voice_style: str, speed: float) -> torch.Tensor:
        style_name, style = self._voice_style(voice_style)
        started_at = time.perf_counter()
        wav_array, _ = self._ensure_supertonic().synthesize(
            text,
            voice_style=style,
            total_steps=self.supertonic_steps,
            lang="bg",
            speed=speed,
        )
        wav_data = np.asarray(wav_array, dtype=np.float32).reshape(-1)
        wav_max = float(np.max(np.abs(wav_data))) if wav_data.size else 0.0
        if wav_max > 0:
            wav_data = wav_data / wav_max
        self._log(
            f"Supertonic reference generated for {style_name}@{speed:g} "
            f"in {time.perf_counter() - started_at:.3f}s"
        )
        return torch.from_numpy(wav_data.copy())

    def _speaker_embedding(self, chunk_text: str, voice_style: str, speed: float) -> torch.Tensor:
        self._ensure_bgtts_loaded()
        style_name = voice_style.strip() if isinstance(voice_style, str) and voice_style.strip() else self.default_style
        key = (style_name, round(float(speed), 3))
        if self.cache_speaker and key in self.speaker_embeddings:
            return self.speaker_embeddings[key]

        reference_text = self.reference_text if self.cache_speaker else chunk_text
        started_at = time.perf_counter()
        waveform = self._supertonic_reference_waveform(reference_text, style_name, speed)
        result = self.codec.encode_waveform(waveform, self.supertonic_sample_rate)
        speaker_emb = result["global_embedding"].detach().cpu()
        self._log(
            f"speaker embedding {'cached' if self.cache_speaker else 'generated'} "
            f"for {style_name}@{speed:g} in {time.perf_counter() - started_at:.3f}s"
        )
        if self.cache_speaker:
            self.speaker_embeddings[key] = speaker_emb
        return speaker_emb

    def generate_chunk(self, chunk_text: str, voice_style: str = "F5", speed: float = 1.6) -> bytes:
        clean_text = self._clean_text(chunk_text)
        if not clean_text:
            return b""

        request_started_at = time.perf_counter()
        with self.lock:
            self._ensure_bgtts_loaded()
            speaker_started_at = time.perf_counter()
            speaker_emb = self._speaker_embedding(clean_text, voice_style, speed)
            speaker_s = time.perf_counter() - speaker_started_at

            split_chunks = _split_text(clean_text, self.tokenizer, max_len=250)
            self._log(f"text split into {len(split_chunks)} BgTTS chunk(s)")

            gen_started_at = time.perf_counter()
            all_codes = []
            for index, chunk in enumerate(split_chunks):
                enc_len = len(self.tokenizer.build_encoder_input(chunk))
                self._log(f"  [{index + 1}/{len(split_chunks)}] {enc_len} enc tokens: '{chunk[:60]}...'")
                codes = generate(
                    self.model,
                    self.tokenizer,
                    chunk,
                    speaker_emb,
                    max_new_tokens=self.max_tokens,
                    temperature=self.temperature,
                    top_k=self.top_k,
                    top_p=self.top_p,
                    rep_penalty=self.rep_penalty,
                    device=self.device,
                )
                if codes is not None and len(codes) > 0:
                    all_codes.append(codes)
            gen_s = time.perf_counter() - gen_started_at

            if not all_codes:
                self._log("no audio generated")
                return b""

            codes = torch.cat(all_codes)
            audio_dur = len(codes) / CODEC_FRAME_RATE
            rtf = gen_s / audio_dur if audio_dur > 0 else float("inf")

            decode_started_at = time.perf_counter()
            fd, final_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            try:
                wav = self.codec.tokens_to_wav(codes, speaker_emb, final_path)
                with open(final_path, "rb") as handle:
                    audio_bytes = handle.read()
            finally:
                try:
                    os.remove(final_path)
                except OSError:
                    pass
            decode_s = time.perf_counter() - decode_started_at

        self._log(
            f"chunk total={time.perf_counter() - request_started_at:.3f}s "
            f"speaker={speaker_s:.3f}s gen={gen_s:.3f}s decode={decode_s:.3f}s "
            f"tokens={len(codes)} audio={audio_dur:.2f}s rtf={rtf:.3f} "
            f"wav={len(wav) / CODEC_SAMPLE_RATE:.2f}s bytes={len(audio_bytes)}"
        )
        return audio_bytes

    def synthesize_stream(self, text: str, voice_style: str = "F5", speed: float = 1.6):
        normalized_text = normalize_text(text)
        chunks = self.split_text_for_tts(normalized_text)
        self._log(f"stream request split into {len(chunks)} text chunk(s)")
        for chunk in chunks:
            audio_bytes = self.generate_chunk(chunk, voice_style, speed)
            if audio_bytes:
                yield audio_bytes

    def synthesize_full(self, text: str, voice_style: str = "F5", speed: float = 1.6) -> bytes:
        normalized_text = normalize_text(text)
        chunks = self.split_text_for_tts(normalized_text)

        all_frames = b""
        params = None

        for chunk in chunks:
            audio_bytes = self.generate_chunk(chunk, voice_style, speed)
            if not audio_bytes:
                continue
            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                if not params:
                    params = wf.getparams()
                all_frames += wf.readframes(wf.getnframes())

        if not params:
            return b""

        out_io = io.BytesIO()
        with wave.open(out_io, "wb") as wf:
            wf.setparams(params)
            wf.writeframes(all_frames)
        return out_io.getvalue()


engine = TTSEngine()
