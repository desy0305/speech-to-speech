# Local Docker Deployment

This repository contains both pieces needed for the local full stack:

- repo root: the model backend, exposing OpenAI Realtime-compatible `ws://localhost:8765/v1/realtime`.
- `apps/hf-realtime-voice-space/`: the Hugging Face Space UI, served at `http://localhost:7860`.

## LM Studio LLM, Local STT/TTS

1. In LM Studio, enable the local server. Load `google/gemma-4-12b-qat` for the
   first run; after that, the UI can list and load other downloaded LLMs on demand.
2. Copy `.env.local.example` to `.env`.
3. Set `LM_STUDIO_API_KEY` in `.env`. If Docker cannot reach LM Studio through `host.docker.internal`, set:

   ```env
   LM_STUDIO_BASE_URL=http://<LAN_IP>:1234/v1
   ```

4. Start the stack:

   ```bash
   docker compose -f docker-compose.local.yml --profile lmstudio up --build
   ```

5. Open `http://localhost:7860`. In Settings, `Speech backend` should show
   `LM Studio (local)`, the backend URL should be `http://localhost:8765`, and
   the API provider/model controls should show:

   - API provider: `LM Studio`
   - Model: downloaded LM Studio LLMs returned by `/api/v1/models`; the default
     is `google/gemma-4-12b-qat`
   - STT: `nvidia/parakeet-tdt-0.6b-v3`
   - TTS: `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`

6. If you change the API provider, model, backend preset, or URL while connected, click
   `Restart conversation with these settings`. An already-running conversation
   keeps the provider/model it started with until it is restarted.

The LM Studio profile uses `--llm_backend chat-completions` and `--responses_api_reasoning_effort none`. For the tested Gemma 4 QAT model this is required; otherwise LM Studio may return reasoning-only output with empty speakable content.

To confirm the LLM turn is local, watch LM Studio's server log while talking to
the app. Each assistant response should create a local `/v1/chat/completions`
request. The browser does not call LM Studio directly; it connects to the S2S
backend on `localhost:8765`, and that backend calls LM Studio.

The Settings model dropdown is populated server-side from LM Studio. The UI asks
the local app server, which then queries LM Studio's native `/api/v1/models`
endpoint and filters to LLMs. Before starting a conversation, the UI asks the
local app server to call `/api/v1/models/load` for the selected LM Studio model.
If the model is already loaded this is a no-op; otherwise LM Studio loads it
before the speech backend sends its first chat-completions turn.

## LAN HTTPS Broadcast

Browsers expose microphone/camera APIs only on secure origins, except for
`localhost`. To use the app from another computer on the same network, serve the
UI through the `lan-https` proxy profile and open the HTTPS URL.

Generate a local certificate for your LAN IP:

```powershell
$net = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | Select-Object -First 1
$lanIp = $net.IPv4Address.IPAddress
New-Item -ItemType Directory -Force .\.local-https\certs | Out-Null
$certDir = (Resolve-Path .\.local-https\certs).Path -replace '\\','/'
docker run --rm -v "${certDir}:/certs" alpine:latest sh -c "apk add --no-cache openssl >/dev/null && openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes -keyout /certs/hf-voice-ui.key -out /certs/hf-voice-ui.crt -subj '/CN=${lanIp}' -addext 'subjectAltName=IP:${lanIp},DNS:localhost'"
```

Start the normal backend, optional BG Ani backend, UI, and HTTPS proxy:

```powershell
docker compose -f docker-compose.local.yml --profile lmstudio --profile bgtts-ani --profile lan-https up --build
```

Open `https://<LAN_IP>:50056/` from another computer and accept the
self-signed certificate warning. When the UI is loaded through HTTPS, `/api/config`
automatically rewrites the named presets:

- `LM Studio (local)` -> `https://<host>:50056/s2s/v1/realtime`
- `LM Studio + BG TTS (Ani)` -> `https://<host>:50056/s2s-bg/v1/realtime`

Set the HTTPS proxy credentials before exposing this port outside your machine:

```env
UI_HTTPS_PORT=50056
UI_HTTPS_AUTH_ENABLED=1
UI_HTTPS_AUTH_USER=hfvoice
UI_HTTPS_AUTH_PASSWORD=<strong-password>
```

If auth is enabled but the password is blank, the proxy denies access. The proxy
also sends `noindex` headers, serves a deny-all `robots.txt`, rate-limits UI/API
requests, and blocks common sensitive files such as `.env`, compose files, keys,
certificates, database dumps, and logs.

The HTTPS proxy keeps the routes separate: `/s2s/v1/realtime` reaches
`backend-lmstudio`, and `/s2s-bg/v1/realtime` reaches
`backend-lmstudio-bgtts`. If only the BG Ani profile is running, the default
`LM Studio (local)` preset is shown offline instead of silently hitting the BG
TTS backend.

The old `http://localhost:8765` and `http://localhost:8766` preset URLs remain as
aliases, so browsers with saved settings migrate to the LAN-safe preset URL
instead of showing `Custom backend URL`.

## Optional Visual Observer

The visual observer feature is available by default, but the browser toggle is
off until you turn it on. It does not replace the modular STT/TTS/LLM speech
stack. When enabled, it reuses the webcam preview, sends periodic JPEG frames to
a local SmolVLM `llama-server`, keeps only a capped rolling summary, and
injects that hidden summary into the active instructions.

Start the Docker SmolVLM service:

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
```

The app calls `SMOLVLM_BASE_URL/v1/chat/completions` with typed
`image_url` content. Keep `SMOLVLM_REQUIRE_LOCAL=1` unless you intentionally
want to allow a non-local vision endpoint. Set `VISION_OBSERVER_ENABLED=0` only
when you want to hide the feature entirely.

`SMOLVLM_BASE_URL` can include or omit `/v1`; both
`http://host.docker.internal:8080` and `http://host.docker.internal:8080/v1`
are accepted. If you point the observer at an authenticated local OpenAI-style
server such as LM Studio, set `SMOLVLM_API_KEY` to that local token.

If another computer cannot reach the proxy, allow the HTTPS port on the host:

```powershell
New-NetFirewallRule -DisplayName "HF Voice HTTPS 50056 LAN" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 50056 -RemoteAddress <LAN_CIDR> -Profile Private,Public
```

## Runtime Provider Switching

The `lmstudio` Docker profile now runs one speech stack and switches only the
LLM route per realtime session. This avoids loading Parakeet STT and Qwen3-TTS
multiple times.

Configured providers:

- `LM Studio`: `google/gemma-4-12b-qat`, local OpenAI-compatible chat completions.
- `Cerebras`: `gemma-4-31b`, direct Cerebras OpenAI-compatible API.
- `BGGPT`: `bggpt-gemma-3-27b` and `bggpt-gemma-3-27b-fp8`. `/models` and chat completions were tested; `bggpt-gemma-3-27b` returned `ready`.
- `Gemini`: `gemini-3.5-flash`, disabled until `GEMINI_API_KEY` is set.

Provider selectors are sent as `session.model` values such as
`bggpt::bggpt-gemma-3-27b`. The backend maps those selectors to base URLs and
API keys from environment variables; keys are never sent to the browser.

## Direct Cerebras API

Set `CEREBRAS_API_KEY` and run:

```bash
docker compose -f docker-compose.local.yml --profile cerebras up --build
```

This uses Cerebras' OpenAI-compatible API at `https://api.cerebras.ai/v1` with `gemma-4-31b`.

## Cerebras/HF Router

Set `HF_TOKEN` and run:

```bash
docker compose -f docker-compose.local.yml --profile hf-cerebras up --build
```

This keeps the same local STT/TTS backend and routes only the LLM call through Hugging Face Inference Providers on Cerebras.

## Experimental Bulgarian TTS

The `bgtts-ani` profile starts a separate backend on `S2S_BG_PORT` (`8766` by
default) and an isolated Ani-Voice-API sidecar. The default `lmstudio` profile
still uses qwen3 TTS on `S2S_PORT` (`8765`).

```bash
docker compose -f docker-compose.local.yml --profile bgtts-ani up --build
```

In Settings, choose `LM Studio + BG TTS (Ani)` to connect the UI to
`http://localhost:8766`. The sidecar is internal at
`http://ani-voice-api:8000`, with host access exposed on `ANI_VOICE_PORT`
(`8001`) for direct smoke tests.

Quick sidecar check after it is healthy:

```bash
curl -X POST "http://localhost:8001/api/v1/synthesize" \
  -H "Content-Type: application/json" \
  -d "{\"text\":\"Здравейте! Това е тест на български глас.\",\"voice_style\":\"F5\",\"speed\":1.6}" \
  --output ani-bg-test.wav
```

No new API key is required for public downloads. Set `HF_TOKEN` only if
Hugging Face download limits require it.

## Keys

- Required for LM Studio profile: `LM_STUDIO_API_KEY` if authentication is enabled in LM Studio.
- Required for direct Cerebras: `CEREBRAS_API_KEY`.
- Required for Cerebras/HF Router: `HF_TOKEN`.
- Required for Gemini: `GEMINI_API_KEY`.
- Required for BGGPT: `BGGPT_API_KEY`.
- Optional for UI web search tool: `TAVILY_API_KEY` with `SEARCH_PROVIDER=tavily`.
  `SERPER_API_KEY` remains supported as a fallback with `SEARCH_PROVIDER=serper`.
- Not required for local public default models: `OPENAI_API_KEY`.

The provided Tavily and Serper keys currently return `401/403 Unauthorized`;
the UI returns that as a normal tool result now, but web search will not work
until the provider accepts the key. You can also turn off Web Search in the
Tools modal.

## Docker MCP

Docker MCP Toolkit is available on the host (`docker mcp tools ls` works). The
app surfaces configured MCP servers in Settings, but browser JavaScript never
receives the Docker MCP bearer token. Live MCP execution is routed:

```text
browser -> UI FastAPI server -> Docker MCP gateway on the host
```

On this machine the working Docker MCP profile is `p16`; it exposes Playwright
tools such as `browser_navigate`, `browser_snapshot`, and
`browser_console_messages`. Verify it with:

```powershell
docker mcp profile list
docker mcp tools ls --gateway-arg "--profile=p16"
```

Run a Docker MCP gateway on the host with the same bearer token that is in
`.env`:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\mcp\start-mcp-gateway.ps1
```

For a UI container on Docker Desktop, use this URL:

```env
MCP_GATEWAY_URL=http://host.docker.internal:8811/sse
MCP_GATEWAY_AUTH_TOKEN=<token printed by docker mcp gateway run>
MCP_ALLOWED_TOOLS=mcp-find,mcp-add,mcp-remove,mcp-exec,mcp-config-set,mcp-create-profile,mcp-activate-profile,browser_navigate,browser_snapshot,browser_console_messages,browser_network_requests,browser_network_request,browser_take_screenshot,browser_wait_for,browser_click,browser_type,browser_select_option,browser_press_key,browser_fill_form,search_nodes,open_nodes,create_entities,create_relations,add_observations,sequentialthinking
```

Restart the UI after changing these values:

```bash
docker compose -f docker-compose.local.yml up -d --build ui
```

Verify the full MCP path from host to UI proxy:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\mcp\check-mcp-health.ps1
```

Run the full MCP QA audit, including a memory write/read, a separate-session
read, optional gateway restart persistence, and a sequentialthinking smoke
call:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\mcp\qa-mcp-audit.ps1 -EnvPath .\.env -RestartGateway
```

The Docker MCP Memory server persists through the named Docker volume
`claude-memory:/app/dist`. The gateway starts memory containers with `--rm`, so
the containers are disposable, but `memory.json` survives as long as the
`claude-memory` volume is not removed.

When `MCP_GATEWAY_URL` is configured, MCP is enabled by default and is visible in
the Tools modal as `Docker MCP / Playwright`; the same switch also appears in
Settings as `Allow model to call MCP tools`. The current allowlist gives the
assistant Docker MCP discovery/profile tools plus basic Playwright browsing
controls. Memory tools are exposed twice on purpose: as direct first-class tools
(`search_nodes`, `open_nodes`, `create_entities`, `add_observations`,
`create_relations`) for reliable local-model use, and through the generic
`mcp_call` wrapper for ordered batches such as write-then-verify. Bulgarian
memory recall should search both Cyrillic and Latin/transliterated forms before
concluding no memory exists. `browser_run_code_unsafe`, arbitrary evaluate, file
upload, and drop tools are intentionally not allowlisted.

## VRAM Notes

The current machine reports a 16 GB NVIDIA GPU. Gemma 4 12B QAT in LM Studio is plausible on 16 GB, but running Gemma, Parakeet STT, and Qwen3-TTS all on the same GPU is tight. Best starting point is to let LM Studio own the LLM and keep Docker to the audio stack; close other GPU-heavy apps before starting if model loading fails.
