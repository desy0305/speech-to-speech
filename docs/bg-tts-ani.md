# Bulgarian TTS with Ani Voice

This is an experimental, failsafe Bulgarian TTS profile. The default
`qwen3` backend remains unchanged.

Run the BG profile from the `speech-to-speech` repo root:

```bash
docker compose -f docker-compose.bgtts-ani.yml --profile bgtts-ani up --build
```

The profile starts:

- `ani-voice-api`: isolated Ani-Voice-API sidecar pinned to
  `91722a7fdd404cca1818ace44038ed43322fc66e`, serving
  `http://ani-voice-api:8000`.
- `backend-lmstudio-bgtts`: speech-to-speech realtime backend using
  `--tts ani-voice`, exposed on `S2S_BG_PORT` (`8766` by default).

Relevant `.env` values:

```env
S2S_BG_PORT=8766
ANI_VOICE_REV=91722a7fdd404cca1818ace44038ed43322fc66e
ANI_VOICE_PORT=8001
ANI_VOICE_API_URL=http://ani-voice-api:8000
ANI_VOICE_STYLE=F5
ANI_VOICE_SPEED=1.6
ANI_VOICE_TIMEOUT_S=120
ANI_VOICE_BLOCKSIZE=512
ANI_VOICE_PRELOAD=true
ANI_VOICE_CACHE_SPEAKER=true
ANI_VOICE_WARMUP_TEXT=Здравейте.
ANI_VOICE_REFERENCE_TEXT=Здравейте, радвам се да ви помогна.
ANI_VOICE_TIMING_LOGS=true
```

No new API keys are required for public downloads. Set `HF_TOKEN` only if
Hugging Face download limits or private access require it.

Quick checks:

```bash
docker compose -f docker-compose.bgtts-ani.yml --profile bgtts-ani config
docker compose -f docker-compose.bgtts-ani.yml --profile bgtts-ani build backend-lmstudio-bgtts ani-voice-api
```

The Ani sidecar exposes:

- `POST /api/v1/synthesize`
- `POST /api/v1/synthesize/stream`

The speech-to-speech handler uses the streaming endpoint and converts Ani WAV
chunks into the existing 16 kHz PCM16 output blocks.

The sidecar image overwrites Ani's original `tts_engine.py` with a cached
engine. It preloads BgTTS, MioCodec, and Supertonic once at startup and caches
the fixed F5 speaker embedding by `(voice_style, speed)`. Disable
`ANI_VOICE_CACHE_SPEAKER` only if you need the older per-request reference
behavior for quality comparison.
