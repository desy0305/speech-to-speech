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
   LM_STUDIO_BASE_URL=http://192.168.0.115:1234/v1
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
MCP_ALLOWED_TOOLS=mcp-find,mcp-add,mcp-remove,mcp-exec,mcp-config-set,mcp-create-profile,mcp-activate-profile,browser_navigate,browser_snapshot,browser_console_messages,browser_network_requests,browser_network_request,browser_take_screenshot,browser_wait_for,browser_click,browser_type,browser_select_option,browser_press_key,browser_fill_form,search_nodes,open_nodes,create_entities,create_relations,add_observations
```

Restart the UI after changing these values:

```bash
docker compose -f docker-compose.local.yml up -d --build ui
```

Verify the full MCP path from host to UI proxy:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\mcp\check-mcp-health.ps1
```

When `MCP_GATEWAY_URL` is configured, MCP is enabled by default and is visible in
the Tools modal as `Docker MCP / Playwright`; the same switch also appears in
Settings as `Allow model to call MCP tools`. The current allowlist gives the
assistant Docker MCP discovery/profile tools plus basic Playwright browsing
controls. `browser_run_code_unsafe`, arbitrary evaluate, file upload, and drop
tools are intentionally not allowlisted.

## VRAM Notes

The current machine reports a 16 GB NVIDIA GPU. Gemma 4 12B QAT in LM Studio is plausible on 16 GB, but running Gemma, Parakeet STT, and Qwen3-TTS all on the same GPU is tight. Best starting point is to let LM Studio own the LLM and keep Docker to the audio stack; close other GPU-heavy apps before starting if model loading fails.
