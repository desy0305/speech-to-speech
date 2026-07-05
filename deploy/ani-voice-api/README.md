# Ani Voice API Sidecar

This image isolates the Bulgarian Ani-Voice-API runtime from the main
speech-to-speech backend.

It clones `https://huggingface.co/beleata74/Ani-Voice-API` at
`91722a7fdd404cca1818ace44038ed43322fc66e` and serves Ani's FastAPI app on
port `8000`.

The Ani repository currently pins unreleased CUDA 13.2 nightly torch packages.
This sidecar intentionally installs stable CUDA 12.8 PyTorch wheels instead, so
the main backend never inherits those pinned development dependencies.

The build copies an optimized `tts_engine.py` over Ani's original engine. It
preloads BgTTS, MioCodec, and Supertonic at startup and caches the speaker
embedding for fixed voice styles such as `F5`. Runtime knobs:

- `ANI_VOICE_PRELOAD` (`true`)
- `ANI_VOICE_CACHE_SPEAKER` (`true`)
- `ANI_VOICE_WARMUP_TEXT` (`Здравейте.`)
- `ANI_VOICE_REFERENCE_TEXT` (`Здравейте, радвам се да ви помогна.`)
- `ANI_VOICE_TIMING_LOGS` (`true`)
