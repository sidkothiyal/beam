"""Orchestrator: run the agentic tool loop and return a structured verdict.

``diagnose_session`` is pure decision-making — it performs NO side effects (no
``do_*`` calls, no counter writes). On ANY failure (no key, litellm missing, LLM
error, tool-loop exhaustion) it returns ``AgentVerdict(used_agent=False, ...)``
so the caller falls back to the deterministic pulse matrix. A monitor must never
be less reliable than before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beam import log
from beam.config import BeamConfig

from .llm import AgentLLM
from .project_doc import read_project_doc
from .prompts import SYSTEM_PROMPT, build_observation_summary
from .tools import DECISIONS, ToolContext, build_tool_schemas, make_dispatch


@dataclass
class AgentVerdict:
    decision: str = "defer"
    root_cause: str = ""
    reason: str = ""
    confidence: float = 0.0
    used_agent: bool = False          # False => caller must use deterministic fallback
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""
    project_doc_update: str = ""       # proposed notes; written by pulse on success
    project_doc_updated: bool = False  # set True by pulse after a successful write

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _coerce_verdict(decision: dict[str, Any], result) -> AgentVerdict:
    raw = str(decision.get("decision", "")).strip()
    if raw not in DECISIONS:
        # Unknown/empty decision — be conservative.
        raw = "wait"
    try:
        conf = float(decision.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    return AgentVerdict(
        decision=raw,
        root_cause=str(decision.get("root_cause", "") or ""),
        reason=str(decision.get("reason", "") or ""),
        confidence=conf,
        used_agent=True,
        tool_trace=result.tool_trace,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        project_doc_update=str(decision.get("project_doc_update", "") or ""),
    )


def diagnose_session(
    sess: dict[str, Any],
    observations: dict[str, Any],
    logs_dir: Path,
    cfg: BeamConfig,
) -> AgentVerdict:
    """Run the agent loop for one unhealthy check. Never raises."""
    agent_cfg = cfg.pulse.agent
    try:
        fallback_key = (sess.get("env_vars") or {}).get(agent_cfg.llm.api_key_env)
        llm = AgentLLM(agent_cfg.llm, fallback_key=fallback_key)
        if not llm.available():
            return AgentVerdict(used_agent=False, error="no API key for agent LLM")

        ctx = ToolContext(sess=sess, cfg=cfg, logs_dir=logs_dir)
        tools = build_tool_schemas(agent_cfg)
        dispatch = make_dispatch(ctx)
        repo_root = Path(sess.get("repo_root") or ".")
        project_doc = read_project_doc(repo_root, cfg)
        user = build_observation_summary(observations, project_doc=project_doc)

        result = llm.run_tool_loop(
            SYSTEM_PROMPT, user, tools, dispatch,
            max_iterations=agent_cfg.llm.max_tool_iterations,
        )
        verdict = _coerce_verdict(result.decision, result)
        log.info(
            f"  agent: decision={verdict.decision} root_cause={verdict.root_cause} "
            f"confidence={verdict.confidence:.2f} "
            f"tools={[t['tool'] for t in verdict.tool_trace]} "
            f"tokens={verdict.total_tokens}"
        )
        return verdict
    except Exception as exc:  # noqa: BLE001
        log.warn(f"  agent diagnosis unavailable ({exc}) — using deterministic fallback")
        return AgentVerdict(used_agent=False, error=str(exc))
