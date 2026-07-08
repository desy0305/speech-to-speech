# Production Docker Setup

This guide is the deployment checklist for this fork. It assumes the main branch
is the source of truth and that secrets live only in `.env`, never in git.

## What This Stack Runs

The local production stack is intentionally multi-container:

- `ui`: the Hugging Face realtime voice browser UI on `UI_PORT` (`7860`).
- `backend-lmstudio`: default speech-to-speech backend on `S2S_PORT` (`8765`).
- `backend-lmstudio-bgtts`: optional Bulgarian Ani TTS backend on `S2S_BG_PORT`
  (`8766`).
- `ani-voice-api`: isolated Ani-Voice-API sidecar on `ANI_VOICE_PORT` (`8001`).
- `ui-https`: optional LAN HTTPS reverse proxy on `UI_HTTPS_PORT` (`7862`).
- Docker MCP gateway: host-side process, not a compose service.

Keeping these pieces separate is deliberate. The Ani sidecar has risky CUDA and
TTS dependencies that should not be installed into the main backend, and the UI
should stay small and restartable.

## Required Keys

Only configure the providers you use:

- `LM_STUDIO_API_KEY`: required only if LM Studio server auth is enabled.
- `CEREBRAS_API_KEY`: required for direct Cerebras.
- `HF_TOKEN`: required for Hugging Face downloads only when public rate limits or
  private access require it.
- `GEMINI_API_KEY`: required for Gemini.
- `BGGPT_API_KEY`: required for BGGPT.
- `TAVILY_API_KEY` or `SERPER_API_KEY`: optional web search tool.
- `MCP_GATEWAY_AUTH_TOKEN`: optional, but recommended if MCP gateway auth is
  enabled.

Do not commit `.env`. Use `.env.local.example` or `.env.example` as the template.

## Fresh Server Setup

```powershell
git clone https://github.com/desy0305/speech-to-speech.git
cd speech-to-speech
copy .env.local.example .env
notepad .env
```

Minimum LM Studio setup:

```env
LM_STUDIO_BASE_URL=http://host.docker.internal:1234/v1
LM_STUDIO_MODEL=google/gemma-4-12b-qat
LM_STUDIO_API_KEY=
S2S_CHAT_SIZE=60
S2S_BG_STT_LANGUAGE=bg
S2S_LLM_REQUEST_TIMEOUT_S=120
QWEN3_TTS_MAX_NEW_TOKENS=3072
```

Start default LM Studio + qwen3 TTS:

```powershell
docker compose -f docker-compose.local.yml --profile lmstudio up -d --build
```

Start default backend plus Bulgarian Ani TTS:

```powershell
docker compose -f docker-compose.local.yml --profile lmstudio --profile bgtts-ani up -d --build
```

Open:

- UI: `http://localhost:7860`
- Default backend docs: `http://localhost:8765/docs`
- BG backend docs: `http://localhost:8766/docs`
- Ani sidecar docs: `http://localhost:8001/docs`

## LAN HTTPS

Browsers usually block microphone capture on plain HTTP when accessed from
another computer. Use the LAN HTTPS profile for phones/laptops on the local
network.

Generate a local self-signed certificate:

```powershell
$net = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | Select-Object -First 1
$lanIp = $net.IPv4Address.IPAddress
New-Item -ItemType Directory -Force .\.local-https\certs | Out-Null
$certDir = (Resolve-Path .\.local-https\certs).Path -replace '\\','/'
docker run --rm -v "${certDir}:/certs" alpine:latest sh -c "apk add --no-cache openssl >/dev/null && openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes -keyout /certs/hf-voice-ui.key -out /certs/hf-voice-ui.crt -subj '/CN=${lanIp}' -addext 'subjectAltName=IP:${lanIp},DNS:localhost'"
```

Start with LAN HTTPS:

```powershell
docker compose -f docker-compose.local.yml --profile lmstudio --profile bgtts-ani --profile lan-https up -d --build
```

Open:

```text
https://<LAN_IP>:7862/
```

Accept or trust the self-signed certificate on the client machine. For an
internet-facing server, replace this with a real certificate and a normal
reverse proxy.

The LAN proxy exposes both realtime paths through the same HTTPS origin:
`/s2s/v1/realtime` and `/s2s-bg/v1/realtime`. These are deliberately separate:
`/s2s/v1/realtime` reaches `backend-lmstudio`, while `/s2s-bg/v1/realtime`
reaches `backend-lmstudio-bgtts`. If only one backend is running, the UI marks
the other preset offline instead of proxying it to the wrong stack.

## Optional Visual Observer

The visual observer feature is available by default, but the browser toggle is
off until you turn it on. It is a local-only helper that sends periodic webcam
frames to a separate SmolVLM `llama-server` and injects a capped rolling visual
summary into the main assistant instructions. It does not change STT, TTS, or
the selected LLM provider.

Start the Docker vision model service:

```powershell
docker compose -f docker-compose.local.yml --profile vision-observer up -d smolvlm
```

The service runs `llama-server -hf ggml-org/SmolVLM-500M-Instruct-GGUF`,
exposes `http://localhost:8080`, and shares the repo `cache/` directory for
model downloads. Override `SMOLVLM_IMAGE`, `SMOLVLM_HF_REPO`, or
`SMOLVLM_N_GPU_LAYERS` in `.env` if needed. The UI talks to the internal Docker
URL by default:

```env
SMOLVLM_BASE_URL=http://smolvlm:8080
SMOLVLM_MODEL=ggml-org/SmolVLM-500M-Instruct-GGUF
SMOLVLM_API_KEY=
SMOLVLM_REQUIRE_LOCAL=1
```

The UI server calls `SMOLVLM_BASE_URL/v1/chat/completions` with typed
`image_url` content, matching llama.cpp's OpenAI-compatible multimodal route.
Set `VISION_OBSERVER_ENABLED=0` only when you want to hide the feature entirely.
`SMOLVLM_BASE_URL` may include or omit `/v1`. If you target an authenticated
local OpenAI-style server such as LM Studio, set `SMOLVLM_API_KEY` to that local
token.

## Memory And MCP

MCP tools are exposed through the Docker MCP gateway. The app can show "gateway
online" only when the gateway is reachable.

Start/check from PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\mcp\start-mcp-gateway.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\mcp\check-mcp-health.ps1
```

For persistent memory, make sure the Docker MCP Memory server has a persistent
volume/directory in Docker MCP Toolkit. The model can call memory tools, but the
data will survive restarts only if the MCP server itself is configured with
persistent storage.

Useful memory tools:

- `search_nodes`
- `open_nodes`
- `create_entities`
- `create_relations`
- `add_observations`

## Current Context And Audio Defaults

These defaults were chosen for long voice sessions:

```env
S2S_CHAT_SIZE=60
S2S_BG_STT_LANGUAGE=bg
S2S_LLM_REQUEST_TIMEOUT_S=120
QWEN3_TTS_MAX_NEW_TOKENS=3072
ANI_VOICE_MAX_TOKENS=1024
TZ=Europe/Sofia
```

Details:

- Chat history keeps 60 user turns before rolling compaction/eviction.
- Realtime messages are timestamped when serialized to the LLM context.
- LM Studio has 60 seconds to answer before the backend ends the response.
- Qwen3 TTS codec budget is doubled from the earlier `1536` cap to `3072`.
- Ani BG sidecar generation cap is doubled from `512` to `1024`.

## Docker Hub / Prebuilt Image Path

The easiest production install is to publish prebuilt images and run the compose
stack without rebuilding on the server. This repo includes
`docker-compose.hub.yml` for that path.

Images used by the override:

```env
S2S_UI_IMAGE=desy0305/hf-realtime-voice-ui:latest
S2S_BACKEND_IMAGE=desy0305/speech-to-speech-backend:latest
ANI_VOICE_IMAGE=desy0305/ani-voice-api:91722a7
```

Build and push after Docker Hub login:

```powershell
docker login
docker build -t desy0305/hf-realtime-voice-ui:latest .\apps\hf-realtime-voice-space
docker build -t desy0305/speech-to-speech-backend:latest .
docker build -t desy0305/ani-voice-api:91722a7 .\deploy\ani-voice-api
docker push desy0305/hf-realtime-voice-ui:latest
docker push desy0305/speech-to-speech-backend:latest
docker push desy0305/ani-voice-api:91722a7
```

Run production from published images:

```powershell
docker compose -f docker-compose.local.yml -f docker-compose.hub.yml --profile lmstudio --profile bgtts-ani pull
docker compose -f docker-compose.local.yml -f docker-compose.hub.yml --profile lmstudio --profile bgtts-ani up -d --no-build
```

Use the `lan-https` profile too if clients connect over the LAN:

```powershell
docker compose -f docker-compose.local.yml -f docker-compose.hub.yml --profile lmstudio --profile bgtts-ani --profile lan-https up -d --no-build
```

## Verification Checklist

```powershell
docker compose -f docker-compose.local.yml --profile lmstudio --profile bgtts-ani config
Invoke-WebRequest -UseBasicParsing http://localhost:7860/api/config
Invoke-WebRequest -UseBasicParsing http://localhost:8765/docs
Invoke-WebRequest -UseBasicParsing http://localhost:8766/docs
Invoke-WebRequest -UseBasicParsing http://localhost:8001/docs
```

Expected:

- UI returns config with LM Studio and BG Ani presets.
- MCP status is online when the Docker MCP gateway is running.
- Default backend runs qwen3 TTS.
- BG backend runs Ani TTS and does not load qwen3 TTS.
- Container command includes `--chat_size 60` and `--request_timeout_s 120`.

## Troubleshooting

- If the LAN UI loads but microphone does not work, use HTTPS on `7862`.
- If the assistant appears to forget context after a tool call, check backend
  logs for LM Studio timeouts or provider rate limits.
- If Cerebras returns `429`, switch to LM Studio or wait for the provider quota.
- If memory write succeeds but read returns empty after restart, run
  `powershell -ExecutionPolicy Bypass -File .\scripts\mcp\qa-mcp-audit.ps1 -EnvPath .\.env -RestartGateway`.
  The Docker MCP Memory server stores durable state in the `claude-memory`
  Docker volume at `/app/dist/memory.json`; if that volume is missing or was
  removed, memory starts blank even though the voice app is healthy.
- Local models get first-class memory tools (`search_nodes`, `open_nodes`,
  `create_entities`, `add_observations`, `create_relations`) plus the generic
  `mcp_call` wrapper. Keep all five memory tool names in `MCP_ALLOWED_TOOLS`.
  For Bulgarian users, memory recall should search Cyrillic and Latin forms
  such as `Пловдив`/`Plovdiv` and `Матееви`/`Mateevi`.
- If Ani BG audio starts slowly, check `ani-voice-api` logs for repeated model
  loads. The cached sidecar should preload models and cache speaker embeddings.
