"""LLM client abstractions with target/optimizer call isolation.

The CSS design's First Law (LLM-Code division) requires the frozen TASK agent to
be call-isolated from the OPTIMIZER: rollout code must only ever be able to call
the target model, and analysis/optimization code must only ever be able to call
the optimizer model. We enforce this at the call boundary with two narrow
capability views (:class:`TargetOnlyClient` / :class:`OptimizerOnlyClient`) that
wrap a single underlying :class:`LLMClient`.

Two concrete implementations:

* :class:`RouterLLMClient` — adapts SkillOpt's model backends (claude / azure /
  codex). Backend resolution is direct (not via ``router.set_backend``). All
  SkillOpt imports are lazy so this module imports cleanly without SkillOpt.

* :class:`OpenAICompatLLMClient` — standalone OpenAI-compatible HTTP client with
  no SkillOpt dependency. Uses ``urllib.request`` directly, supports Qwen3's
  ``chat_template_kwargs`` for thinking-mode control, thread-safe for high
  concurrency (256+), and retries with exponential backoff.

The call-isolation guarantee (First Law) comes from the narrow capability views,
not from backend-global state.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from css.trajectory import GROUND_TRUTH_FIREWALL

if TYPE_CHECKING:
    from css.config import CSSConfig


@runtime_checkable
class LLMClient(Protocol):
    """A client that can call both the target and the optimizer models.

    Implementations are the *capability source*; the narrow views below restrict
    which half of this surface a given caller may touch.
    """

    def complete_target(
        self, system: str, user: str, *, max_tokens: int = 4096, temperature: float = 0.0
    ) -> str:
        """Single-shot target (frozen task agent) completion -> text."""
        ...

    def complete_target_messages(
        self, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.0
    ) -> str:
        """Multi-turn target completion -> text."""
        ...

    def complete_target_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> dict:
        """Multi-turn target completion with OpenAI function-calling.

        Returns the raw assistant message: ``{"content": str | None,
        "tool_calls": list | None}``. Used by env task-execution agents whose
        ReAct loop is driven by native tool calls (e.g. Bird Text-to-SQL).
        """
        ...

    def complete_optimizer(
        self, system: str, user: str, *, max_tokens: int = 4096
    ) -> tuple[str, dict]:
        """Single-shot optimizer completion -> (text, usage)."""
        ...

    def complete_optimizer_messages(
        self, messages: list[dict], *, max_tokens: int = 4096
    ) -> tuple[str, dict]:
        """Multi-turn optimizer completion -> (text, usage)."""
        ...


# Maps an injected backend name to the concrete SkillOpt backend module name.
# We resolve directly (not via router.normalize_backend_name, which maps
# 'claude'->'claude_chat' that _backend_module rejects).
_BACKEND_MODULE_NAMES = {
    "claude": "claude_backend",
    "claude_backend": "claude_backend",
    "claude_chat": "claude_backend",
    "anthropic": "claude_backend",
    "azure": "azure_openai",
    "azure_openai": "azure_openai",
    "codex": "codex_backend",
    "openai": "codex_backend",
}


class RouterLLMClient:
    """Concrete :class:`LLMClient` backed by a SkillOpt model backend module.

    The backend is fixed at construction and resolved directly to the concrete
    SkillOpt backend module per call. Call isolation (First Law) is provided by
    the narrow views, not by any backend-global state.
    """

    def __init__(
        self,
        target_model: str,
        optimizer_model: str,
        backend: str = "claude",
    ) -> None:
        self.target_model = target_model
        self.optimizer_model = optimizer_model
        self.backend = backend

    def _resolve_backend(self):
        """Lazily import and return the concrete SkillOpt backend module."""
        key = (self.backend or "").strip().lower()
        mod_name = _BACKEND_MODULE_NAMES.get(key)
        if mod_name is None:
            raise ValueError(
                f"Unknown backend {self.backend!r}; expected one of "
                f"{sorted(set(_BACKEND_MODULE_NAMES))}"
            )
        import importlib

        return importlib.import_module(f"skillopt.model.{mod_name}")

    def complete_target(
        self, system: str, user: str, *, max_tokens: int = 4096, temperature: float = 0.0
    ) -> str:
        del temperature  # SkillOpt target backends do not take a temperature arg.
        backend = self._resolve_backend()
        if hasattr(backend, "set_target_deployment"):
            backend.set_target_deployment(self.target_model)
        text, _usage = backend.chat_target(
            system=system,
            user=user,
            max_completion_tokens=max_tokens,
            stage="target",
        )
        return text

    def complete_target_messages(
        self, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.0
    ) -> str:
        del temperature
        backend = self._resolve_backend()
        if hasattr(backend, "set_target_deployment"):
            backend.set_target_deployment(self.target_model)
        text, _usage = backend.chat_target_messages(
            messages=messages,
            max_completion_tokens=max_tokens,
            stage="target",
        )
        return text

    def complete_target_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> dict:
        raise NotImplementedError(
            "function-calling target is not supported on the SkillOpt router "
            "backend; use the openai_compat backend for tool-using task agents"
        )

    def complete_optimizer(
        self, system: str, user: str, *, max_tokens: int = 4096
    ) -> tuple[str, dict]:
        backend = self._resolve_backend()
        if hasattr(backend, "set_optimizer_deployment"):
            backend.set_optimizer_deployment(self.optimizer_model)
        text, usage = backend.chat_optimizer(
            system=system,
            user=user,
            max_completion_tokens=max_tokens,
            stage="optimizer",
        )
        return text, usage

    def complete_optimizer_messages(
        self, messages: list[dict], *, max_tokens: int = 4096
    ) -> tuple[str, dict]:
        backend = self._resolve_backend()
        if hasattr(backend, "set_optimizer_deployment"):
            backend.set_optimizer_deployment(self.optimizer_model)
        text, usage = backend.chat_optimizer_messages(
            messages=messages,
            max_completion_tokens=max_tokens,
            stage="optimizer",
        )
        return text, usage


class StubLLMClient:
    """Deterministic test double implementing :class:`LLMClient`.

    Injected callables drive the responses; defaults echo a fixed string so the
    client never touches the network or any API. ``target_fn`` /
    ``optimizer_fn`` both take ``(system, user)`` and return text.
    """

    def __init__(
        self,
        target_fn: Callable[[str, str], str] | None = None,
        optimizer_fn: Callable[[str, str], str] | None = None,
        target_tools_fn: Callable[[list, list], dict] | None = None,
    ) -> None:
        self.target_fn = target_fn or (lambda system, user: "stub-target-response")
        self.optimizer_fn = optimizer_fn or (lambda system, user: "stub-optimizer-response")
        # Drives complete_target_tools in tests: (messages, tools) -> assistant msg.
        self.target_tools_fn = target_tools_fn

    def complete_target(
        self, system: str, user: str, *, max_tokens: int = 4096, temperature: float = 0.0
    ) -> str:
        del max_tokens, temperature
        return self.target_fn(system, user)

    def complete_target_messages(
        self, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.0
    ) -> str:
        del max_tokens, temperature
        system = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "system"
        )
        user = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") != "system"
        )
        return self.target_fn(system, user)

    def complete_target_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> dict:
        del tool_choice, max_tokens, temperature
        if self.target_tools_fn is not None:
            return self.target_tools_fn(messages, tools)
        # Generic default: a plain text turn with no tool call.
        return {"content": "stub-target-tools-response", "tool_calls": None}

    def complete_optimizer(
        self, system: str, user: str, *, max_tokens: int = 4096
    ) -> tuple[str, dict]:
        del max_tokens
        text = self.optimizer_fn(system, user)
        return text, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def complete_optimizer_messages(
        self, messages: list[dict], *, max_tokens: int = 4096
    ) -> tuple[str, dict]:
        del max_tokens
        # Flatten messages into a single (system, user) pair for the stub.
        system = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "system"
        )
        user = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") != "system"
        )
        text = self.optimizer_fn(system, user)
        return text, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


class TargetOnlyClient:
    """Narrow view: exposes only the target capability (First Law).

    Rollout code receives one of these so it is structurally incapable of calling
    the optimizer.
    """

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner

    def complete_target(
        self, system: str, user: str, *, max_tokens: int = 4096, temperature: float = 0.0
    ) -> str:
        return self._inner.complete_target(
            system, user, max_tokens=max_tokens, temperature=temperature
        )

    def complete_target_messages(
        self, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.0
    ) -> str:
        return self._inner.complete_target_messages(
            messages, max_tokens=max_tokens, temperature=temperature
        )

    def complete_target_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> dict:
        return self._inner.complete_target_tools(
            messages,
            tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def complete_optimizer(self, *args, **kwargs) -> tuple[str, dict]:
        raise RuntimeError("target-only client cannot call optimizer")

    def complete_optimizer_messages(self, *args, **kwargs) -> tuple[str, dict]:
        raise RuntimeError("target-only client cannot call optimizer")


def _inject_firewall_system(system: str) -> str:
    """Append the train/test ground-truth firewall to an optimizer system prompt."""
    if not system:
        return GROUND_TRUTH_FIREWALL
    return f"{system}\n\n{GROUND_TRUTH_FIREWALL}"


def _inject_firewall_messages(messages: list[dict]) -> list[dict]:
    """Ensure the firewall is present in the leading system message of a message
    list (prepending a system message if there is none). Input is not mutated."""
    if not messages:
        return [{"role": "system", "content": GROUND_TRUTH_FIREWALL}]
    out = [dict(m) for m in messages]
    if str(out[0].get("role", "")) == "system":
        out[0]["content"] = _inject_firewall_system(str(out[0].get("content", "")))
    else:
        out.insert(0, {"role": "system", "content": GROUND_TRUTH_FIREWALL})
    return out


class OptimizerOnlyClient:
    """Narrow view: exposes only the optimizer capability (First Law).

    Analysis / optimization code receives one of these so it is structurally
    incapable of calling the frozen target agent. It is also the SINGLE, can't-miss
    enforcement point for the ground-truth firewall: every optimizer LLM call in
    the system routes through here (direct ``complete_optimizer`` calls AND the
    ``complete_optimizer_json`` wrapper, which calls ``complete_optimizer`` on the
    client it is given), so injecting :data:`GROUND_TRUTH_FIREWALL` into the system
    prompt here guarantees EVERY current and future optimizer prompt carries it —
    the optimizer may reason FROM ground truth but can never emit an output that
    depends on it. See :data:`css.trajectory.GROUND_TRUTH_FIREWALL`.
    """

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner

    def complete_optimizer(
        self, system: str, user: str, *, max_tokens: int = 4096
    ) -> tuple[str, dict]:
        return self._inner.complete_optimizer(
            _inject_firewall_system(system), user, max_tokens=max_tokens
        )

    def complete_optimizer_messages(
        self, messages: list[dict], *, max_tokens: int = 4096
    ) -> tuple[str, dict]:
        return self._inner.complete_optimizer_messages(
            _inject_firewall_messages(messages), max_tokens=max_tokens
        )

    def complete_tool_call(
        self, system: str, user: str, tool: dict, *, max_tokens: int = 16384
    ) -> dict:
        """Tool call through the optimizer path (GT firewall injected)."""
        return self._inner.complete_tool_call(
            _inject_firewall_system(system), user, tool, max_tokens=max_tokens
        )

    def complete_target(self, *args, **kwargs) -> str:
        raise RuntimeError("optimizer-only client cannot call target")

    def complete_target_messages(self, *args, **kwargs) -> str:
        raise RuntimeError("optimizer-only client cannot call target")


# ── OpenAI-compatible standalone client ──────────────────────────────────────


class OpenAICompatLLMClient:
    """Standalone :class:`LLMClient` for OpenAI-compatible endpoints.

    No SkillOpt dependency. Uses ``urllib.request`` directly, thread-safe for
    high concurrency, retries with exponential backoff. Supports Qwen3's
    ``chat_template_kwargs`` for thinking-mode control.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        target_model: str,
        optimizer_model: str,
        *,
        max_tokens: int = 16384,
        temperature: float = 0.7,
        timeout_seconds: float = 300,
        enable_thinking: bool = False,
        retries: int = 5,
        optimizer_json_mode: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.target_model = target_model
        self.optimizer_model = optimizer_model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout_seconds = timeout_seconds
        self.enable_thinking = enable_thinking
        self.retries = retries
        # When True, optimizer (NOT target) calls request a structured JSON
        # object via ``response_format`` — engine-level guarantee for backends
        # like Qwen/vLLM that honor it. Enable only when every optimizer prompt
        # in use returns a top-level JSON object (e.g. the Plan A reflect
        # pipeline); bare-array prompts would be rejected under this mode.
        self.optimizer_json_mode = optimizer_json_mode

    def _chat_url(self) -> str:
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _post(
        self, payload: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self._chat_url(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        effective_timeout = timeout or self.timeout_seconds
        last_err: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                last_err = RuntimeError(
                    f"OpenAI-compat API returned HTTP {e.code}: {body}"
                )
                if 400 <= e.code < 500:
                    raise last_err
            except (urllib.error.URLError, OSError) as e:
                last_err = RuntimeError(f"OpenAI-compat API request failed: {e}")
            time.sleep(min(2 ** attempt, 30))
        raise last_err  # type: ignore[misc]

    def _call(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
        temperature: float,
        *,
        response_format: dict | None = None,
    ) -> tuple[str, dict[str, int]]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": min(max_tokens, self.max_tokens),
            "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
        }
        if response_format is not None:
            payload["response_format"] = response_format
        data = self._post(payload)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenAI-compat API returned no choices: {data}")
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        usage = data.get("usage") or {}
        usage_info = {
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
            "total_tokens": int(usage.get("total_tokens", 0)),
        }
        return text, usage_info

    def complete_target(
        self, system: str, user: str, *, max_tokens: int = 4096, temperature: float = 0.0
    ) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        text, _ = self._call(messages, self.target_model, max_tokens, temperature)
        return text

    def complete_target_messages(
        self, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.0
    ) -> str:
        text, _ = self._call(list(messages), self.target_model, max_tokens, temperature)
        return text

    def complete_target_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> dict:
        """Target-path multi-turn completion with native OpenAI function-calling.

        Returns the raw assistant message ``{"content", "tool_calls"}`` so an
        env's task-execution agent can drive a tool-call ReAct loop. No
        ground-truth firewall is applied (this is the frozen task agent, not the
        optimizer).
        """
        payload: dict[str, Any] = {
            "model": self.target_model,
            "messages": list(messages),
            "max_tokens": min(max_tokens, self.max_tokens),
            "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
            "tools": tools,
            "tool_choice": tool_choice,
        }
        data = self._post(payload)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenAI-compat API returned no choices: {data}")
        message = choices[0].get("message") or {}
        return {
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls"),
        }

    def complete_optimizer(
        self, system: str, user: str, *, max_tokens: int = 4096
    ) -> tuple[str, dict]:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return self._call(
            messages, self.optimizer_model, max_tokens, self.temperature,
            response_format=self._optimizer_response_format(),
        )


    def complete_optimizer_messages(
        self, messages: list[dict], *, max_tokens: int = 4096
    ) -> tuple[str, dict]:
        return self._call(
            list(messages), self.optimizer_model, max_tokens, self.temperature,
            response_format=self._optimizer_response_format(),
        )

    def complete_tool_call(
        self, system: str, user: str, tool: dict, *, max_tokens: int = 16384
    ) -> dict:
        """Call the LLM with a tool definition, return the tool call arguments as a dict.

        Uses forced tool_choice so the LLM MUST call the specified function.
        Returns the parsed arguments dict. Raises on failure.
        """
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        func_name = tool["function"]["name"]
        payload: dict[str, Any] = {
            "model": self.optimizer_model,
            "messages": messages,
            "max_tokens": min(max_tokens, self.max_tokens),
            "temperature": self.temperature,
            "tools": [tool],
            "tool_choice": {"type": "function", "function": {"name": func_name}},
        }
        data = self._post(payload)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"Tool call returned no choices: {data}")
        message = choices[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            args_str = tool_calls[0].get("function", {}).get("arguments", "{}")
            return json.loads(args_str)
        content = message.get("content") or ""
        if content.strip().startswith("{"):
            return json.loads(content)
        raise RuntimeError(f"Tool call returned no tool_calls and no parseable content")

    def _optimizer_response_format(self) -> dict | None:
        return {"type": "json_object"} if self.optimizer_json_mode else None


def build_clients(cfg: "CSSConfig") -> tuple["TargetOnlyClient", "OptimizerOnlyClient"]:
    """Build an LLM client from ``cfg`` and return its two narrow views.

    Returns ``(target_view, optimizer_view)`` over a single shared client,
    call-isolated per the First Law. When ``cfg.extra["llm_backend"]`` is
    ``"openai_compat"``, uses :class:`OpenAICompatLLMClient` (no SkillOpt
    dependency); otherwise delegates to :class:`RouterLLMClient`.
    """
    backend = cfg.extra.get("llm_backend", "claude") if cfg.extra else "claude"
    extra = cfg.extra or {}
    if backend == "openai_compat":
        inner: LLMClient = OpenAICompatLLMClient(
            base_url=str(extra.get("base_url", "http://localhost:8000/v1")),
            api_key=str(extra.get("api_key", "")),
            target_model=cfg.target_model,
            optimizer_model=cfg.optimizer_model,
            max_tokens=int(extra.get("max_tokens", 16384)),
            temperature=float(extra.get("temperature", 0.7)),
            timeout_seconds=float(extra.get("timeout_seconds", 300)),
            enable_thinking=bool(extra.get("enable_thinking", False)),
            optimizer_json_mode=bool(extra.get("optimizer_json_mode", False)),
        )
    else:
        inner = RouterLLMClient(
            target_model=cfg.target_model,
            optimizer_model=cfg.optimizer_model,
            backend=backend,
        )
    return TargetOnlyClient(inner), OptimizerOnlyClient(inner)
