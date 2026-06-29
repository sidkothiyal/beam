"""LiteLLM gateway: one OpenAI-compatible surface for NIM / OpenRouter / OpenAI.

Centralizes provider -> (model, api_key, api_base) resolution so the rest of the
agent never branches on provider. Ported from the ``kb`` project and extended
with an OpenAI-style tool-calling loop (``run_tool_loop``) for agentic diagnosis.

litellm is imported lazily inside call sites so the base ``beam`` install stays
lean; absence raises ``LLMUnavailable``, which the orchestrator catches and
degrades to the deterministic pulse matrix.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from beam.config import AgentLLMConfig

from .secrets import get_secret

# OpenAI-compatible base URLs for non-OpenAI providers.
PROVIDER_BASE: dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "nim": "https://integrate.api.nvidia.com/v1",
}


class LLMUnavailable(RuntimeError):
    """Raised when the LLM backend cannot be used (no key / litellm missing)."""


@dataclass
class ToolLoopResult:
    decision: dict[str, Any]              # raw args from the terminal submit_decision call
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    iterations: int = 0


class AgentLLM:
    def __init__(self, cfg: AgentLLMConfig, *, fallback_key: str | None = None):
        self.provider = cfg.provider
        self.model_name = cfg.model
        self.base_url = cfg.base_url or PROVIDER_BASE.get(cfg.provider)
        # Resolve the key from the 3-tier resolver; fall back to a value the
        # caller pulled from the session env_vars snapshot (opportunistic).
        self.api_key = get_secret(cfg.api_key_env) or fallback_key or None
        self.timeout = int(os.environ.get("BEAM_LLM_TIMEOUT", str(cfg.timeout)))
        self.retries = int(os.environ.get("BEAM_LLM_RETRIES", str(cfg.retries)))
        self.max_tokens = cfg.max_tokens
        self.temperature = cfg.temperature

    # -- helpers ---------------------------------------------------------------

    def _model_str(self) -> str:
        # LiteLLM wants an explicit "openai/" prefix when handed a custom base_url.
        if self.provider in ("openrouter", "nim", "local"):
            return f"openai/{self.model_name}"
        return self.model_name

    def available(self) -> bool:
        """True if a key is present (or the backend is keyless 'local')."""
        if self.provider == "local":
            return True
        return bool(self.api_key)

    def _litellm(self):
        try:
            import litellm  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise LLMUnavailable(
                "litellm not installed — pip install 'beam-deploy[agent]'"
            ) from e
        return litellm

    def _require_key(self) -> None:
        if not self.available():
            raise LLMUnavailable(
                f"No API key for provider '{self.provider}'. "
                f"Run: beam pulse agent set-key --provider {self.provider}"
            )

    # -- plain completion ------------------------------------------------------

    def complete(self, system: str, user: str, *, max_tokens: Optional[int] = None) -> str:
        litellm = self._litellm()
        self._require_key()
        resp = litellm.completion(
            model=self._model_str(),
            api_key=self.api_key,
            api_base=self.base_url,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self.temperature,
            max_tokens=max_tokens or self.max_tokens,
            timeout=self.timeout,
            num_retries=self.retries,
        )
        return resp["choices"][0]["message"]["content"] or ""

    # -- agentic tool-calling loop ---------------------------------------------

    def run_tool_loop(
        self,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        dispatch: Callable[[str, dict[str, Any]], str],
        *,
        max_iterations: int,
        terminal_tool: str = "submit_decision",
    ) -> ToolLoopResult:
        """Drive an OpenAI-style tool-calling loop until *terminal_tool* is called.

        ``dispatch(name, args)`` executes a tool and returns a string result.
        The loop terminates when the model calls *terminal_tool* (whose args are
        returned as ``decision``) or when *max_iterations* is exhausted (raises).
        """
        litellm = self._litellm()
        self._require_key()

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        result = ToolLoopResult(decision={})

        for i in range(max_iterations):
            result.iterations = i + 1
            resp = litellm.completion(
                model=self._model_str(),
                api_key=self.api_key,
                api_base=self.base_url,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                timeout=self.timeout,
                num_retries=self.retries,
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                result.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                result.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)

            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []

            if not tool_calls:
                # Model replied with prose instead of a tool call. Nudge it once
                # toward the terminal decision tool; if it keeps refusing we hit
                # max_iterations and raise (the orchestrator then falls back).
                messages.append({"role": "assistant", "content": msg.content or ""})
                messages.append({
                    "role": "user",
                    "content": f"You must call the `{terminal_tool}` tool to record your decision.",
                })
                continue

            # Echo the assistant turn (with its tool calls) back into the thread.
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })

            terminal_seen = False
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except (ValueError, TypeError):
                    args = {}

                if name == terminal_tool:
                    result.decision = args
                    result.tool_trace.append({"tool": name, "args": args, "summary": "decision recorded"})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": "decision recorded",
                    })
                    terminal_seen = True
                    continue

                tool_result = dispatch(name, args)
                # Keep the trace summary compact (logs/tokens).
                result.tool_trace.append({
                    "tool": name,
                    "args": args,
                    "summary": tool_result[:300],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                })

            if terminal_seen:
                return result

        raise LLMUnavailable(
            f"Agent did not call {terminal_tool} within {max_iterations} iterations"
        )
