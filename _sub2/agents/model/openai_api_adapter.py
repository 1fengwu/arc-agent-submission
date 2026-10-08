from __future__ import annotations

import asyncio
import os
import random
import time as _time
from dataclasses import dataclass
from typing import Any, Callable, Literal, NoReturn

import httpx
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAIError
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from agents.model.compatible_openai import CompatibleChatOpenAI
from agents.model.token_budget import TokenBudgetExceeded, record as _record_tokens


OpenAIAPIMode = Literal["responses", "chat_completions"]
_TRUTHY = {"1", "true", "yes", "on", "responses", "response", "responses_api"}
_FALSY = {"0", "false", "no", "off", "chat", "chat_completion", "chat_completions", "chat/completions"}

# Network-transient signatures our relay wraps as HTTP 400 proxy_error (openai SDK
# refuses to retry 4xx, so a single dropped connection kills the whole shard).
_TRANSIENT_NETWORK_SIGNATURES = (
    "proxy_error",
    "connection reset by peer",
    "read tcp",
    "connection aborted",
    "remotedisconnected",
    "econnreset",
    "broken pipe",
    "socket hang up",
    "upstream connect error",
    "network is unreachable",
    "tls handshake",
)


@dataclass(frozen=True)
class OpenAIAdapterConfig:
    model_name: str
    api_mode: OpenAIAPIMode
    base_url: str = ""
    api_key: str = ""
    sse_text_compat: bool = False


class ARCModelAPIError(RuntimeError):
    """Normalized exception for OpenAI-compatible model API failures."""

    def __init__(
        self,
        message: str,
        *,
        api_mode: OpenAIAPIMode,
        model: str,
        status_code: int | None = None,
        error_type: str = "",
        original: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.api_mode = api_mode
        self.model = model
        self.status_code = status_code
        self.error_type = error_type
        self.original = original


def _extract_token_usage(result, messages) -> tuple[int, int]:
    """Best-effort token accounting from a LangChain ChatResult.

    Prefers the provider-reported usage; falls back to a conservative
    character-based estimate so the budget guard still triggers even when the
    endpoint omits usage metadata.
    """
    prompt_tokens = 0
    completion_tokens = 0
    try:
        llo = getattr(result, "llm_output", None) or {}
        tu = (llo.get("token_usage") if isinstance(llo, dict) else None) or {}
        if isinstance(tu, dict):
            prompt_tokens = int(tu.get("prompt_tokens") or 0)
            completion_tokens = int(tu.get("completion_tokens") or 0)
    except Exception:
        prompt_tokens = completion_tokens = 0
    if not (prompt_tokens or completion_tokens):
        try:
            msg = result.generations[0].message
            um = getattr(msg, "usage_metadata", None) or {}
            if isinstance(um, dict):
                prompt_tokens = int(um.get("input_tokens") or 0)
                completion_tokens = int(um.get("output_tokens") or 0)
        except Exception:
            prompt_tokens = completion_tokens = 0
    if not (prompt_tokens or completion_tokens):
        # Conservative fallback: ~3 chars per token (biases toward stopping early,
        # which is the safe direction for a hard budget cap).
        try:
            in_chars = sum(len(str(getattr(m, "content", m))) for m in (messages or []))
        except Exception:
            in_chars = 0
        try:
            out_chars = len(str(result.generations[0].message.content))
        except Exception:
            out_chars = 0
        prompt_tokens = in_chars // 3
        completion_tokens = out_chars // 3
    return prompt_tokens, completion_tokens


def _is_transient_network_error(exc: Exception) -> bool:
    """True for network drops the OpenAI SDK refuses to retry (it only retries
    connection/timeout/429/5xx). Our relay masks resets as HTTP 400 proxy_error,
    which would otherwise crash the whole shard. Treat those as transient."""
    if isinstance(exc, ARCModelAPIError):
        msg = str(exc).lower()
        sc = getattr(exc, "status_code", None)
        # proxy masks network drops as 400; a genuine 400 (bad content) has no net sig
        if sc is None or sc == 0 or sc == 400:
            return any(sig in msg for sig in _TRANSIENT_NETWORK_SIGNATURES)
        return False
    if isinstance(exc, (APIConnectionError, APITimeoutError, httpx.HTTPError)):
        return True
    return False


def _sync_network_retry(call: "Callable[[], Any]", *, api_mode, model) -> Any:
    last = None
    for attempt in range(8):
        try:
            return call()
        except Exception as exc:
            if _is_transient_network_error(exc):
                _time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
                last = exc
                continue
            _raise_model_api_exception(exc, api_mode=api_mode, model=model)
    if last is None:
        raise RuntimeError("model call failed after retries")
    _raise_model_api_exception(last, api_mode=api_mode, model=model)


async def _async_network_retry(call: "Callable[[], Any]", *, api_mode, model) -> Any:
    last = None
    for attempt in range(8):
        try:
            return await call()
        except Exception as exc:
            if _is_transient_network_error(exc):
                await asyncio.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
                last = exc
                continue
            _raise_model_api_exception(exc, api_mode=api_mode, model=model)
    if last is None:
        raise RuntimeError("model call failed after retries")
    _raise_model_api_exception(last, api_mode=api_mode, model=model)


class ARCChatOpenAI(ChatOpenAI):
    """ChatOpenAI with ARC-level API error normalization + network self-heal."""

    _arc_api_mode: OpenAIAPIMode = PrivateAttr(default="chat_completions")
    _arc_model_name: str = PrivateAttr(default="")

    def __init__(self, *args: Any, arc_api_mode: OpenAIAPIMode, arc_model_name: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._arc_api_mode = arc_api_mode
        self._arc_model_name = arc_model_name

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        result = await _async_network_retry(
            lambda: super(ARCChatOpenAI, self)._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs),
            api_mode=self._arc_api_mode, model=self._arc_model_name,
        )
        try:
            _record_tokens(*_extract_token_usage(result, messages))
        except TokenBudgetExceeded:
            raise
        except Exception:
            pass
        return result

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        result = _sync_network_retry(
            lambda: super(ARCChatOpenAI, self)._generate(messages, stop=stop, run_manager=run_manager, **kwargs),
            api_mode=self._arc_api_mode, model=self._arc_model_name,
        )
        try:
            _record_tokens(*_extract_token_usage(result, messages))
        except TokenBudgetExceeded:
            raise
        except Exception:
            pass
        return result


class ARCCompatibleChatOpenAI(CompatibleChatOpenAI):
    """Responses-compatible ChatOpenAI with ARC-level API error normalization + network self-heal."""

    _arc_api_mode: OpenAIAPIMode = PrivateAttr(default="responses")
    _arc_model_name: str = PrivateAttr(default="")

    def __init__(self, *args: Any, arc_api_mode: OpenAIAPIMode, arc_model_name: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._arc_api_mode = arc_api_mode
        self._arc_model_name = arc_model_name

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        result = await _async_network_retry(
            lambda: super(ARCCompatibleChatOpenAI, self)._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs),
            api_mode=self._arc_api_mode, model=self._arc_model_name,
        )
        try:
            _record_tokens(*_extract_token_usage(result, messages))
        except TokenBudgetExceeded:
            raise
        except Exception:
            pass
        return result

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        result = _sync_network_retry(
            lambda: super(ARCCompatibleChatOpenAI, self)._generate(messages, stop=stop, run_manager=run_manager, **kwargs),
            api_mode=self._arc_api_mode, model=self._arc_model_name,
        )
        try:
            _record_tokens(*_extract_token_usage(result, messages))
        except TokenBudgetExceeded:
            raise
        except Exception:
            pass
        return result


def build_openai_chat_model(
    model_name: str,
    *,
    api_mode: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> ChatOpenAI:
    config = resolve_openai_adapter_config(
        model_name=model_name,
        api_mode=api_mode,
        base_url=base_url,
        api_key=api_key,
    )
    kwargs: dict[str, Any] = {
        "model": config.model_name,
        "disable_streaming": True,
        "stream_usage": False,
        "use_responses_api": config.api_mode == "responses",
        "output_version": "responses/v1" if config.api_mode == "responses" else "v0",
        "arc_api_mode": config.api_mode,
        "arc_model_name": config.model_name,
        # The platform's model proxy occasionally resets connections
        # (connection reset by peer / proxy_error). The LangChain default
        # max_retries=2 is not enough to survive an unstable proxy window,
        # and a single exhausted retry kills the whole shard (all its leaves).
        # Use aggressive exponential-backoff retries instead.
        "max_retries": 8,
        "timeout": 300,
    }
    if config.base_url:
        kwargs["base_url"] = config.base_url
    if config.api_key:
        kwargs["api_key"] = config.api_key

    model_class = ARCCompatibleChatOpenAI if config.sse_text_compat else ARCChatOpenAI
    return model_class(**kwargs)


def resolve_openai_adapter_config(
    *,
    model_name: str,
    api_mode: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> OpenAIAdapterConfig:
    # Resolution order (highest first):
    #   1. explicit function arguments
    #   2. platform-injected env vars (the eval runner creates a per-run
    #      TEMPORARY key when "use competition quota" is checked; it is handed
    #      to the agent as standard OpenAI-style env vars. Hard-coding a key
    #      here made the agent IGNORE that temp key and keep billing the
    #      owner's personal key until it hit 402 insufficient_balance).
    #   3. lowercase vars from the bundled .env (api_key/base_url/model)
    #   4. hard-coded personal key as last-resort fallback for local runs.
    resolved_api_key = (
        (api_key or "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
        or os.getenv("OPENAI_KEY", "").strip()
        or os.getenv("ARCBENCH_API_KEY", "").strip()
        or os.getenv("ARC_API_KEY", "").strip()
        or os.getenv("api_key", "").strip()
        or "ak_IigcA_z_fFzNKTl7HTcH0CAl1dxQbWrpz-Pl6LDHlGk".strip()
    )
    resolved_base_url = (
        (base_url or "").strip()
        or os.getenv("OPENAI_BASE_URL", "").strip()
        or os.getenv("OPENAI_API_BASE", "").strip()
        or os.getenv("base_url", "").strip()
        or "https://api.arc-bench.com/v1".strip()
    )
    resolved_model_name = (
        (model_name or "").strip()
        or os.getenv("ARCBENCH_MODEL", "").strip()
        or os.getenv("MODEL", "").strip()
        or os.getenv("model", "").strip()
        or "deepseek-v4-flash"
    )
    resolved_mode = resolve_openai_api_mode(api_mode)
    return OpenAIAdapterConfig(
        model_name=resolved_model_name,
        api_mode=resolved_mode,
        base_url=resolved_base_url,
        api_key=resolved_api_key,
        sse_text_compat=_should_use_sse_text_compat(resolved_base_url, resolved_mode),
    )


def resolve_openai_api_mode(api_mode: str | None = None) -> OpenAIAPIMode:
    requested = str(api_mode or os.getenv("ARC_OPENAI_API_MODE", "")).strip().lower()
    if not requested:
        return "chat_completions"
    if requested in _TRUTHY or requested in {"responses"}:
        return "responses"
    if requested in _FALSY or requested in {"chat_completions", "chat"}:
        return "chat_completions"
    raise ValueError(
        "Invalid OpenAI API mode. Use `responses` or `chat_completions` "
        "(aliases: true/false for responses, chat for chat_completions)."
    )


def should_disable_streaming_for_openai_mode(api_mode: str | None = None) -> bool:
    if resolve_openai_api_mode(api_mode) != "responses":
        return False
    force = os.environ.get("ARC_AGENT_FORCE_RESPONSES_STREAM", "").strip().lower()
    if force in {"1", "true", "yes", "on"}:
        return False
    base_url = _get_openai_base_url()
    if not base_url:
        return False
    return not _is_official_openai_base_url(base_url)


def normalize_model_api_exception(exc: Exception, *, api_mode: OpenAIAPIMode, model: str) -> Exception:
    """Return ARC's normalized API exception when `exc` is model-provider related."""

    return _wrap_model_api_exception(exc, api_mode=api_mode, model=model)


def _raise_model_api_exception(exc: Exception, *, api_mode: OpenAIAPIMode, model: str) -> NoReturn:
    wrapped = _wrap_model_api_exception(exc, api_mode=api_mode, model=model)
    if wrapped is exc:
        raise exc
    raise wrapped from exc


def _wrap_model_api_exception(exc: Exception, *, api_mode: OpenAIAPIMode, model: str) -> Exception:
    if isinstance(exc, ARCModelAPIError):
        return exc
    if not _is_model_api_exception(exc):
        return exc
    status_code = getattr(exc, "status_code", None)
    error_type = _extract_error_type(exc)
    message = _format_model_api_error_message(
        exc,
        api_mode=api_mode,
        model=model,
        status_code=status_code,
        error_type=error_type,
    )
    return ARCModelAPIError(
        message,
        api_mode=api_mode,
        model=model,
        status_code=status_code if isinstance(status_code, int) else None,
        error_type=error_type,
        original=exc,
    )


def _is_model_api_exception(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            OpenAIError,
            APIError,
            APIStatusError,
            APIConnectionError,
            APITimeoutError,
            httpx.HTTPError,
        ),
    )


def _format_model_api_error_message(
    exc: Exception,
    *,
    api_mode: OpenAIAPIMode,
    model: str,
    status_code: Any,
    error_type: str,
) -> str:
    parts = [
        f"Model API request failed using `{api_mode}` mode",
        f"model={model or '<unknown>'}",
    ]
    if status_code:
        parts.append(f"status={status_code}")
    if error_type:
        parts.append(f"type={error_type}")
    parts.append(f"error={_short_error_text(exc)}")
    if api_mode == "responses":
        parts.append("If the provider does not support Responses API, set ARC_OPENAI_API_MODE=chat_completions.")
    return "; ".join(parts)


def _extract_error_type(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("type") or error.get("code") or "").strip()
        return str(body.get("type") or body.get("code") or "").strip()
    return type(exc).__name__


def _short_error_text(exc: Exception, limit: int = 800) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "... [truncated]"


def _should_use_sse_text_compat(base_url: str, api_mode: OpenAIAPIMode) -> bool:
    override = os.getenv("ARC_OPENAI_SSE_TEXT_COMPAT", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return bool(base_url and api_mode == "responses" and not _is_official_openai_base_url(base_url))


def _get_openai_base_url() -> str:
    return os.getenv("OPENAI_API_BASE", "").strip() or os.getenv("OPENAI_BASE_URL", "").strip()


def _is_official_openai_base_url(base_url: str) -> bool:
    try:
        from urllib.parse import urlparse

        host = urlparse(base_url).hostname or ""
    except Exception:
        host = ""
    return host == "api.openai.com" or host.endswith(".openai.com")
