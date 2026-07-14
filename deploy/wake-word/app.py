from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

logger = logging.getLogger("wake-word")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

SAMPLE_RATE = 16_000
MAX_FRAME_BYTES = 128 * 1024
MODEL_DIR = Path(os.environ.get("WAKE_WORD_MODEL_DIR", "/opt/wake-word/model"))
PHRASE = " ".join(os.environ.get("WAKE_WORD_PHRASE", "HEY EVA").upper().split())
THREADS = max(
    1,
    min(4, int(os.environ.get("WAKE_WORD_NUM_THREADS", os.environ.get("WAKE_WORD_THREADS", "1")))),
)
SCORE = max(0.0, min(10.0, float(os.environ.get("WAKE_WORD_SCORE", "2.5"))))
THRESHOLD = max(0.01, min(0.99, float(os.environ.get("WAKE_WORD_THRESHOLD", "0.10"))))

ENCODER = MODEL_DIR / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
DECODER = MODEL_DIR / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
JOINER = MODEL_DIR / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
TOKENS = MODEL_DIR / "tokens.txt"
BPE_MODEL = MODEL_DIR / "bpe.model"
KEYWORDS_RAW = Path("/tmp/wake-keywords/keywords_raw.txt")
KEYWORDS = Path("/tmp/wake-keywords/keywords.txt")

app = FastAPI(title="local-wake-word", docs_url=None, redoc_url=None, openapi_url=None)
spotter: Any | None = None
startup_error = ""
decode_lock = asyncio.Lock()


def _required_model_files() -> tuple[Path, ...]:
    return ENCODER, DECODER, JOINER, TOKENS, BPE_MODEL


def _generate_keywords_file() -> None:
    KEYWORDS_RAW.parent.mkdir(parents=True, exist_ok=True)
    KEYWORDS_RAW.write_text(f"{PHRASE} :{SCORE:g} #{THRESHOLD:g}\n", encoding="utf-8")
    result = subprocess.run(
        [
            "sherpa-onnx-cli",
            "text2token",
            "--tokens",
            str(TOKENS),
            "--tokens-type",
            "bpe",
            "--bpe-model",
            str(BPE_MODEL),
            str(KEYWORDS_RAW),
            str(KEYWORDS),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "keyword tokenizer failed").strip()[-300:]
        raise RuntimeError(f"keyword tokenizer failed: {detail}")


def _load_spotter() -> Any:
    missing = [str(path) for path in _required_model_files() if not path.is_file()]
    if missing:
        raise RuntimeError(f"wake model is incomplete: {', '.join(missing)}")
    _generate_keywords_file()

    import sherpa_onnx

    return sherpa_onnx.KeywordSpotter(
        tokens=str(TOKENS),
        encoder=str(ENCODER),
        decoder=str(DECODER),
        joiner=str(JOINER),
        num_threads=THREADS,
        max_active_paths=4,
        keywords_score=SCORE,
        keywords_threshold=THRESHOLD,
        keywords_file=str(KEYWORDS),
        provider="cpu",
    )


def parse_detection(result: Any) -> dict[str, Any] | None:
    """Normalize sherpa-onnx's string/JSON result without logging audio."""
    if result is None:
        return None
    if isinstance(result, dict):
        data = result
    else:
        raw = str(result).strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"keyword": raw}
        data = parsed if isinstance(parsed, dict) else {"keyword": raw}

    keyword = " ".join(str(data.get("keyword") or PHRASE).upper().split())
    if not keyword:
        return None
    normalized: dict[str, Any] = {"phrase": keyword}
    timestamps = data.get("timestamps")
    if isinstance(timestamps, list):
        normalized["timestamps"] = [value for value in timestamps[:32] if isinstance(value, (int, float))]
    return normalized


@app.on_event("startup")
async def initialize() -> None:
    global spotter, startup_error
    try:
        spotter = await asyncio.to_thread(_load_spotter)
        startup_error = ""
        logger.info("wake detector ready phrase=%r threads=%d", PHRASE.title(), THREADS)
    except Exception as exc:  # pragma: no cover - exercised by container health checks
        spotter = None
        startup_error = f"{exc.__class__.__name__}: {exc}"
        logger.exception("wake detector initialization failed")


@app.get("/health")
async def health() -> JSONResponse:
    payload = {
        "ready": spotter is not None,
        "status": "ready" if spotter is not None else "error",
        "phrase": PHRASE.title(),
        "sampleRate": SAMPLE_RATE,
        "engine": "sherpa-onnx",
        "version": "1.13.4",
        **({"detail": startup_error[:300]} if startup_error else {}),
    }
    return JSONResponse(status_code=200 if spotter is not None else 503, content=payload)


@app.websocket("/v1/detect")
async def detect(websocket: WebSocket) -> None:
    await websocket.accept()
    if spotter is None:
        await websocket.send_json({"type": "error", "code": "not_ready", "message": "Wake detector is not ready."})
        await websocket.close(code=1011)
        return

    stream = spotter.create_stream()
    received_samples = 0
    await websocket.send_json(
        {
            "type": "ready",
            "phrase": PHRASE.title(),
            "sampleRate": SAMPLE_RATE,
            "frameFormat": "pcm16le-mono",
        }
    )
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            payload = message.get("bytes")
            if payload is None:
                await websocket.send_json(
                    {"type": "error", "code": "binary_required", "message": "Send PCM16 as binary WebSocket frames."}
                )
                continue
            if not payload or len(payload) > MAX_FRAME_BYTES or len(payload) % 2:
                await websocket.send_json(
                    {"type": "error", "code": "invalid_frame", "message": "PCM16 frame length is invalid."}
                )
                continue

            samples = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
            received_samples += int(samples.size)
            detection = None
            async with decode_lock:
                stream.accept_waveform(SAMPLE_RATE, samples)
                while spotter.is_ready(stream):
                    spotter.decode_stream(stream)
                detection = parse_detection(spotter.get_result(stream))
                if detection:
                    spotter.reset_stream(stream)

            if detection:
                await websocket.send_json(
                    {
                        "type": "detected",
                        **detection,
                        "receivedSamples": received_samples,
                        "detectedAt": time.time(),
                    }
                )
                logger.info("wake phrase detected phrase=%r", detection["phrase"].title())
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("wake stream failed: %s", exc.__class__.__name__)
        try:
            await websocket.send_json({"type": "error", "code": "detector_error", "message": "Wake detection failed."})
            await websocket.close(code=1011)
        except Exception:
            pass
