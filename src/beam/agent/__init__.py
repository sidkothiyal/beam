"""Pulse agent mode — LLM tool-calling diagnosis over the deterministic monitor.

Public surface kept tiny and lazily imported so the base ``beam`` install never
pays for litellm/wandb. Import ``diagnose_session`` from ``beam.agent.diagnose``
at call time.
"""

from __future__ import annotations

__all__ = ["diagnose_session", "AgentVerdict"]


def __getattr__(name: str):  # lazy re-export, avoids importing litellm at module load
    if name in ("diagnose_session", "AgentVerdict"):
        from .diagnose import AgentVerdict, diagnose_session
        return {"diagnose_session": diagnose_session, "AgentVerdict": AgentVerdict}[name]
    raise AttributeError(name)
