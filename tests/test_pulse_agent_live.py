"""Live end-to-end tests against NVIDIA NIM (real LLM, mocked elemental signals).

These exercise the *real* agent loop — the configured NIM model drives the tools
and makes the actual recovery decision — over a fully mocked elemental training
run. The only real thing is the LLM round-trip (and tool-calling); SSH, provider,
and wandb are simulated.

Skipped automatically when ``NVIDIA_API_KEY`` is not set. Assertions are
deliberately lenient (decision *categories*, not exact strings) because LLM
output varies; what they pin down is the behavior that matters: a transient
failure keeps the job alive, a deterministic failure is NOT blindly restarted,
and tool-calling actually works against the configured model.
"""

from __future__ import annotations

import os

import pytest

from beam.agent.diagnose import diagnose_session
from conftest import IMPORT_ERROR_LOG, Scenario

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("NVIDIA_API_KEY"), reason="NVIDIA_API_KEY not set"),
]

# Decisions that re-run the same (broken) code — wrong for a deterministic bug.
_BLIND_RESTARTS = {"soft_restart", "outbid_restart", "hard_restart", "destroy_restart"}
# Decisions that abandon a recoverable job — wrong for a transient preemption.
_GIVE_UPS = {"escalate", "give_up", "idle_stop"}


def _live_cfg():
    from beam.config import load_config
    cfg = load_config()
    cfg.pulse.agent.enabled = True
    cfg.pulse.agent.wandb_entity = "elemental-ai"
    cfg.pulse.agent.wandb_project = "elemental"
    # Bounded but with enough room for the agent to use several tools.
    cfg.pulse.agent.llm.max_tool_iterations = 10
    return cfg


def test_live_gateway_smoke():
    from beam.agent.llm import AgentLLM
    cfg = _live_cfg()
    llm = AgentLLM(cfg.pulse.agent.llm)
    assert llm.available()
    reply = llm.complete("You are a health check.", "Reply with exactly: OK")
    assert reply.strip()


def test_live_spot_preemption_keeps_job_alive(mock_elemental, make_session, tmp_logs):
    """An outbid spot instance is recoverable — the agent should NOT give up."""
    mock_elemental(Scenario(
        job_state="unknown", is_outbid=True, provider_status="stopped",
        log_tail="Train Epoch 2: 50000it [04:00:00, 3.4it/s, loss=1.6]\n",
    ))
    sess = make_session()
    obs = {
        "instance_status": "outbid", "job_state": "unknown",
        "provider": "vast.ai", "interruptible": True, "offer_type": "bid",
        "consecutive_failures": 1, "dph_total": 0.2,
    }
    v = diagnose_session(sess, obs, tmp_logs, _live_cfg())

    assert v.used_agent is True
    assert v.tool_trace, "the model should have called at least one tool"
    assert v.root_cause
    assert v.decision not in _GIVE_UPS, f"outbid is recoverable; got {v.decision} ({v.reason})"


def test_live_crash_loop_not_blindly_restarted(mock_elemental, make_session, tmp_logs, seed_history):
    """A deterministic ImportError that already recurred must not be restarted again."""
    mock_elemental(Scenario(job_state="crashed", log_tail=IMPORT_ERROR_LOG))
    sess = make_session()
    seed_history(sess["session_id"], [
        {"ts": "2026-03-19 03:45:01", "action": "soft_restart", "root_cause": "import_error", "job_state": "crashed"},
        {"ts": "2026-03-19 03:50:01", "action": "soft_restart", "root_cause": "import_error", "job_state": "crashed"},
        {"ts": "2026-03-19 03:55:01", "action": "soft_restart", "root_cause": "import_error", "job_state": "crashed"},
        {"ts": "2026-03-19 04:00:01", "action": "soft_restart", "root_cause": "import_error", "job_state": "crashed"},
    ])
    obs = {
        "instance_status": "running", "job_state": "crashed", "exit_code": "1",
        "provider": "vast.ai", "consecutive_failures": 1,
    }
    v = diagnose_session(sess, obs, tmp_logs, _live_cfg())

    assert v.used_agent is True
    assert v.tool_trace
    assert v.decision not in _BLIND_RESTARTS, (
        f"deterministic crash-loop should not be restarted; got {v.decision} ({v.reason})"
    )


def test_live_project_doc_steers_decision(mock_elemental, make_session, tmp_logs, write_project_doc):
    """The canonical project doc is fed to the agent and must steer its decision."""
    write_project_doc(
        "# Elemental\n\n"
        "## Operator guidance\n"
        "- This job runs on Vast.ai spot and gets outbid constantly. On ANY outbid / "
        "spot preemption, ALWAYS just restart it — never escalate, never give up.\n"
    )
    mock_elemental(Scenario(
        job_state="unknown", is_outbid=True, provider_status="stopped",
        log_tail="Train Epoch 1: 30000it [02:00:00, 3.4it/s, loss=1.7]\n",
    ))
    sess = make_session()
    obs = {
        "instance_status": "outbid", "job_state": "unknown",
        "provider": "vast.ai", "interruptible": True, "consecutive_failures": 1,
    }
    v = diagnose_session(sess, obs, tmp_logs, _live_cfg())

    assert v.used_agent is True
    assert v.tool_trace
    # The doc explicitly says: on outbid, restart — never escalate/give up.
    assert v.decision not in _GIVE_UPS, f"doc says restart on outbid; got {v.decision} ({v.reason})"


def test_live_nan_divergence_not_blindly_restarted(mock_elemental, make_session, tmp_logs):
    """Training diverged to NaN (a config/pipeline problem) — don't blindly restart."""
    mock_elemental(Scenario(
        job_state="failed", exit_code="1",
        log_tail="Train Epoch 0: 1200it [03:10, 6.3it/s, loss=nan]\nRuntimeError: Function 'LogBackward' returned nan values.\n",
        wandb_state="crashed",
        wandb_metric_values=[2.3, 5.1, 38.0, float("nan"), float("nan")],
    ))
    sess = make_session()
    obs = {
        "instance_status": "running", "job_state": "failed", "exit_code": "1",
        "provider": "vast.ai", "consecutive_failures": 2,
    }
    v = diagnose_session(sess, obs, tmp_logs, _live_cfg())

    assert v.used_agent is True
    assert v.tool_trace
    assert v.decision not in _BLIND_RESTARTS, (
        f"NaN divergence is deterministic; should not be restarted; got {v.decision} ({v.reason})"
    )
