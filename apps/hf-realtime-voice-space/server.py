"""
Tiny server for the speech-to-speech demo.

The demo used to ship as a `sdk: static` Space, but the web-search tool needs a
search key the browser must NOT see. A static Space has no runtime process, so it
can't hold a secret the front-end uses. This server fixes that: it serves the
unchanged front-end AND exposes a same-origin `/api/search` proxy that holds the
search provider key server-side (see docs/adr/0001).

Everything lives in one container; the speech-to-speech backend stays a separate,
load-balanced service the browser talks to over WebSocket as before. The load
balancer's address is a secret too (like the search key): the browser never sees
it. `/api/session` proxies the session handshake server-side so only the
per-session compute URL the LB hands back (which the browser must dial) is exposed.

On the deployed Space the server also meters conversation time by HF login tier
(anonymous / signed-in / PRO) — see `limiter.py` and `auth.py`. That whole feature
is off unless BOTH `LOAD_BALANCER_URL` and `SPACE_ID` are set, so it runs only on
the live Space, never locally (even with the LB exported for testing).

Endpoints:
  GET  /api/config           -> { search, lb, allowDirect, auth }
  GET  /api/me               -> login + tier + remaining budget (LB mode only)
  POST /api/search           -> { results, answer }  Search via Tavily or Serper
  POST /api/session          -> proxies <LB>/session: a grant, or a queue ticket
  GET  /api/queue/{id}       -> proxies <LB>/queue/{id}: position, or a grant on claim
  DELETE /api/queue/{id}     -> leave the queue (explicit "Leave queue" button)
  POST /api/queue/end        -> leave the queue (sendBeacon on teardown)
  POST /api/session/heartbeat-> extend the reservation; { expired }
  POST /api/session/end      -> reconcile + refund (sendBeacon on teardown)
  /*                         -> static files (index.html, main.js, ...)

When every compute slot is busy the load balancer hands back a queue ticket
instead of a grant; the browser polls /api/queue/{id} until it reaches the front
and a slot frees. Waiting reserves nothing — the daily budget is only reserved at
the moment a slot is actually claimed (a grant), never while queued.
"""

import asyncio
import json
import logging
import os
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
import limiter

logger = logging.getLogger("s2s.search")


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("invalid %s=%r, using %s", name, raw, default)
        return default
    return max(minimum, min(maximum, value))


def _bounded_float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("invalid %s=%r, using %s", name, raw, default)
        return default
    return max(minimum, min(maximum, value))


SERPER_KEY = os.environ.get("SERPER_API_KEY", "").strip()
TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "").strip()
SEARCH_PROVIDER = os.environ.get("SEARCH_PROVIDER", "").strip().lower()
# Speech-to-speech load balancer URL. When set, the browser POSTs /api/session
# (which proxies <lb>/session here, server-side) and connects to the URL the LB
# returns (the original flow). The LB address itself is never sent to the browser.
# When empty, the user may instead set a direct s2s server URL in Settings and the
# browser connects to it straight (no load balancer).
LOAD_BALANCER_URL = os.environ.get("LOAD_BALANCER_URL", "").strip()
# Optional default direct backend URL for local/docker runs. This is not a
# secret; it is only used to prefill Settings when no load balancer is configured.
DIRECT_S2S_URL = os.environ.get("DIRECT_S2S_URL", "").strip()
SAME_ORIGIN_S2S_PROXY = os.environ.get("SAME_ORIGIN_S2S_PROXY", "auto").strip().lower()
SAME_ORIGIN_S2S_PATH = os.environ.get("SAME_ORIGIN_S2S_PATH", "/s2s/v1/realtime").strip()
SAME_ORIGIN_BG_S2S_PATH = os.environ.get("SAME_ORIGIN_BG_S2S_PATH", "/s2s-bg/v1/realtime").strip()
# HF injects SPACE_ID ("owner/space") into every Space runtime; it's absent
# locally and on a plain `docker run`. We meter conversation time ONLY on the
# deployed Space — i.e. when BOTH the LB is configured AND we're on a Space.
# Off-Space (local dev, even with the LB exported) the app still proxies the LB,
# but nothing is metered: no budget, no reservations, no sign-in gating.
SPACE_ID = os.environ.get("SPACE_ID", "").strip()
LIMITER_ENABLED = bool(LOAD_BALANCER_URL) and bool(SPACE_ID)
SERPER_URL = "https://google.serper.dev/search"
TAVILY_URL = "https://api.tavily.com/search"
MCP_GATEWAY_URL = os.environ.get("MCP_GATEWAY_URL", "").strip()
MCP_GATEWAY_AUTH_TOKEN = os.environ.get("MCP_GATEWAY_AUTH_TOKEN", "").strip()
MCP_ALLOWED_TOOLS = {
    item.strip()
    for item in os.environ.get(
        "MCP_ALLOWED_TOOLS",
        "mcp-find,mcp-add,mcp-remove,mcp-exec,mcp-config-set,mcp-create-profile,mcp-activate-profile,browser_navigate,browser_snapshot,browser_console_messages,browser_network_requests,browser_network_request,browser_take_screenshot,browser_wait_for,browser_click,browser_type,browser_select_option,browser_press_key,browser_fill_form,search_nodes,open_nodes,create_entities,create_relations,add_observations,sequentialthinking",
    ).split(",")
    if item.strip()
}
MCP_MAX_CALLS = _bounded_int_env("MCP_MAX_CALLS", 5, 1, 10)
MCP_MAX_ARGUMENT_BYTES = _bounded_int_env("MCP_MAX_ARGUMENT_BYTES", 8192, 512, 65536)
MCP_REQUEST_TIMEOUT_S = _bounded_float_env("MCP_REQUEST_TIMEOUT_S", 20.0, 2.0, 120.0)
MCP_CONNECT_TIMEOUT_S = _bounded_float_env("MCP_CONNECT_TIMEOUT_S", 5.0, 0.5, 30.0)
MCP_REQUEST_LOCK = asyncio.Lock()
# Cap results so the tool output stays small enough to feed back to the model.
MAX_RESULTS = 5
HERE = os.path.dirname(os.path.abspath(__file__))


def _runtime_env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _parse_backend_presets() -> list[dict[str, str]]:
    raw = os.environ.get("BACKEND_PRESETS", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("invalid BACKEND_PRESETS JSON: %s", exc)
        return []
    if not isinstance(data, list):
        logger.warning("BACKEND_PRESETS must be a JSON list")
        return []

    presets: list[dict[str, str]] = []
    allowed = {"id", "label", "url", "llmProvider", "llmModel", "stt", "tts"}
    for item in data:
        if not isinstance(item, dict):
            continue
        preset = {key: str(item.get(key, "")).strip() for key in allowed}
        if not preset["id"] and preset["label"]:
            preset["id"] = preset["label"].lower().replace(" ", "-")
        if preset["label"] and (preset["url"] or preset["id"] == "custom"):
            presets.append(preset)
    return presets


def _parse_llm_providers() -> list[dict[str, object]]:
    raw = os.environ.get("LLM_PROVIDERS_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("invalid LLM_PROVIDERS_JSON JSON: %s", exc)
        return []
    if not isinstance(data, list):
        logger.warning("LLM_PROVIDERS_JSON must be a JSON list")
        return []

    providers: list[dict[str, object]] = []
    for provider in data:
        if not isinstance(provider, dict):
            continue
        provider_id = str(provider.get("id", "")).strip()
        label = str(provider.get("label", "")).strip() or provider_id
        api_key_env = str(provider.get("apiKeyEnv", "")).strip()
        configured = bool(os.environ.get(api_key_env, "").strip()) if api_key_env else True
        models: list[dict[str, str]] = []
        for model in provider.get("models") or []:
            if not isinstance(model, dict):
                continue
            model_id = str(model.get("id", "")).strip()
            if not model_id:
                continue
            models.append(
                {
                    "id": model_id,
                    "label": str(model.get("label", "")).strip() or model_id,
                    "selector": f"{provider_id}::{model_id}",
                }
            )
        if provider_id and models:
            providers.append(
                {
                    "id": provider_id,
                    "label": label,
                    "configured": configured,
                    "requiresKey": bool(api_key_env),
                    "models": models,
                }
            )
    return providers


def _parse_mcp_servers() -> list[dict[str, object]]:
    raw = os.environ.get("MCP_SERVERS_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("invalid MCP_SERVERS_JSON JSON: %s", exc)
        return []
    return data if isinstance(data, list) else []


BACKEND_PRESETS = _parse_backend_presets()
LLM_PROVIDERS = _parse_llm_providers()
MCP_SERVERS = _parse_mcp_servers()
RUNTIME_STACK = {
    key: value
    for key, value in {
        "activeBackend": _runtime_env("ACTIVE_BACKEND"),
        "backendLabel": _runtime_env("BACKEND_LABEL"),
        "llmProvider": _runtime_env("LLM_PROVIDER"),
        "llmModel": _runtime_env("LLM_MODEL"),
        "stt": _runtime_env("STT_MODEL"),
        "tts": _runtime_env("TTS_MODEL"),
    }.items()
    if value
}


def _ensure_leading_slash(path: str) -> str:
    clean = (path or "").strip()
    if not clean:
        return ""
    return clean if clean.startswith("/") else f"/{clean}"


def _external_origin(request: Request) -> str:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
        or ""
    ).split(",")[0].strip()
    if not host:
        return ""
    return f"{proto}://{host}"


def _same_origin_proxy_enabled(request: Request) -> bool:
    if LOAD_BALANCER_URL:
        return False
    if SAME_ORIGIN_S2S_PROXY in {"0", "false", "no", "off", "disabled"}:
        return False
    if SAME_ORIGIN_S2S_PROXY in {"1", "true", "yes", "on", "enabled"}:
        return True
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").split(",")[0].strip()
    return proto == "https"


def _same_origin_url(origin: str, path: str) -> str:
    clean_path = _ensure_leading_slash(path)
    return f"{origin.rstrip('/')}{clean_path}" if origin and clean_path else ""


def _direct_config_for_request(request: Request) -> tuple[str, list[dict[str, object]]]:
    presets: list[dict[str, object]] = [dict(preset) for preset in BACKEND_PRESETS]
    direct_url = DIRECT_S2S_URL
    if not _same_origin_proxy_enabled(request):
        return direct_url, presets

    origin = _external_origin(request)
    lmstudio_url = _same_origin_url(origin, SAME_ORIGIN_S2S_PATH)
    bgtts_url = _same_origin_url(origin, SAME_ORIGIN_BG_S2S_PATH)
    route_by_id = {"lmstudio": lmstudio_url, "lmstudio-bgtts": bgtts_url}

    for preset in presets:
        preset_id = str(preset.get("id", "")).strip()
        routed_url = route_by_id.get(preset_id, "")
        if not routed_url:
            continue
        aliases = []
        old_url = str(preset.get("url", "")).strip()
        if old_url and old_url != routed_url:
            aliases.append(old_url)
        preset["url"] = routed_url
        if aliases:
            preset["aliases"] = aliases

    active_backend = _runtime_env("ACTIVE_BACKEND") or "lmstudio"
    active_url = route_by_id.get(active_backend) or lmstudio_url
    return active_url or direct_url, presets

app = FastAPI(title="s2s-demo")


@app.middleware("http")
async def _no_cache_ui_assets(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if not path.startswith("/api/") and not path.startswith("/s2s"):
        response.headers["Cache-Control"] = "no-store"
    return response


# Wire HF OAuth before the app serves (no-op unless the OAuth env is present).
# Sign-in only matters when we're metering (prod Space), so gate it on that.
AUTH_ENABLED = LIMITER_ENABLED and auth.attach(app)


@app.on_event("startup")
async def _startup():
    """Stand up the usage DB and a periodic sweeper — metered (prod Space) only."""
    if not LIMITER_ENABLED:
        return
    limiter.init()
    asyncio.create_task(_sweeper())


async def _sweeper():
    while True:
        await asyncio.sleep(limiter.REAP_AFTER_SEC)
        try:
            await asyncio.to_thread(limiter.sweep)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("usage sweep failed: %r", exc)


class SearchRequest(BaseModel):
    query: str
    # Optional user-supplied key (fallback when the deploy has no server key).
    # Used for this request only; never stored.
    key: str | None = None
    provider: str | None = None


class McpCallRequest(BaseModel):
    name: str | None = None
    arguments: dict | None = None
    calls: list[dict] | None = None


class ProviderModelLoadRequest(BaseModel):
    model: str
    context_length: int | None = None


def _default_search_provider() -> str:
    if SEARCH_PROVIDER in {"tavily", "serper"}:
        return SEARCH_PROVIDER
    if TAVILY_KEY:
        return "tavily"
    if SERPER_KEY:
        return "serper"
    return ""


def _search_provider_and_key(req: SearchRequest) -> tuple[str, str]:
    provider = (req.provider or "").strip().lower()
    if provider not in {"tavily", "serper"}:
        provider = _default_search_provider() or "tavily"

    user_key = (req.key or "").strip()
    if user_key:
        return provider, user_key

    if provider == "tavily":
        return provider, TAVILY_KEY
    if provider == "serper":
        return provider, SERPER_KEY
    return provider, ""


def _search_error(provider: str, status_code: int, body: str) -> dict[str, object]:
    logger.warning("%s search error %s: %s", provider, status_code, body[:300])
    msg = None
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            raw_msg = parsed.get("message") or parsed.get("error") or parsed.get("detail")
            if isinstance(raw_msg, dict):
                raw_msg = raw_msg.get("message") or raw_msg.get("detail")
            msg = str(raw_msg).strip() if raw_msg else None
    except Exception:
        pass
    detail = f"{provider.title()} search provider error ({status_code})"
    if msg:
        detail += f": {msg}"
    return {"error": detail, "results": [], "provider": provider}


async def _search_tavily(query: str, key: str) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "query": query,
        "search_depth": "basic",
        "include_answer": True,
        "max_results": MAX_RESULTS,
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as http:
            resp = await http.post(TAVILY_URL, headers=headers, json=payload)
    except httpx.RequestError as exc:
        logger.warning("Tavily unreachable: %r", exc)
        return {"error": "Tavily search provider unreachable.", "results": [], "provider": "tavily"}

    if resp.status_code != 200:
        return _search_error("tavily", resp.status_code, resp.text)

    data = resp.json()
    results = []
    for item in (data.get("results") or [])[:MAX_RESULTS]:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": item.get("title", ""),
                "snippet": item.get("content", "") or item.get("snippet", ""),
                "url": item.get("url", ""),
            }
        )
    return {
        "query": query,
        "answer": data.get("answer") or None,
        "results": results,
        "provider": "tavily",
    }


async def _search_serper(query: str, key: str) -> dict[str, object]:
    headers = {"X-API-KEY": key, "Content-Type": "application/json"}
    payload = {"q": query, "num": MAX_RESULTS}
    try:
        async with httpx.AsyncClient(timeout=12.0) as http:
            resp = await http.post(SERPER_URL, headers=headers, json=payload)
    except httpx.RequestError as exc:
        logger.warning("Serper unreachable: %r", exc)
        return {"error": "Serper search provider unreachable.", "results": [], "provider": "serper"}

    if resp.status_code != 200:
        return _search_error("serper", resp.status_code, resp.text)

    data = resp.json()
    results = []
    for item in (data.get("organic") or [])[:MAX_RESULTS]:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "url": item.get("link", ""),
            }
        )

    box = data.get("answerBox") or {}
    answer = box.get("answer") or box.get("snippet") or None
    if not answer:
        kg = data.get("knowledgeGraph") or {}
        answer = kg.get("description") or None

    return {"query": query, "answer": answer, "results": results, "provider": "serper"}


@app.get("/api/config")
async def config(request: Request):
    """Client bootstrap: whether web search is available, whether the deploy runs
    behind a load balancer (so the browser uses the /api/session proxy + limiter),
    whether HF sign-in is available, and whether the user may instead set a direct
    s2s server URL. The LB address itself is intentionally NOT included."""
    mcp_health = await _mcp_health_snapshot(timeout_s=0.8)
    direct_url, backend_presets = _direct_config_for_request(request)
    return {
        "search": bool(TAVILY_KEY or SERPER_KEY),
        "searchProvider": _default_search_provider(),
        "lb": bool(LOAD_BALANCER_URL),
        "allowDirect": not LOAD_BALANCER_URL,
        "directUrl": "" if LOAD_BALANCER_URL else direct_url,
        "backendPresets": [] if LOAD_BALANCER_URL else backend_presets,
        "llmProviders": LLM_PROVIDERS,
        "defaultLlmProvider": _runtime_env("DEFAULT_LLM_PROVIDER") or "lmstudio",
        "defaultLlmModel": _runtime_env("DEFAULT_LLM_MODEL") or _runtime_env("LM_STUDIO_MODEL"),
        "mcp": {
            "servers": MCP_SERVERS,
            "configured": bool(MCP_GATEWAY_URL),
            "healthy": bool(mcp_health["healthy"]),
            "status": mcp_health["status"],
            "detail": mcp_health["detail"],
            "allowedTools": sorted(MCP_ALLOWED_TOOLS),
        },
        "runtime": RUNTIME_STACK,
        "auth": AUTH_ENABLED,
    }


@app.get("/api/me")
async def me(request: Request):
    """Login state, tier, and remaining daily budget. Only meaningful in LB mode;
    sets the anonymous tracking cookie when first seen."""
    if not LIMITER_ENABLED:
        return {"enabled": False}
    view = auth.user_view(request)
    tier, keys, set_cookie = auth.resolve_identity(request)
    unlimited = limiter.budget_for(tier) is None
    rem = None if unlimited else await asyncio.to_thread(limiter.remaining, keys, tier)
    out = {
        "enabled": True,
        "auth": AUTH_ENABLED,
        **view,
        "remainingSec": rem,
        "limitSec": limiter.budget_for(tier),
        "loginUrl": auth.OAUTH_LOGIN_PATH if AUTH_ENABLED else None,
        "logoutUrl": auth.OAUTH_LOGOUT_PATH if AUTH_ENABLED else None,
    }
    resp = JSONResponse(out)
    if set_cookie:
        auth.set_anon_cookie(resp, set_cookie)
    return resp


@app.post("/api/search")
async def search(req: SearchRequest):
    """Proxy web search via the configured provider. The key stays on the server
    unless the user brought their own (then theirs is used for this request only)."""
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Empty query.")

    provider, key = _search_provider_and_key(req)
    if not key:
        # No server key and the user didn't supply one — search is unavailable.
        raise HTTPException(status_code=503, detail="Search is not configured.")

    result = await (_search_tavily(query, key) if provider == "tavily" else _search_serper(query, key))
    return JSONResponse(result)

    # A direct answer when Google has one — saves the model a hop.
def _raw_llm_providers() -> list[dict]:
    raw = os.environ.get("LLM_PROVIDERS_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _raw_llm_provider(provider_id: str) -> dict:
    for provider in _raw_llm_providers():
        if str(provider.get("id", "")).strip() == provider_id:
            return provider
    raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")


def _provider_headers(provider: dict) -> dict[str, str]:
    api_key_env = str(provider.get("apiKeyEnv", "")).strip()
    api_key = os.environ.get(api_key_env, "").strip() if api_key_env else ""
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _provider_base_url(provider: dict) -> str:
    base_url = str(provider.get("baseUrl", "")).strip()
    if not base_url:
        raise HTTPException(status_code=503, detail="Provider base URL is not configured.")
    return base_url.rstrip("/")


def _lmstudio_native_base_url(openai_base_url: str) -> str:
    parsed = urlsplit(openai_base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/api/v1", "", "")).rstrip("/")


def _static_provider_models(provider: dict, provider_id: str) -> list[dict[str, object]]:
    models: list[dict[str, object]] = []
    for model in provider.get("models") or []:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id", "")).strip()
        if not model_id:
            continue
        models.append(
            {
                "id": model_id,
                "label": str(model.get("label", "")).strip() or model_id,
                "selector": f"{provider_id}::{model_id}",
                "source": "configured",
            }
        )
    return models


def _lmstudio_model_from_native(item: dict, provider_id: str) -> dict[str, object] | None:
    if item.get("type") and item.get("type") != "llm":
        return None
    model_id = str(item.get("key") or item.get("id") or "").strip()
    if not model_id:
        return None
    quant = item.get("quantization") if isinstance(item.get("quantization"), dict) else {}
    quant_name = str(quant.get("name", "")).strip()
    display = str(item.get("display_name") or model_id).strip()
    parts = [display]
    if item.get("params_string"):
        parts.append(str(item["params_string"]))
    if quant_name:
        parts.append(quant_name)
    loaded_instances = item.get("loaded_instances") if isinstance(item.get("loaded_instances"), list) else []
    return {
        "id": model_id,
        "label": " / ".join(parts),
        "selector": f"{provider_id}::{model_id}",
        "source": "lmstudio",
        "loaded": len(loaded_instances) > 0,
        "sizeBytes": item.get("size_bytes"),
        "contextLength": item.get("max_context_length"),
        "format": item.get("format"),
    }


def _is_provider_chat_model(provider_id: str, model_id: str) -> bool:
    lower = model_id.lower()
    if "embedding" in lower:
        return False
    if provider_id != "gemini":
        return True

    non_chat_markers = (
        "imagen",
        "image",
        "veo",
        "lyria",
        "tts",
        "native-audio",
        "live",
        "aqa",
        "robotics",
        "computer-use",
        "deep-research",
        "antigravity",
    )
    if any(marker in lower for marker in non_chat_markers):
        return False
    return "gemini" in lower or "gemma" in lower


async def _fetch_openai_models(provider: dict, provider_id: str) -> list[dict[str, object]]:
    base_url = _provider_base_url(provider)
    async with httpx.AsyncClient(timeout=8.0) as http:
        resp = await http.get(f"{base_url}/models", headers=_provider_headers(provider))
        resp.raise_for_status()
        data = resp.json()
    models = []
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id", "")).strip()
        if not model_id:
            continue
        if not _is_provider_chat_model(provider_id, model_id):
            continue
        models.append(
            {
                "id": model_id,
                "label": model_id,
                "selector": f"{provider_id}::{model_id}",
                "source": "openai-compatible",
            }
        )
    return models


async def _fetch_lmstudio_models(provider: dict, provider_id: str) -> list[dict[str, object]]:
    base_url = _provider_base_url(provider)
    headers = _provider_headers(provider)
    native_url = _lmstudio_native_base_url(base_url)
    async with httpx.AsyncClient(timeout=8.0) as http:
        native_resp = await http.get(f"{native_url}/models", headers=headers)
        if native_resp.status_code == 200:
            native_data = native_resp.json()
            models = [
                parsed
                for item in native_data.get("models") or []
                if isinstance(item, dict)
                for parsed in [_lmstudio_model_from_native(item, provider_id)]
                if parsed
            ]
            if models:
                return models
        return await _fetch_openai_models(provider, provider_id)


def _merge_models(static_models: list[dict[str, object]], discovered: list[dict[str, object]]) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for model in discovered + static_models:
        model_id = str(model.get("id", "")).strip()
        if model_id and model_id not in merged:
            merged[model_id] = model
    return list(merged.values())


@app.get("/api/providers/{provider_id}/models")
async def provider_models(provider_id: str):
    """Return server-side-discovered models without exposing provider credentials."""
    provider = _raw_llm_provider(provider_id)
    static_models = _static_provider_models(provider, provider_id)
    try:
        discovered = (
            await _fetch_lmstudio_models(provider, provider_id)
            if provider_id == "lmstudio"
            else await _fetch_openai_models(provider, provider_id)
        )
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300] if exc.response is not None else str(exc)
        logger.warning("provider model listing failed for %s: %s", provider_id, detail)
        if static_models:
            return {"provider": provider_id, "models": static_models, "error": "Provider model listing failed."}
        raise HTTPException(status_code=502, detail="Provider model listing failed.")
    except httpx.RequestError as exc:
        logger.warning("provider model listing unreachable for %s: %r", provider_id, exc)
        if static_models:
            return {"provider": provider_id, "models": static_models, "error": "Provider model listing unreachable."}
        raise HTTPException(status_code=502, detail="Provider model listing unreachable.")
    return {"provider": provider_id, "models": _merge_models(static_models, discovered)}


@app.post("/api/providers/{provider_id}/models/load")
async def provider_model_load(provider_id: str, req: ProviderModelLoadRequest):
    """Load an LM Studio model before the speech backend sends the first LLM turn."""
    model_id = (req.model or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="Model id is required.")
    provider = _raw_llm_provider(provider_id)
    if provider_id != "lmstudio":
        return {"provider": provider_id, "model": model_id, "loaded": False, "skipped": True}

    base_url = _provider_base_url(provider)
    headers = {**_provider_headers(provider), "Content-Type": "application/json"}
    native_url = _lmstudio_native_base_url(base_url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=5.0)) as http:
        try:
            models_resp = await http.get(f"{native_url}/models", headers=_provider_headers(provider))
            if models_resp.status_code == 200:
                for item in models_resp.json().get("models") or []:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("key") or "").strip() != model_id:
                        continue
                    loaded = item.get("loaded_instances") if isinstance(item.get("loaded_instances"), list) else []
                    if loaded:
                        return {"provider": provider_id, "model": model_id, "loaded": True, "alreadyLoaded": True}

            payload: dict[str, object] = {"model": model_id}
            if req.context_length:
                payload["context_length"] = req.context_length
            load_resp = await http.post(f"{native_url}/models/load", headers=headers, json=payload)
            load_resp.raise_for_status()
            body = load_resp.json() if load_resp.content else {}
            return {"provider": provider_id, "model": model_id, "loaded": True, "response": body}
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:300] if exc.response is not None else str(exc)
            raise HTTPException(status_code=502, detail=f"LM Studio model load failed: {detail}")
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"LM Studio model API unreachable: {exc}")


def _safe_detail(value: object, limit: int = 300) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def _json_byte_len(value: object) -> int:
    try:
        return len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        )
    except Exception:
        return MCP_MAX_ARGUMENT_BYTES + 1


def _require_http_url(value: object, field_name: str) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail=f"{field_name} must be an http(s) URL.")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail=f"{field_name} must not include credentials.")
    return url


def _safe_screenshot_filename(value: object) -> str:
    filename = str(value or "").strip()
    if not filename:
        return ""
    if len(filename) > 120:
        raise HTTPException(status_code=400, detail="Screenshot filename is too long.")
    if filename != os.path.basename(filename) or any(part in filename for part in ("\\", "/", ":")):
        raise HTTPException(status_code=400, detail="Screenshot filename must be a simple file name.")
    return filename


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _memory_property_observations(properties: object) -> list[str]:
    if not isinstance(properties, dict):
        return []
    observations = []
    for key, value in properties.items():
        clean_key = str(key).strip()
        if not clean_key:
            continue
        if isinstance(value, (dict, list)):
            clean_value = json.dumps(value, ensure_ascii=False)
        else:
            clean_value = str(value).strip()
        if clean_value:
            observations.append(f"{clean_key}: {clean_value}")
    return observations


def _normalize_memory_entity(entity: object) -> dict[str, object]:
    if not isinstance(entity, dict):
        raise HTTPException(status_code=400, detail="Memory entity must be an object.")
    name = str(entity.get("name") or entity.get("entityName") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Memory entity name is required.")
    entity_type = str(entity.get("entityType") or entity.get("type") or entity.get("category") or "memory").strip()
    observations = _string_list(entity.get("observations") or entity.get("contents") or entity.get("notes"))
    observations.extend(_memory_property_observations(entity.get("properties")))
    return {
        "name": name,
        "entityType": entity_type or "memory",
        "observations": observations,
    }


def _normalize_memory_relation(relation: object) -> dict[str, str]:
    if not isinstance(relation, dict):
        raise HTTPException(status_code=400, detail="Memory relation must be an object.")
    source = str(relation.get("from") or relation.get("source") or relation.get("subject") or "").strip()
    target = str(relation.get("to") or relation.get("target") or relation.get("object") or "").strip()
    relation_type = str(relation.get("relationType") or relation.get("relation") or relation.get("type") or "").strip()
    if not source or not target or not relation_type:
        raise HTTPException(status_code=400, detail="Memory relation requires from, to, and relationType.")
    return {"from": source, "to": target, "relationType": relation_type}


def _normalize_memory_observation(observation: object) -> dict[str, object]:
    if not isinstance(observation, dict):
        raise HTTPException(status_code=400, detail="Memory observation must be an object.")
    entity_name = str(observation.get("entityName") or observation.get("name") or observation.get("entity") or "").strip()
    contents = _string_list(
        observation.get("contents")
        or observation.get("observations")
        or observation.get("addedObservations")
        or observation.get("content")
        or observation.get("text")
    )
    if not entity_name or not contents:
        raise HTTPException(status_code=400, detail="Memory observation requires entityName and contents.")
    return {"entityName": entity_name, "contents": contents}


def _normalize_memory_arguments(name: str, arguments: dict) -> dict:
    clean = dict(arguments)
    if name == "create_entities":
        entities = clean.get("entities")
        if not isinstance(entities, list):
            raise HTTPException(status_code=400, detail="create_entities requires an entities array.")
        clean["entities"] = [_normalize_memory_entity(entity) for entity in entities]
    elif name == "create_relations":
        relations = clean.get("relations")
        if not isinstance(relations, list):
            raise HTTPException(status_code=400, detail="create_relations requires a relations array.")
        clean["relations"] = [_normalize_memory_relation(relation) for relation in relations]
    elif name == "add_observations":
        observations = clean.get("observations")
        if not isinstance(observations, list):
            raise HTTPException(status_code=400, detail="add_observations requires an observations array.")
        clean["observations"] = [_normalize_memory_observation(observation) for observation in observations]
    elif name == "open_nodes":
        clean["names"] = _string_list(clean.get("names") or clean.get("name"))
        if not clean["names"]:
            raise HTTPException(status_code=400, detail="open_nodes requires names.")
    elif name == "search_nodes":
        query = str(clean.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="search_nodes requires query.")
        clean["query"] = query
    return clean


def _normalize_sequentialthinking_arguments(arguments: dict) -> dict:
    clean = dict(arguments)
    aliases = {
        "next_thought_needed": "nextThoughtNeeded",
        "thought_number": "thoughtNumber",
        "total_thoughts": "totalThoughts",
        "is_revision": "isRevision",
        "revises_thought": "revisesThought",
        "branch_from_thought": "branchFromThought",
        "branch_id": "branchId",
        "needs_more_thoughts": "needsMoreThoughts",
    }
    for source, target in aliases.items():
        if source in clean and target not in clean:
            clean[target] = clean.pop(source)
    for field_name in ("thoughtNumber", "totalThoughts", "revisesThought", "branchFromThought"):
        if field_name in clean and clean[field_name] is not None:
            try:
                clean[field_name] = int(clean[field_name])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"sequentialthinking {field_name} must be a number.")
    if "thought" not in clean or not str(clean.get("thought") or "").strip():
        raise HTTPException(status_code=400, detail="sequentialthinking requires thought.")
    if "thoughtNumber" not in clean:
        raise HTTPException(status_code=400, detail="sequentialthinking requires thoughtNumber.")
    if "totalThoughts" not in clean:
        raise HTTPException(status_code=400, detail="sequentialthinking requires totalThoughts.")
    if "nextThoughtNeeded" not in clean:
        clean["nextThoughtNeeded"] = False
    return clean


def _clean_mcp_arguments(name: str, arguments: dict | None) -> dict:
    if arguments is None:
        clean_arguments: dict = {}
    elif isinstance(arguments, dict):
        clean_arguments = dict(arguments)
    else:
        raise HTTPException(status_code=400, detail=f"MCP arguments for {name} must be an object.")

    if _json_byte_len(clean_arguments) > MCP_MAX_ARGUMENT_BYTES:
        raise HTTPException(status_code=400, detail=f"MCP arguments for {name} are too large.")

    if name == "browser_navigate":
        clean_arguments["url"] = _require_http_url(clean_arguments.get("url"), "url")
    elif name == "browser_wait_for":
        raw_time = clean_arguments.get("time", 1)
        try:
            seconds = float(raw_time)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="browser_wait_for time must be a number.")
        clean_arguments["time"] = max(0.0, min(10.0, seconds))
    elif name == "browser_take_screenshot" and clean_arguments.get("filename"):
        clean_arguments["filename"] = _safe_screenshot_filename(clean_arguments.get("filename"))
    elif name in {"create_entities", "create_relations", "add_observations", "open_nodes", "search_nodes"}:
        clean_arguments = _normalize_memory_arguments(name, clean_arguments)
    elif name == "sequentialthinking":
        clean_arguments = _normalize_sequentialthinking_arguments(clean_arguments)

    return clean_arguments


def _sanitize_mcp_tool(tool: object) -> dict[str, object] | None:
    if not isinstance(tool, dict):
        return None
    name = str(tool.get("name", "")).strip()
    if not name:
        return None
    allowed = name in MCP_ALLOWED_TOOLS and not name.endswith("_unsafe")
    description = str(tool.get("description", "")).strip()
    annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
    safe_annotations = {
        key: annotations.get(key)
        for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
        if isinstance(annotations.get(key), (bool, str, int, float))
    }
    return {
        "name": name[:120],
        "description": description[:800] if allowed else "",
        "allowed": allowed,
        "annotations": safe_annotations,
    }


def _mcp_base_and_sse_url() -> tuple[str, str]:
    if not MCP_GATEWAY_URL:
        raise HTTPException(status_code=503, detail="MCP gateway is not configured.")
    gateway_url = MCP_GATEWAY_URL.rstrip("/")
    parsed = urlsplit(gateway_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=503, detail="MCP gateway URL must be an http(s) URL.")
    if gateway_url.endswith("/sse"):
        return gateway_url[:-4], gateway_url
    return gateway_url, f"{gateway_url}/sse"


def _mcp_headers() -> dict[str, str]:
    headers = {"Accept": "application/json, text/event-stream"}
    if MCP_GATEWAY_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {MCP_GATEWAY_AUTH_TOKEN}"
    return headers


async def _read_sse_event(lines) -> tuple[str, str]:
    event = "message"
    data: list[str] = []
    async for line in lines:
        if line == "":
            if data:
                return event, "\n".join(data)
            event = "message"
            data = []
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
    raise HTTPException(status_code=502, detail="MCP gateway stream closed.")


async def _mcp_health_snapshot(*, timeout_s: float = 1.0) -> dict[str, object]:
    if not MCP_GATEWAY_URL:
        return {"healthy": False, "status": "not_configured", "detail": "MCP gateway is not configured."}

    try:
        _base_url, sse_url = _mcp_base_and_sse_url()
        headers = _mcp_headers()
        timeout = httpx.Timeout(timeout_s, connect=min(0.5, timeout_s))
        async with httpx.AsyncClient(timeout=timeout) as http:
            async with http.stream("GET", sse_url, headers=headers) as stream:
                stream.raise_for_status()
                event, _data = await asyncio.wait_for(_read_sse_event(stream.aiter_lines()), timeout=timeout_s)
                if event == "endpoint":
                    return {"healthy": True, "status": "online", "detail": "MCP gateway is online."}
                return {"healthy": True, "status": "online", "detail": f"MCP gateway opened SSE event: {event}."}
    except HTTPException as exc:
        return {"healthy": False, "status": "offline", "detail": str(exc.detail)}
    except httpx.HTTPStatusError as exc:
        detail = _safe_detail(exc.response.text if exc.response is not None else exc)
        return {"healthy": False, "status": "offline", "detail": f"MCP gateway HTTP error: {detail}"}
    except (httpx.RequestError, asyncio.TimeoutError) as exc:
        return {"healthy": False, "status": "offline", "detail": f"MCP gateway unreachable: {_safe_detail(exc)}"}


async def _mcp_requests(
    requests: list[tuple[str, dict | None]],
    *,
    timeout_s: float = MCP_REQUEST_TIMEOUT_S,
) -> list[dict]:
    base_url, sse_url = _mcp_base_and_sse_url()
    headers = _mcp_headers()
    request_id = 1

    async with MCP_REQUEST_LOCK:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=MCP_CONNECT_TIMEOUT_S)) as http:
            try:
                async with http.stream("GET", sse_url, headers=headers) as stream:
                    stream.raise_for_status()
                    lines = stream.aiter_lines()
                    endpoint = ""
                    while not endpoint:
                        event, data = await asyncio.wait_for(_read_sse_event(lines), timeout=timeout_s)
                        if event == "endpoint":
                            endpoint = data
                    post_url = f"{base_url}{endpoint}"

                    async def post_rpc(
                        rpc_method: str,
                        rpc_params: dict | None = None,
                        *,
                        notification: bool = False,
                    ):
                        nonlocal request_id
                        body: dict[str, object] = {"jsonrpc": "2.0", "method": rpc_method}
                        if not notification:
                            body["id"] = request_id
                            request_id += 1
                        if rpc_params is not None:
                            body["params"] = rpc_params
                        resp = await http.post(
                            post_url,
                            headers={**headers, "Content-Type": "application/json"},
                            json=body,
                        )
                        resp.raise_for_status()
                        return body.get("id")

                    async def read_response(target_id: int) -> dict:
                        while True:
                            _event, data = await _read_sse_event(lines)
                            try:
                                message = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            if message.get("id") == target_id:
                                if message.get("error"):
                                    raise HTTPException(
                                        status_code=502,
                                        detail=f"MCP error: {_safe_detail(message['error'])}",
                                    )
                                return message.get("result") or {}

                    init_id = await post_rpc(
                        "initialize",
                        {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "hf-realtime-voice-ui", "version": "local"},
                        },
                    )
                    if not isinstance(init_id, int):
                        raise HTTPException(status_code=502, detail="MCP initialize request id missing.")
                    await asyncio.wait_for(read_response(init_id), timeout=timeout_s)
                    await post_rpc("notifications/initialized", notification=True)

                    results = []
                    for method, params in requests:
                        call_id = await post_rpc(method, params or {})
                        if not isinstance(call_id, int):
                            raise HTTPException(status_code=502, detail="MCP tool request id missing.")
                        results.append(await asyncio.wait_for(read_response(call_id), timeout=timeout_s))
                    return results
            except httpx.HTTPStatusError as exc:
                detail = _safe_detail(exc.response.text if exc.response is not None else exc)
                raise HTTPException(status_code=502, detail=f"MCP gateway HTTP error: {detail}")
            except httpx.RequestError as exc:
                raise HTTPException(status_code=502, detail=f"MCP gateway unreachable: {_safe_detail(exc)}")
            except asyncio.TimeoutError:
                raise HTTPException(status_code=504, detail="MCP gateway timed out.")


async def _mcp_request(method: str, params: dict | None = None, *, timeout_s: float = MCP_REQUEST_TIMEOUT_S) -> dict:
    results = await _mcp_requests([(method, params)], timeout_s=timeout_s)
    return results[0] if results else {}


def _validate_mcp_tool_call(name: str, arguments: dict | None) -> dict:
    clean_name = (name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="MCP tool name is required.")
    if clean_name.endswith("_unsafe") or clean_name not in MCP_ALLOWED_TOOLS:
        raise HTTPException(status_code=403, detail=f"MCP tool is not allowed: {clean_name}")
    clean_arguments = _clean_mcp_arguments(clean_name, arguments)
    if clean_name == "mcp-exec":
        nested_name = str(clean_arguments.get("name", "")).strip()
        if (
            not nested_name
            or nested_name == "mcp-exec"
            or nested_name.endswith("_unsafe")
            or nested_name not in MCP_ALLOWED_TOOLS
        ):
            raise HTTPException(status_code=403, detail=f"MCP nested tool is not allowed: {nested_name}")
        nested_arguments = _clean_mcp_arguments(nested_name, clean_arguments.get("arguments"))
        clean_arguments["name"] = nested_name
        clean_arguments["arguments"] = nested_arguments
    return {"name": clean_name, "arguments": clean_arguments}


@app.get("/api/mcp/tools")
async def mcp_tools():
    """List MCP tools exposed by the configured Docker MCP gateway."""
    if not MCP_GATEWAY_URL:
        return {
            "configured": False,
            "healthy": False,
            "status": "not_configured",
            "tools": [],
            "allowedTools": sorted(MCP_ALLOWED_TOOLS),
        }
    try:
        result = await _mcp_request("tools/list")
    except HTTPException as exc:
        return {
            "configured": True,
            "healthy": False,
            "status": "offline",
            "error": _safe_detail(exc.detail),
            "tools": [],
            "allowedTools": sorted(MCP_ALLOWED_TOOLS),
        }
    tools = result.get("tools") if isinstance(result, dict) else []
    if not isinstance(tools, list):
        tools = []
    safe_tools = []
    for tool in tools:
        safe_tool = _sanitize_mcp_tool(tool)
        if safe_tool:
            safe_tools.append(safe_tool)
    return {
        "configured": True,
        "healthy": True,
        "status": "online",
        "tools": safe_tools,
        "allowedTools": sorted(MCP_ALLOWED_TOOLS),
    }


@app.get("/api/mcp/health")
async def mcp_health():
    """Cheap gateway health probe for local Docker MCP setup checks."""
    health = await _mcp_health_snapshot(timeout_s=2.0)
    return {
        "configured": bool(MCP_GATEWAY_URL),
        **health,
        "allowedTools": sorted(MCP_ALLOWED_TOOLS),
    }


@app.post("/api/mcp/call")
async def mcp_call(req: McpCallRequest):
    """Call an allowlisted MCP tool through the configured Docker MCP gateway."""
    if req.calls is not None:
        if not req.calls:
            raise HTTPException(status_code=400, detail="At least one MCP call is required.")
        if len(req.calls) > MCP_MAX_CALLS:
            raise HTTPException(status_code=400, detail=f"MCP calls array is limited to {MCP_MAX_CALLS} calls.")
        calls = []
        for raw_call in req.calls:
            if not isinstance(raw_call, dict):
                raise HTTPException(status_code=400, detail="Each MCP call must be an object.")
            call = _validate_mcp_tool_call(str(raw_call.get("name", "")), raw_call.get("arguments"))
            calls.append(("tools/call", call))
        results = await _mcp_requests(calls)
        return {"results": results}

    call = _validate_mcp_tool_call(req.name or "", req.arguments)
    result = await _mcp_request("tools/call", call)
    return {"name": call["name"], "result": result}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    """Serve the SVG favicon for browsers that still probe /favicon.ico."""
    return FileResponse(os.path.join(HERE, "favicon.svg"), media_type="image/svg+xml")


@app.post("/api/session")
async def session(request: Request):
    """Proxy the session handshake to the load balancer, keeping its URL secret,
    and meter conversation time by tier.

    The browser POSTs here (same-origin); we resolve the caller's tier, refuse if
    today's budget is already spent (402), otherwise POST <LOAD_BALANCER_URL>/session
    and relay the JSON back. The LB body carries a per-session `connect_url`
    (compute host + short-lived token) the browser must dial directly — that one
    URL is unavoidably exposed, but the stable load-balancer address is not. On a
    successful grant we reserve the first time chunk against the day's budget."""
    if not LOAD_BALANCER_URL:
        # No LB configured — this deploy is direct-mode only; the browser should
        # never call this. 404 so it's indistinguishable from a missing route.
        raise HTTPException(status_code=404, detail="Not found.")

    tier, keys, set_cookie = auth.resolve_identity(request)
    # Metering runs only on the deployed Space; off-Space the LB still proxies but
    # nothing is tracked. Within metering, unlimited tiers (pro, org) aren't either.
    tracked = LIMITER_ENABLED and limiter.budget_for(tier) is not None

    # Refuse before troubling the LB if the day's budget is already gone. Done
    # here (at enqueue) so we never put a user who can't talk into the queue.
    if tracked:
        rem = await asyncio.to_thread(limiter.remaining, keys, tier)
        if rem is not None and rem <= 0:
            resp = JSONResponse(
                {"tier": tier, "reason": "limit", "remainingSec": 0}, status_code=402
            )
            if set_cookie:
                auth.set_anon_cookie(resp, set_cookie)
            return resp

    url = f"{LOAD_BALANCER_URL.rstrip('/')}/session"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            lb = await http.post(url, headers={"Content-Type": "application/json"}, content="{}")
    except httpx.RequestError as exc:
        logger.warning("Load balancer unreachable: %r", exc)
        raise HTTPException(status_code=502, detail="Speech service unreachable.")

    # The queue is full: the LB replies 503 {state:"at_capacity"}. Relay it as-is
    # so the client shows a soft "try again shortly", not a hard error.
    if lb.status_code == 503:
        body = _safe_json(lb)
        if body.get("state") == "at_capacity":
            resp = JSONResponse({"state": "at_capacity"}, status_code=503)
            if set_cookie:
                auth.set_anon_cookie(resp, set_cookie)
            return resp

    if lb.status_code != 200:
        # The LB's error body may name the reason (e.g. capacity); it carries no
        # secret, so relay a trimmed copy.
        logger.warning("Session handshake failed %s: %s", lb.status_code, lb.text[:300])
        raise HTTPException(status_code=502, detail=f"Session handshake failed ({lb.status_code}).")

    data = lb.json()

    # Busy pool: the LB queued us. Relay the ticket untouched — crucially with NO
    # reservation, so waiting in line never costs the day's budget.
    if data.get("state") == "queued":
        data["tier"] = tier
        resp = JSONResponse(data)
        if set_cookie:
            auth.set_anon_cookie(resp, set_cookie)
        return resp

    # A slot was free: reserve the first chunk now and return the grant.
    return await _finalize_grant(data, keys, tier, tracked, set_cookie)


@app.get("/api/queue/{queue_id}")
async def queue_status(queue_id: str, request: Request):
    """Poll a waiting ticket: relay the position, or — when the head of the line
    claims a freed slot — reserve the budget now and return the grant. Re-checks the
    daily budget at claim, since a multi-minute wait could have spent it elsewhere."""
    if not LOAD_BALANCER_URL:
        raise HTTPException(status_code=404, detail="Not found.")

    tier, keys, set_cookie = auth.resolve_identity(request)
    tracked = LIMITER_ENABLED and limiter.budget_for(tier) is not None

    url = f"{LOAD_BALANCER_URL.rstrip('/')}/queue/{queue_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            lb = await http.get(url)
    except httpx.RequestError as exc:
        logger.warning("Load balancer unreachable: %r", exc)
        raise HTTPException(status_code=502, detail="Speech service unreachable.")

    if lb.status_code == 404:
        # Ticket unknown/expired (reaped after we stopped polling). Tell the client
        # to start over rather than spin.
        resp = JSONResponse({"state": "expired"}, status_code=404)
        if set_cookie:
            auth.set_anon_cookie(resp, set_cookie)
        return resp

    if lb.status_code != 200:
        logger.warning("Queue poll failed %s: %s", lb.status_code, lb.text[:300])
        raise HTTPException(status_code=502, detail=f"Queue poll failed ({lb.status_code}).")

    data = lb.json()

    if data.get("state") == "queued":
        data["tier"] = tier
        resp = JSONResponse(data)
        if set_cookie:
            auth.set_anon_cookie(resp, set_cookie)
        return resp

    # Claimed a slot. Re-check the budget: it may have been spent in another tab
    # during the wait. If so, refuse — the just-claimed slot is now a pending
    # session on the LB and its pending-timeout reaper reclaims it shortly.
    if tracked:
        rem = await asyncio.to_thread(limiter.remaining, keys, tier)
        if rem is not None and rem <= 0:
            resp = JSONResponse(
                {"tier": tier, "reason": "limit", "remainingSec": 0}, status_code=402
            )
            if set_cookie:
                auth.set_anon_cookie(resp, set_cookie)
            return resp

    return await _finalize_grant(data, keys, tier, tracked, set_cookie)


@app.delete("/api/queue/{queue_id}")
async def queue_leave(queue_id: str):
    """Leave the queue from the explicit 'Leave queue' button (a real fetch)."""
    if not LOAD_BALANCER_URL:
        raise HTTPException(status_code=404, detail="Not found.")
    await _lb_leave(queue_id)
    return {"ok": True}


@app.post("/api/queue/end")
async def queue_end(request: Request):
    """Leave the queue on teardown/tab-close (navigator.sendBeacon, which can only
    POST). Body: { queueId }. Best-effort; the LB reaps the ticket on TTL anyway."""
    if not LOAD_BALANCER_URL:
        raise HTTPException(status_code=404, detail="Not found.")
    qid = await _queue_id(request)
    if qid:
        await _lb_leave(qid)
    return {"ok": True}


async def _finalize_grant(data, keys, tier, tracked, set_cookie):
    """Shared grant tail (fast path or queue claim): reserve the first chunk, attach
    the metering fields the client needs, and set the anon cookie."""
    remaining = None
    if tracked and data.get("session_id"):
        await asyncio.to_thread(limiter.begin, data["session_id"], keys, tier)
        remaining = await asyncio.to_thread(limiter.remaining, keys, tier)

    data.update({
        "tier": tier,
        "limited": tracked,
        "remainingSec": remaining,
        "heartbeatSec": limiter.HEARTBEAT_SEC,
    })
    resp = JSONResponse(data)
    if set_cookie:
        auth.set_anon_cookie(resp, set_cookie)
    return resp


async def _lb_leave(queue_id: str) -> None:
    """Best-effort: tell the LB to drop a waiting ticket."""
    url = f"{LOAD_BALANCER_URL.rstrip('/')}/queue/{queue_id}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            await http.delete(url)
    except httpx.RequestError as exc:
        logger.warning("Queue leave failed: %r", exc)


def _safe_json(response) -> dict:
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def _queue_id(request: Request) -> str:
    """Pull `queueId` from a JSON body, tolerating sendBeacon's blob posts."""
    try:
        data = await request.json()
    except Exception:
        return ""
    return (data or {}).get("queueId", "") if isinstance(data, dict) else ""


async def _session_id(request: Request) -> str:
    """Pull `sessionId` from a JSON body, tolerating sendBeacon's blob posts."""
    try:
        data = await request.json()
    except Exception:
        return ""
    return (data or {}).get("sessionId", "") if isinstance(data, dict) else ""


@app.post("/api/session/heartbeat")
async def session_heartbeat(request: Request):
    """Extend the live reservation one chunk at a time. `expired` once the day's
    budget is spent — the client then tears down."""
    if not LIMITER_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    sid = await _session_id(request)
    alive = bool(sid) and await asyncio.to_thread(limiter.heartbeat, sid)
    return {"expired": not alive}


@app.post("/api/session/end")
async def session_end(request: Request):
    """Clean teardown: reconcile to real elapsed time and refund the unused
    chunk. Sent via navigator.sendBeacon, so it must succeed without a response."""
    if not LIMITER_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    sid = await _session_id(request)
    if sid:
        await asyncio.to_thread(limiter.end, sid)
    return {"ok": True}


# Static front-end. Registered last so the /api routes win. `html=True` serves
# index.html at "/". The repo is public anyway, so serving the dir is fine.
app.mount("/", StaticFiles(directory=HERE, html=True), name="static")
