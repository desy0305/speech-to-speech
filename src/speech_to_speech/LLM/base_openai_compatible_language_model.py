from __future__ import annotations

import logging
import os
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from collections.abc import Iterator
from typing import Any, Optional

import httpx
from nltk import sent_tokenize
from openai import OpenAI
from openai.types.realtime.conversation_item import (
    RealtimeConversationItemAssistantMessage,
    RealtimeConversationItemFunctionCall,
)
from openai.types.realtime.realtime_conversation_item_assistant_message import (
    Content as AssistantContent,
)
from openai.types.responses import ResponseFunctionToolCall
from pydantic import BaseModel, ConfigDict, Field

from speech_to_speech.baseHandler import BaseHandler
from speech_to_speech.LLM.chat import (
    Chat,
    ChatItemError,
    SupportedItem,
    build_active_chat,
    make_system_message,
    make_user_message,
)
from speech_to_speech.LLM.compaction_prompt import CompactGenerateFn, build_compactor
from speech_to_speech.LLM.text_prompt import build_text_system_prompt
from speech_to_speech.LLM.tool_call.function_call import extract_inline_function_calls_from_text
from speech_to_speech.LLM.tool_call.function_tool import FunctionTool
from speech_to_speech.LLM.utils import remove_unspeechable, resolve_auto_language
from speech_to_speech.LLM.voice_prompt import build_voice_system_prompt
from speech_to_speech.pipeline.cancel_scope import CancelScope
from speech_to_speech.pipeline.handler_types import LLMIn, LLMOut
from speech_to_speech.pipeline.messages import (
    EndOfResponse,
    LLMResponseChunk,
    TokenUsage,
)
from speech_to_speech.pipeline.speculative_turns import SpeculativeTurnTracker
from speech_to_speech.utils.utils import is_out_of_band, response_wants_audio

logger = logging.getLogger(__name__)


PROVIDER_SELECTOR_SEP = "::"


@dataclass(frozen=True)
class OpenAICompatibleRoute:
    selector: str
    provider_id: str
    provider_label: str
    model_name: str
    base_url: Optional[str]
    api_key: Optional[str]
    extra_body: Optional[dict[str, Any]]


@dataclass(frozen=True)
class OpenAICompatibleProviderRoute:
    provider_id: str
    provider_label: str
    base_url: Optional[str]
    api_key: Optional[str]
    reasoning_effort: Optional[str]
    disable_thinking: bool


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _selector(provider_id: str, model_name: str) -> str:
    return f"{provider_id}{PROVIDER_SELECTOR_SEP}{model_name}"


# ── Normalised provider events ────────────────────────────────────────────────
# Each backend's stream/response is mapped to this small vocabulary so the shared
# speech-pipeline logic (sentence batching, cancellation, history, token usage)
# lives in one place. Subclasses differ only in how they produce these events.


class TextDelta(BaseModel):
    """Incremental assistant text. Always RAW (unfiltered); the base applies
    ``remove_unspeechable`` for the audio path."""

    text: str


class AssistantMessage(BaseModel):
    """A complete assistant turn to write back to history."""

    content: list[AssistantContent]


class ToolCall(BaseModel):
    """A complete function tool call (``call_id`` / ``id`` already regenerated)."""

    item: ResponseFunctionToolCall


class Usage(BaseModel):
    """Token accounting for the turn."""

    input_tokens: int
    output_tokens: int


ProviderEvent = TextDelta | AssistantMessage | ToolCall | Usage


class _Turn(BaseModel):
    """Per-request context threaded through generation (immutable for the turn)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    language_code: Optional[str]
    gen: int | None
    runtime_config: Any
    response: Any
    turn_id: str | None
    turn_revision: int | None
    speech_stopped_at_s: float | None
    wants_audio: bool


class _GenState(BaseModel):
    """Mutable accumulators collected while consuming a turn's events."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tools: list[ResponseFunctionToolCall] = Field(default_factory=list)
    pending: list[SupportedItem] = Field(default_factory=list)
    clean_text: str = ""  # filtered text, kept only for the debug log
    input_tokens: int = 0
    output_tokens: int = 0


class BaseOpenAICompatibleHandler(BaseHandler[LLMIn, LLMOut], ABC):
    """Shared lifecycle for OpenAI-compatible LLM backends (Responses & Chat
    Completions).

    Subclasses implement four hooks — :meth:`warmup`,
    :meth:`_build_compaction_generate_fn`, :meth:`_serialize`, :meth:`_request`,
    :meth:`_iter_events` and :meth:`_build_optional_kwargs` — and inherit the
    request/response orchestration: speculative-turn gating, cancellation,
    sentence batching, text-only vs audio handling, history write-back, token
    usage, out-of-band handling and error termination.
    """

    # ── setup ─────────────────────────────────────────────────────────────────

    def setup(
        self,
        model_name: str = "gpt-5.4-mini",
        device: str = "cuda",
        gen_kwargs: dict[str, Any] = {},
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        stream: bool = True,
        user_role: str = "user",
        cancel_scope: CancelScope | None = None,
        speculative_turns: SpeculativeTurnTracker | None = None,
        disable_thinking: bool = True,
        reasoning_effort: Optional[str] = None,
        request_timeout_s: float = 120.0,
        stream_batch_sentences: int = 3,
        enable_lang_prompt: bool = False,
        compact_history: bool = False,
        **_kwargs: Any,
    ) -> None:
        self.cancel_scope = cancel_scope
        self.speculative_turns = speculative_turns
        self.model_name = model_name
        self.stream = stream
        self.stream_batch_sentences = max(1, stream_batch_sentences)
        self.enable_lang_prompt = enable_lang_prompt
        self.gen_kwargs = dict(gen_kwargs)
        self.request_timeout_s = float(request_timeout_s)
        self.request_timeout = httpx.Timeout(
            self.request_timeout_s,
            connect=min(10.0, self.request_timeout_s),
        )

        self.user_role = user_role
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self._extra_body = self._build_extra_body(base_url, disable_thinking, reasoning_effort)
        self._clients: dict[tuple[Optional[str], Optional[str]], OpenAI] = {(base_url, api_key): self.client}
        self._default_route = OpenAICompatibleRoute(
            selector=model_name,
            provider_id="default",
            provider_label="Default",
            model_name=model_name,
            base_url=base_url,
            api_key=api_key,
            extra_body=self._extra_body,
        )
        self._provider_routes: dict[str, OpenAICompatibleProviderRoute] = {}
        self._routes = self._load_routes(
            default_route=self._default_route,
            disable_thinking=disable_thinking,
            fallback_reasoning_effort=reasoning_effort,
        )
        self.compactor = build_compactor(self._build_compaction_generate_fn()) if compact_history else None
        self.warmup()

    @staticmethod
    def _is_official_openai(base_url: Optional[str]) -> bool:
        """Whether ``base_url`` points at the official OpenAI server.

        Normalises a trailing slash so ``https://api.openai.com/v1/`` is also
        recognised; the official server rejects the provider-specific extra_body
        keys we send to vLLM / the HF router.
        """
        if base_url is None:
            return False
        return base_url.rstrip("/") == "https://api.openai.com/v1"

    @classmethod
    def _build_extra_body(
        cls,
        base_url: Optional[str],
        disable_thinking: bool,
        reasoning_effort: Optional[str],
    ) -> Optional[dict[str, Any]]:
        """Build the provider-specific ``extra_body`` used to disable reasoning.

        Providers differ in how reasoning is turned off: vLLM/Qwen honour
        ``chat_template_kwargs.enable_thinking=false``, while others (e.g. GLM via
        the HF router) ignore that and require ``reasoning_effort='none'``. A
        non-empty ``reasoning_effort`` therefore takes precedence; otherwise we fall
        back to the chat-template flag. None of this applies to the official
        OpenAI server, which rejects unknown extra_body keys.
        """
        if base_url is None or cls._is_official_openai(base_url):
            return None
        if reasoning_effort:
            return {"reasoning_effort": reasoning_effort}
        if disable_thinking:
            return {"chat_template_kwargs": {"enable_thinking": False}}
        return None

    def _load_routes(
        self,
        *,
        default_route: OpenAICompatibleRoute,
        disable_thinking: bool,
        fallback_reasoning_effort: Optional[str],
    ) -> dict[str, OpenAICompatibleRoute]:
        routes = {default_route.selector: default_route}
        raw = os.environ.get("LLM_PROVIDERS_JSON", "").strip()
        if not raw:
            return routes
        try:
            providers = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Invalid LLM_PROVIDERS_JSON: %s", exc)
            return routes
        if not isinstance(providers, list):
            logger.warning("LLM_PROVIDERS_JSON must be a JSON list")
            return routes

        model_to_selector: dict[str, str] = {}
        duplicate_models: set[str] = set()
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            provider_id = _clean_str(provider.get("id"))
            provider_label = _clean_str(provider.get("label")) or provider_id
            base_url = _clean_str(provider.get("baseUrl")) or None
            api_key_env = _clean_str(provider.get("apiKeyEnv"))
            api_key_value = os.environ.get(api_key_env, "").strip() if api_key_env else ""
            api_key = api_key_value or None
            provider_reasoning = _clean_str(provider.get("reasoningEffort")) or fallback_reasoning_effort
            provider_disable = bool(provider.get("disableThinking", disable_thinking))
            if not provider_id or not base_url:
                continue
            self._provider_routes[provider_id] = OpenAICompatibleProviderRoute(
                provider_id=provider_id,
                provider_label=provider_label,
                base_url=base_url,
                api_key=api_key,
                reasoning_effort=provider_reasoning,
                disable_thinking=provider_disable,
            )
            for model in provider.get("models") or []:
                if not isinstance(model, dict):
                    continue
                model_id = _clean_str(model.get("id"))
                if not model_id:
                    continue
                reasoning_effort = _clean_str(model.get("reasoningEffort")) or provider_reasoning
                model_disable = bool(model.get("disableThinking", provider_disable))
                selector = _selector(provider_id, model_id)
                route = OpenAICompatibleRoute(
                    selector=selector,
                    provider_id=provider_id,
                    provider_label=provider_label,
                    model_name=model_id,
                    base_url=base_url,
                    api_key=api_key,
                    extra_body=self._build_extra_body(base_url, model_disable, reasoning_effort),
                )
                routes[selector] = route
                if model_id in model_to_selector:
                    duplicate_models.add(model_id)
                else:
                    model_to_selector[model_id] = selector

        for model_id, selector in model_to_selector.items():
            if model_id not in duplicate_models:
                routes[model_id] = routes[selector]
        logger.info("Loaded %d OpenAI-compatible LLM routes", len({r.selector for r in routes.values()}))
        return routes

    def _client_for_route(self, route: OpenAICompatibleRoute) -> OpenAI:
        key = (route.base_url, route.api_key)
        client = self._clients.get(key)
        if client is None:
            client = OpenAI(api_key=route.api_key, base_url=route.base_url)
            self._clients[key] = client
        return client

    def _route_for_runtime(self, runtime_config: Any) -> OpenAICompatibleRoute:
        session = getattr(runtime_config, "session", None)
        requested = _clean_str(getattr(session, "model", None)) if session is not None else ""
        if not requested:
            return self._default_route
        route = self._routes.get(requested)
        if route is None:
            route = self._dynamic_provider_route(requested)
        if route is None:
            logger.warning("Unknown session model/provider route %r; using default route", requested)
            return self._default_route
        return route

    def _dynamic_provider_route(self, selector: str) -> Optional[OpenAICompatibleRoute]:
        if PROVIDER_SELECTOR_SEP not in selector:
            return None
        provider_id, model_name = selector.split(PROVIDER_SELECTOR_SEP, 1)
        provider_id = _clean_str(provider_id)
        model_name = _clean_str(model_name)
        provider = self._provider_routes.get(provider_id)
        if not provider or not model_name:
            return None
        route = OpenAICompatibleRoute(
            selector=selector,
            provider_id=provider.provider_id,
            provider_label=provider.provider_label,
            model_name=model_name,
            base_url=provider.base_url,
            api_key=provider.api_key,
            extra_body=self._build_extra_body(
                provider.base_url,
                provider.disable_thinking,
                provider.reasoning_effort,
            ),
        )
        self._routes[selector] = route
        logger.info("Created dynamic OpenAI-compatible LLM route: %s / %s", provider.provider_label, model_name)
        return route

    # ── subclass hooks ──────────────────────────────────────────────────────--

    @abstractmethod
    def warmup(self) -> None:
        """Issue a cheap request so the model/connection is ready before serving."""
        ...

    @abstractmethod
    def _build_compaction_generate_fn(self) -> CompactGenerateFn:
        """Return a ``(system, user) -> text`` fn used to compact long histories."""
        ...

    @abstractmethod
    def _serialize(self, active_chat: Chat) -> Any:
        """Serialise the chat to the backend's request payload (input/messages)."""
        ...

    @abstractmethod
    def _request(self, api_input: Any, optional_kwargs: dict[str, Any], route: OpenAICompatibleRoute) -> Any:
        """Issue the create() call and return the response or stream."""
        ...

    @abstractmethod
    def _iter_stream_events(self, api_response: Any) -> Iterator[ProviderEvent]:
        """Map a streaming response to normalised :data:`ProviderEvent`s."""
        ...

    @abstractmethod
    def _iter_response_events(self, api_response: Any) -> Iterator[ProviderEvent]:
        """Map a non-streaming response to normalised :data:`ProviderEvent`s."""
        ...

    def _iter_events(self, api_response: Any) -> Iterator[ProviderEvent]:
        """Dispatch to the stream/non-stream mapper. ``self.stream`` is the single
        source of truth (it set the request's ``stream=`` flag), so the response
        type always matches it."""
        if self.stream:
            yield from self._iter_stream_events(api_response)
        else:
            yield from self._iter_response_events(api_response)

    @abstractmethod
    def _build_optional_kwargs(self, req_tools: Any, req_tool_choice: Any) -> dict[str, Any]:
        """Build the per-request tools/tool_choice kwargs in the backend's shape."""
        ...

    # ── speculative-turn / cancellation gating ─────────────────────────────────

    def _turn_is_latest(self, turn_id: str | None, turn_revision: int | None) -> bool:
        return self.speculative_turns is None or self.speculative_turns.is_latest(turn_id, turn_revision)

    def _generation_is_stale(self, gen: int | None) -> bool:
        return gen is not None and self.cancel_scope is not None and self.cancel_scope.is_stale(gen)

    def _turn_output_allowed(self, turn_id: str | None, turn_revision: int | None) -> bool:
        if self.speculative_turns is None:
            return True
        return self.speculative_turns.is_latest_after_reopen_grace(turn_id, turn_revision)

    def _apply_config(
        self,
        chat: Chat,
        instructions: Optional[str],
        wants_audio: bool = True,
    ) -> None:
        if instructions:
            builder = build_voice_system_prompt if wants_audio else build_text_system_prompt
            full_instructions = builder(instructions)
            chat.add_item(make_system_message(full_instructions))

    # ── output helpers ──────────────────────────────────────────────────────--

    def _chunk(
        self,
        turn: _Turn,
        *,
        text: str = "",
        tools: list[ResponseFunctionToolCall] | None = None,
        language_code: Optional[str] = None,
    ) -> LLMResponseChunk:
        return LLMResponseChunk(
            text=text,
            language_code=language_code if language_code is not None else turn.language_code,
            tools=tools or [],
            runtime_config=turn.runtime_config,
            response=turn.response,
            turn_id=turn.turn_id,
            turn_revision=turn.turn_revision,
            speech_stopped_at_s=turn.speech_stopped_at_s,
            cancel_generation=turn.gen,
        )

    def _record_tool_call(self, state: _GenState, turn: _Turn, item: ResponseFunctionToolCall) -> Iterator[LLMOut]:
        """Emit a tool call, persisting it (and any assistant text seen so far)
        to history *before* it is forwarded to the client.

        The function_call must already exist in the conversation by the time the
        client returns its ``function_call_output``; otherwise a fast client
        races ahead of the deferred end-of-turn write-back and the output is
        rejected ("No function_call with call_id ... found"), which makes the
        model re-issue the same tool call. The call lands in ``_pending_tool_calls``
        (not serialized until its output pairs it), so eager recording is safe.

        Out-of-band turns never touch the default conversation, and a stale turn
        records nothing (it is not forwarded to the client either)."""
        state.tools.append(item)
        item_extra = getattr(item, "model_extra", None) or {}
        extra_fields: dict[str, Any] = {}
        if isinstance(item_extra.get("extra_content"), dict):
            extra_fields["extra_content"] = item_extra["extra_content"]
        fc_item = RealtimeConversationItemFunctionCall(
            type="function_call",
            name=item.name,
            arguments=item.arguments,
            call_id=item.call_id,
            id=item.id,
            status=item.status,
            **extra_fields,
        )
        if self._generation_is_stale(turn.gen) or not self._turn_output_allowed(turn.turn_id, turn.turn_revision):
            logger.info("LLM generation cancelled (stale speculative turn)")
            return
        if not is_out_of_band(turn.response):
            # Flush assistant text accumulated before this call first (so history
            # order matches what the client received), then persist the call —
            # all before the chunk leaves for the client.
            chat = turn.runtime_config.chat
            for pending_item in state.pending:
                chat.add_item(pending_item)
            state.pending.clear()
            chat.add_item(fc_item)
        yield self._chunk(turn, tools=[item])

    def _raw_tools_for_turn(self, turn: _Turn) -> list[Any]:
        response_tools = getattr(turn.response, "tools", None) if turn.response else None
        return response_tools or getattr(turn.runtime_config.session, "tools", None) or []

    def _tool_choice_for_turn(self, turn: _Turn) -> Any:
        response_tool_choice = getattr(turn.response, "tool_choice", None) if turn.response else None
        return response_tool_choice or getattr(turn.runtime_config.session, "tool_choice", None)

    def _function_tools_for_turn(self, turn: _Turn) -> list[FunctionTool]:
        function_tools: list[FunctionTool] = []
        for raw_tool in self._raw_tools_for_turn(turn):
            tool_dict = raw_tool if isinstance(raw_tool, dict) else raw_tool.model_dump(exclude_none=True)
            if tool_dict.get("type") == "function":
                function_tools.append(FunctionTool(**tool_dict))
        return function_tools

    def _fallback_tools_enabled(self, turn: _Turn) -> bool:
        return bool(self._function_tools_for_turn(turn)) and self._tool_choice_for_turn(turn) != "none"

    def _replace_pending_assistant_text(self, state: _GenState, clean_text: str) -> None:
        """Remove raw inline-call tags from assistant history before a recovered tool call."""
        rewritten: list[SupportedItem] = []
        replacement_added = False
        for item in state.pending:
            if isinstance(item, RealtimeConversationItemAssistantMessage):
                if clean_text.strip() and not replacement_added:
                    rewritten.append(
                        RealtimeConversationItemAssistantMessage(
                            type="message",
                            role="assistant",
                            content=[AssistantContent(type="output_text", text=clean_text.strip())],
                        )
                    )
                    replacement_added = True
                continue
            rewritten.append(item)
        state.pending = rewritten

    def _extract_inline_tool_calls(
        self,
        text: str,
        state: _GenState,
        turn: _Turn,
    ) -> tuple[str, list[ResponseFunctionToolCall]]:
        if state.tools or not self._fallback_tools_enabled(turn):
            return text, []

        clean_text, inline_calls = extract_inline_function_calls_from_text(text)
        if not inline_calls:
            return text, []

        parsed_tools: list[ResponseFunctionToolCall] = []
        function_tools = self._function_tools_for_turn(turn)
        for inline_call in inline_calls:
            if parsed_tools:
                logger.warning(
                    "Skipping extra inline tool call '%s'; only one tool call is allowed per response",
                    inline_call.function_name,
                )
                continue
            try:
                parsed_tools.append(inline_call.to_realtime_function_tool_call(function_tools))
            except ValueError as exc:
                logger.warning("Skipping invalid inline tool call: %s", exc)

        if parsed_tools:
            self._replace_pending_assistant_text(state, clean_text)
        return clean_text, parsed_tools

    # ── consumption ─────────────────────────────────────────────────────────--

    def _consume_streaming(self, events: Iterator[ProviderEvent], state: _GenState, turn: _Turn) -> Iterator[LLMOut]:
        cancelled = False
        printable_text = ""
        sentence_batch: list[str] = []

        def _flush(batch: list[str]) -> Iterator[LLMOut]:
            if not batch:
                return
            if not self._turn_output_allowed(turn.turn_id, turn.turn_revision):
                logger.info("LLM generation cancelled (stale speculative turn)")
                return
            yield self._chunk(turn, text=" ".join(batch))

        for event in events:
            if self._generation_is_stale(turn.gen) or not self._turn_is_latest(turn.turn_id, turn.turn_revision):
                logger.info("LLM generation cancelled (interruption)")
                cancelled = True
                break

            if isinstance(event, Usage):
                state.input_tokens = event.input_tokens
                state.output_tokens = event.output_tokens
            elif isinstance(event, AssistantMessage):
                state.pending.append(
                    RealtimeConversationItemAssistantMessage(type="message", role="assistant", content=event.content)
                )
            elif isinstance(event, ToolCall):
                # Flush any pending spoken text before emitting the tool call.
                if printable_text.strip():
                    sentence_batch.append(printable_text.strip())
                    printable_text = ""
                if sentence_batch:
                    if not self._turn_output_allowed(turn.turn_id, turn.turn_revision):
                        logger.info("LLM generation cancelled (stale speculative turn)")
                        cancelled = True
                        break
                    yield from _flush(sentence_batch)
                    sentence_batch = []
                yield from self._record_tool_call(state, turn, event.item)
            elif isinstance(event, TextDelta):
                if not turn.wants_audio and self._fallback_tools_enabled(turn):
                    state.clean_text += event.text
                    printable_text += event.text
                    continue
                if not turn.wants_audio:
                    # Text-only: forward verbatim. Keep every character (no
                    # remove_unspeechable, which strips TTS-unfriendly symbols) and
                    # don't sentence-split (sent_tokenize collapses newlines/markdown).
                    state.clean_text += event.text
                    if event.text:
                        if not self._turn_output_allowed(turn.turn_id, turn.turn_revision):
                            logger.info("LLM generation cancelled (stale speculative turn)")
                            cancelled = True
                            break
                        yield self._chunk(turn, text=event.text)
                    continue
                new_text = remove_unspeechable(event.text)
                state.clean_text += new_text
                printable_text += new_text
                sentences = sent_tokenize(printable_text)
                if len(sentences) > 1:
                    for s in sentences[:-1]:
                        sentence_batch.append(s)
                        if len(sentence_batch) >= self.stream_batch_sentences:
                            if not self._turn_output_allowed(turn.turn_id, turn.turn_revision):
                                logger.info("LLM generation cancelled (stale speculative turn)")
                                cancelled = True
                                break
                            yield from _flush(sentence_batch)
                            sentence_batch = []
                    if cancelled:
                        break
                    printable_text = sentences[-1]

        if not cancelled:
            printable_text, fallback_tools = self._extract_inline_tool_calls(printable_text, state, turn)
            if printable_text.strip():
                sentence_batch.append(printable_text.strip())
            if sentence_batch:
                if self._generation_is_stale(turn.gen):
                    logger.info("LLM generation cancelled (interruption)")
                else:
                    logger.debug(f"Clean text: {state.clean_text}")
                    yield from _flush(sentence_batch)
            for tool_call in fallback_tools:
                yield from self._record_tool_call(state, turn, tool_call)
            logger.info(f"Tools: {state.tools}")

    def _consume_nonstreaming(self, events: Iterator[ProviderEvent], state: _GenState, turn: _Turn) -> Iterator[LLMOut]:
        if self._generation_is_stale(turn.gen) or not self._turn_is_latest(turn.turn_id, turn.turn_revision):
            logger.info("LLM generation cancelled (interruption)")
            return
        for event in events:
            if isinstance(event, Usage):
                state.input_tokens = event.input_tokens
                state.output_tokens = event.output_tokens
            elif isinstance(event, AssistantMessage):
                state.pending.append(
                    RealtimeConversationItemAssistantMessage(type="message", role="assistant", content=event.content)
                )
            elif isinstance(event, ToolCall):
                yield from self._record_tool_call(state, turn, event.item)
            elif isinstance(event, TextDelta):
                # Text-only keeps every character verbatim; audio strips
                # TTS-unfriendly symbols via remove_unspeechable.
                spoken = event.text if not turn.wants_audio else remove_unspeechable(event.text)
                spoken, fallback_tools = self._extract_inline_tool_calls(spoken, state, turn)
                state.clean_text += spoken
                out = spoken if not turn.wants_audio else spoken.strip()
                if (
                    out
                    and not self._generation_is_stale(turn.gen)
                    and self._turn_output_allowed(turn.turn_id, turn.turn_revision)
                ):
                    yield self._chunk(turn, text=out)
                for tool_call in fallback_tools:
                    yield from self._record_tool_call(state, turn, tool_call)
        logger.debug(f"Clean text: {state.clean_text}")
        logger.info(f"Tools: {state.tools}")

    # ── orchestration ─────────────────────────────────────────────────────────

    def _generate(
        self,
        active_chat: Chat,
        original_chat: Chat,
        turn: _Turn,
        optional_kwargs: dict[str, Any],
    ) -> Iterator[LLMOut]:
        api_response: Any = None
        state = _GenState()
        error_message: str | None = None
        route = self._route_for_runtime(turn.runtime_config)
        api_input = self._serialize(active_chat)
        # Images the model actually sees this turn; only these are stripped on
        # write-back, so an image a fast client injects mid-generation for the
        # next turn survives (it is not in this serialized snapshot).
        consumed_image_ids = active_chat.image_message_ids()
        if not api_input:
            # Nothing to send: empty `instructions` and no `input` (in the response,
            # the default conversation, or the out-of-band context). The provider
            # would reject this; fail with a clear message instead of an opaque error.
            error_message = "Cannot generate a response: no instructions and no input were provided."

        try:
            if error_message is None:
                logger.info("LLM route selected: %s / %s", route.provider_label, route.model_name)
                api_response = self._request(api_input, optional_kwargs, route)
            if api_response is not None:
                events = self._iter_events(api_response)
                if self.stream:
                    yield from self._consume_streaming(events, state, turn)
                else:
                    yield from self._consume_nonstreaming(events, state, turn)
        except httpx.ReadTimeout:
            logger.warning(
                "OpenAI API read timed out after %.1fs; ending the current response",
                self.request_timeout_s,
            )
            if not self._generation_is_stale(turn.gen) and self._turn_output_allowed(turn.turn_id, turn.turn_revision):
                # Canned apology carries no language_code (mirrors the prior handlers).
                yield LLMResponseChunk(
                    text="Wow I'm a bit slow today, could you repeat that?",
                    runtime_config=turn.runtime_config,
                    response=turn.response,
                    turn_id=turn.turn_id,
                    turn_revision=turn.turn_revision,
                    speech_stopped_at_s=turn.speech_stopped_at_s,
                    cancel_generation=turn.gen,
                )
        except Exception as exc:
            # Any other generation failure must still terminate the response: record
            # the error and fall through to the EndOfResponse below. Without this the
            # exception would escape process() and no EndOfResponse would be emitted,
            # leaving st.in_response stuck and locking every subsequent response.
            logger.exception("LLM generation failed; ending the current response")
            if error_message is None:
                error_message = f"Language model generation failed: {exc}"
        finally:
            if api_response is not None and hasattr(api_response, "close"):
                try:
                    api_response.close()
                except Exception:
                    pass

        if (
            error_message is None
            and not self._generation_is_stale(turn.gen)
            and self._turn_output_allowed(turn.turn_id, turn.turn_revision)
        ):
            # Out-of-band responses emit output and usage but never write back to the
            # default conversation (their context was a throwaway chat).
            if not is_out_of_band(turn.response):
                # Tool calls (and any assistant text preceding them) were already
                # written eagerly in _record_tool_call; only trailing items remain.
                for item in state.pending:
                    original_chat.add_item(item)
                original_chat.strip_images(consumed_image_ids)
                original_chat.trim_if_needed(self.compactor)
            if state.input_tokens or state.output_tokens:
                yield TokenUsage(
                    input_tokens=state.input_tokens,
                    output_tokens=state.output_tokens,
                    turn_id=turn.turn_id,
                    turn_revision=turn.turn_revision,
                )
        yield EndOfResponse(
            turn_id=turn.turn_id, turn_revision=turn.turn_revision, cancel_generation=turn.gen, error=error_message
        )

    def process(self, request: LLMIn) -> Iterator[LLMOut]:
        """Process a language model request and yield LLMResponseChunks."""
        runtime_config = request.runtime_config
        response = request.response
        turn_id = request.turn_id
        turn_revision = request.turn_revision
        speech_stopped_at_s = request.speech_stopped_at_s
        if not self._turn_is_latest(turn_id, turn_revision):
            logger.info("Skipping stale LLM request for turn=%s rev=%s", turn_id, turn_revision)
            yield EndOfResponse(turn_id=turn_id, turn_revision=turn_revision)
            return

        original_chat = runtime_config.chat
        if is_out_of_band(response):
            try:
                active_chat = build_active_chat(original_chat, response)
            except ChatItemError as exc:
                logger.info("Out-of-band response rejected: %s", exc)
                yield EndOfResponse(turn_id=turn_id, turn_revision=turn_revision, error=str(exc))
                return
        else:
            active_chat = original_chat.copy()
        language_code = request.language_code
        instructions = (
            response.instructions if response and response.instructions else runtime_config.session.instructions
        ) or ""
        req_tools = response.tools if response and response.tools else runtime_config.session.tools
        req_tool_choice = (
            response.tool_choice if response and response.tool_choice else runtime_config.session.tool_choice
        )
        wants_audio = response_wants_audio(response)
        self._apply_config(active_chat, instructions, wants_audio)
        language_code, lang_name = resolve_auto_language(language_code)
        if lang_name and self.enable_lang_prompt:
            active_chat.add_item(make_user_message(f"Please reply to my message in {lang_name}."))

        optional_kwargs = self._build_optional_kwargs(req_tools, req_tool_choice)

        # CancelScope.is_stale(gen) is checked when the stream iterator advances; a
        # blocked read inside httpx cannot be aborted by cancel_scope.cancel() from
        # the websocket router. Mitigations: request_timeout_s / ReadTimeout.
        gen = self.cancel_scope.generation if self.cancel_scope else None

        turn = _Turn(
            language_code=language_code,
            gen=gen,
            runtime_config=runtime_config,
            response=response,
            turn_id=turn_id,
            turn_revision=turn_revision,
            speech_stopped_at_s=speech_stopped_at_s,
            wants_audio=wants_audio,
        )
        yield from self._generate(active_chat, original_chat, turn, optional_kwargs)

    @property
    def timing_log_level(self) -> int:
        return logging.INFO

    def should_log_timing(self, output: LLMOut) -> bool:
        return isinstance(output, LLMResponseChunk) and self.last_time > self.min_time_to_debug
