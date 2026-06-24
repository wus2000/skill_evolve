"""LLM client abstractions with target/optimizer call isolation.

The CSS design's First Law (LLM-Code division) requires the frozen TASK agent to
be call-isolated from the OPTIMIZER: rollout code must only ever be able to call
the target model, and analysis/optimization code must only ever be able to call
the optimizer model. We enforce this at the call boundary with two narrow
capability views (:class:`TargetOnlyClient` / :class:`OptimizerOnlyClient`) that
wrap a single underlying :class:`LLMClient`.

The concrete :class:`RouterLLMClient` adapts SkillOpt's model backends. The
backend module (claude/azure/codex) is injected at construction and resolved
directly to the concrete SkillOpt backend module — we do NOT route through
``router._ACTIVE_BACKEND`` / ``set_backend`` (whose alias normalization is
internally inconsistent for the claude backend). The call-isolation guarantee
(First Law) comes from the narrow capability views below, NOT from backend-global
state.

Caveat: each ``complete_*`` call DOES set the backend module's process-global
target/optimizer deployment (``set_target_deployment`` / ``set_optimizer_deployment``)
to this client's model. Under ``batch_rollout``'s ThreadPoolExecutor all concurrent
target callers set the identical ``target_model``, so this is race-benign for
correctness; a process that shares one client across target and optimizer work
must assume all concurrent callers request the same target/optimizer model.

All SkillOpt imports are lazy (inside methods) so this module imports cleanly in
environments without SkillOpt installed (tests use :class:`StubLLMClient`).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

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
    ) -> None:
        self.target_fn = target_fn or (lambda system, user: "stub-target-response")
        self.optimizer_fn = optimizer_fn or (lambda system, user: "stub-optimizer-response")

    def complete_target(
        self, system: str, user: str, *, max_tokens: int = 4096, temperature: float = 0.0
    ) -> str:
        del max_tokens, temperature
        return self.target_fn(system, user)

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

    def complete_optimizer(self, *args, **kwargs) -> tuple[str, dict]:
        raise RuntimeError("target-only client cannot call optimizer")

    def complete_optimizer_messages(self, *args, **kwargs) -> tuple[str, dict]:
        raise RuntimeError("target-only client cannot call optimizer")


class OptimizerOnlyClient:
    """Narrow view: exposes only the optimizer capability (First Law).

    Analysis / optimization code receives one of these so it is structurally
    incapable of calling the frozen target agent.
    """

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner

    def complete_optimizer(
        self, system: str, user: str, *, max_tokens: int = 4096
    ) -> tuple[str, dict]:
        return self._inner.complete_optimizer(system, user, max_tokens=max_tokens)

    def complete_optimizer_messages(
        self, messages: list[dict], *, max_tokens: int = 4096
    ) -> tuple[str, dict]:
        return self._inner.complete_optimizer_messages(messages, max_tokens=max_tokens)

    def complete_target(self, *args, **kwargs) -> str:
        raise RuntimeError("optimizer-only client cannot call target")


def build_clients(cfg: "CSSConfig") -> tuple["TargetOnlyClient", "OptimizerOnlyClient"]:
    """Build a router client from ``cfg`` and return its two narrow views.

    Returns ``(target_view, optimizer_view)`` over a single shared
    :class:`RouterLLMClient`, so both halves use the same backend yet remain
    call-isolated per the First Law.
    """
    backend = cfg.extra.get("llm_backend", "claude") if cfg.extra else "claude"
    inner = RouterLLMClient(
        target_model=cfg.target_model,
        optimizer_model=cfg.optimizer_model,
        backend=backend,
    )
    return TargetOnlyClient(inner), OptimizerOnlyClient(inner)
