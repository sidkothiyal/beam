"""Offline end-to-end tests for beam pulse agent mode.

These run without network: the elemental training run is mocked via the
``mock_elemental`` fixture and the LLM is either scripted (``scripted_llm``) or
removed (to exercise graceful fallback). They cover the tool harness, the
decision->action dispatch, the safety caps (max_retries, max_agent_restarts,
crash-loop guard), advisory mode, and the full ``run_check`` integration.
"""

from __future__ import annotations

import json

import pytest

import beam.pulse as P
from beam.agent import tools as agent_tools
from beam.agent.diagnose import AgentVerdict, diagnose_session
from conftest import (
    CUDA_OOM_LOG,
    DISK_FULL_LOG,
    IMPORT_ERROR_LOG,
    Scenario,
)


# ---------------------------------------------------------------------------
# Tool harness — confirm the mocked elemental signals reach each tool
# ---------------------------------------------------------------------------

def _ctx(sess, cfg, logs_dir):
    return agent_tools.ToolContext(sess=sess, cfg=cfg, logs_dir=logs_dir)


def test_read_and_grep_logs(mock_elemental, make_session, agent_config, tmp_logs):
    mock_elemental(Scenario(log_tail=CUDA_OOM_LOG))
    sess = make_session()
    ctx = _ctx(sess, agent_config, tmp_logs)

    log = agent_tools.read_remote_log(ctx, tail_lines=50)
    assert "CUDA out of memory" in log

    hits = agent_tools.grep_remote_log(ctx, pattern="out of memory|Traceback")
    assert "OutOfMemoryError" in hits or "Traceback" in hits

    miss = agent_tools.grep_remote_log(ctx, pattern="no space left")
    assert "no matches" in miss.lower()


def test_disk_and_gpu_tools(mock_elemental, make_session, agent_config, tmp_logs):
    mock_elemental(Scenario(
        disk="Filesystem  Size  Used Avail Use% Mounted on\noverlay  100G  100G  0  100% /",
        gpu_compute_procs=0, gpu_utils=[0, 0, 0, 1, 0],
    ))
    sess = make_session()
    ctx = _ctx(sess, agent_config, tmp_logs)

    disk = agent_tools.check_remote_disk(ctx)
    assert "100%" in disk

    gpu = json.loads(agent_tools.get_gpu_status(ctx))
    assert gpu["gpu_peak_util"] == 1 and gpu["idle"] is True


def test_instance_details_reports_outbid(mock_elemental, make_session, agent_config, tmp_logs):
    mock_elemental(Scenario(is_outbid=True, provider_status="stopped"))
    sess = make_session()
    details = json.loads(agent_tools.get_instance_details(_ctx(sess, agent_config, tmp_logs)))
    assert details["interruptible"] is True          # offer_type == "bid"
    assert details["is_outbid"] is True


def test_wandb_tool_flags_divergence(mock_elemental, make_session, agent_config, tmp_logs):
    mock_elemental(Scenario(wandb_metric_values=[2.1, 1.8, float("nan"), float("inf")]))
    sess = make_session()
    res = json.loads(agent_tools.query_wandb_run(_ctx(sess, agent_config, tmp_logs), metric="loss"))
    assert res["diverged_nan_or_inf"] is True
    assert res["state"] == "crashed"


def test_read_pulse_history_summarizes_root_causes(make_session, agent_config, tmp_logs, seed_history):
    sess = make_session()
    seed_history(sess["session_id"], [
        {"ts": "2026-03-19 03:45:01", "action": "soft_restart", "root_cause": "import_error"},
        {"ts": "2026-03-19 03:50:01", "action": "soft_restart", "root_cause": "import_error"},
    ])
    out = json.loads(agent_tools.read_pulse_history(_ctx(sess, agent_config, tmp_logs)))
    assert out["root_cause_counts"]["import_error"] == 2


# ---------------------------------------------------------------------------
# Trigger classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,job,expected", [
    ("running", "failed", "failed"),
    ("reachable", "crashed", "crashed"),
    ("running", "running", ""),       # healthy -> no agent
    ("running", "finished", ""),      # finished -> no agent
    ("outbid", "unknown", "outbid"),
    ("stopped", "unknown", "provider_down"),
    ("api_error", "unknown", ""),     # transient -> deterministic 'wait', no LLM spend
])
def test_classify_trigger(status, job, expected):
    assert P._classify_trigger(status, job) == expected


# ---------------------------------------------------------------------------
# Graceful fallback when the LLM is unavailable
# ---------------------------------------------------------------------------

def test_graceful_fallback_without_key(monkeypatch, mock_elemental, make_session, agent_config, tmp_logs):
    monkeypatch.setattr("beam.agent.llm.get_secret", lambda name: None)
    mock_elemental(Scenario(log_tail=IMPORT_ERROR_LOG))
    sess = make_session(env_vars={})  # no fallback key in the session snapshot either
    v = diagnose_session(sess, {"instance_status": "running", "job_state": "crashed"}, tmp_logs, agent_config)
    assert v.used_agent is False
    assert "key" in v.error.lower()


# ---------------------------------------------------------------------------
# Scripted agent loop -> verdict
# ---------------------------------------------------------------------------

def test_scripted_agent_investigates_then_escalates(
    scripted_llm, mock_elemental, make_session, agent_config, tmp_logs, seed_history
):
    mock_elemental(Scenario(log_tail=IMPORT_ERROR_LOG))
    sess = make_session()
    seed_history(sess["session_id"], [
        {"ts": "2026-03-19 03:45:01", "action": "soft_restart", "root_cause": "import_error"},
    ])
    scripted_llm([
        [("read_remote_log", {"tail_lines": 50})],
        [("read_pulse_history", {"limit": 20})],
        [("submit_decision", {
            "decision": "escalate", "root_cause": "import_error",
            "reason": "Deterministic ModuleNotFoundError recurs; restarting is futile.",
            "confidence": 0.93,
        })],
    ])
    v = diagnose_session(sess, {"instance_status": "running", "job_state": "crashed"}, tmp_logs, agent_config)
    assert v.used_agent is True
    assert v.decision == "escalate"
    assert v.root_cause == "import_error"
    assert [t["tool"] for t in v.tool_trace] == ["read_remote_log", "read_pulse_history", "submit_decision"]
    assert v.total_tokens == 180       # 3 completions * (50 prompt + 10 completion)


# ---------------------------------------------------------------------------
# Decision -> action dispatch mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("decision,expected_fn", [
    ("soft_restart", "do_soft_restart"),
    ("outbid_restart", "do_auto_restart"),
    ("hard_restart", "do_auto_restart"),
    ("destroy_restart", "do_destroy_and_restart"),
    ("idle_stop", "do_idle_stop"),
    ("escalate", "do_escalate"),
])
def test_dispatch_maps_decision_to_action(monkeypatch, make_session, agent_config, tmp_logs, decision, expected_fn):
    sess = make_session()
    calls = []
    for fn in ("do_soft_restart", "do_auto_restart", "do_destroy_and_restart", "do_idle_stop", "do_escalate"):
        monkeypatch.setattr(P, fn, (lambda name: (lambda *a, **k: calls.append(name) or True))(fn))

    failures = tmp_logs / "sessions" / f".pulse_failures_{sess['session_id']}"
    failures.write_text("0")
    verdict = AgentVerdict(decision=decision, root_cause="x", reason="y", confidence=0.9, used_agent=True)
    action = P._dispatch_agent_decision(
        verdict, sess, sess["session_id"], tmp_logs, agent_config,
        consecutive_failures=0, max_retries=5, failures_file=failures,
        with_lock=lambda fn, *a, **k: fn(*a, **k),
    )
    assert action == decision
    assert expected_fn in calls


# ---------------------------------------------------------------------------
# Safety caps
# ---------------------------------------------------------------------------

def test_crash_loop_guard_overrides_restart(monkeypatch, make_session, agent_config, tmp_logs, seed_history):
    """A restart decision is forced to escalate when the same root_cause repeats."""
    sess = make_session()
    seed_history(sess["session_id"], [
        {"ts": "2026-03-19 03:45:01", "action": "soft_restart", "root_cause": "import_error"},
        {"ts": "2026-03-19 03:50:01", "action": "soft_restart", "root_cause": "import_error"},
        {"ts": "2026-03-19 03:55:01", "action": "soft_restart", "root_cause": "import_error"},
        {"ts": "2026-03-19 04:00:01", "action": "soft_restart", "root_cause": "import_error"},
    ])
    calls = []
    monkeypatch.setattr(P, "do_idle_stop", lambda *a, **k: calls.append("idle_stop"))
    monkeypatch.setattr(P, "do_soft_restart", lambda *a, **k: calls.append("soft_restart") or True)
    # Make the guard compare against "now" deterministically: records carry old
    # timestamps, so widen the window to ensure they fall inside it.
    agent_config.pulse.agent.crash_loop_window = 10**12
    agent_config.pulse.agent.crash_loop_threshold = 4

    failures = tmp_logs / "sessions" / f".pulse_failures_{sess['session_id']}"
    failures.write_text("0")
    verdict = AgentVerdict(decision="soft_restart", root_cause="import_error", reason="r", confidence=0.9, used_agent=True)
    action = P._dispatch_agent_decision(
        verdict, sess, sess["session_id"], tmp_logs, agent_config,
        consecutive_failures=0, max_retries=5, failures_file=failures,
        with_lock=lambda fn, *a, **k: fn(*a, **k),
    )
    assert action == "escalate"
    assert "soft_restart" not in calls         # never restarted in a crash-loop
    assert "idle_stop" in calls                # do_escalate tore down via do_idle_stop
    assert (tmp_logs / "sessions" / f"pulse_escalation_{sess['session_id']}.txt").exists()


def test_max_agent_restarts_cap_escalates(monkeypatch, make_session, agent_config, tmp_logs):
    sess = make_session()
    calls = []
    monkeypatch.setattr(P, "do_idle_stop", lambda *a, **k: calls.append("idle_stop"))
    monkeypatch.setattr(P, "do_soft_restart", lambda *a, **k: calls.append("soft_restart") or True)
    agent_config.pulse.agent.max_agent_restarts = 3

    sd = tmp_logs / "sessions"
    (sd / f".pulse_agent_restarts_{sess['session_id']}").write_text("3")
    failures = sd / f".pulse_failures_{sess['session_id']}"
    failures.write_text("0")
    verdict = AgentVerdict(decision="soft_restart", root_cause="cuda_oom", reason="r", confidence=0.8, used_agent=True)
    action = P._dispatch_agent_decision(
        verdict, sess, sess["session_id"], tmp_logs, agent_config,
        consecutive_failures=0, max_retries=99, failures_file=failures,
        with_lock=lambda fn, *a, **k: fn(*a, **k),
    )
    assert action == "escalate"
    assert "soft_restart" not in calls


def test_max_retries_forces_give_up(monkeypatch, make_session, agent_config, tmp_logs):
    sess = make_session()
    calls = []
    monkeypatch.setattr(P, "do_idle_stop", lambda *a, **k: calls.append("idle_stop"))
    monkeypatch.setattr(P, "do_soft_restart", lambda *a, **k: calls.append("soft_restart") or True)

    failures = tmp_logs / "sessions" / f".pulse_failures_{sess['session_id']}"
    failures.write_text("5")
    verdict = AgentVerdict(decision="soft_restart", root_cause="cuda_oom", reason="r", confidence=0.8, used_agent=True)
    action = P._dispatch_agent_decision(
        verdict, sess, sess["session_id"], tmp_logs, agent_config,
        consecutive_failures=5, max_retries=5, failures_file=failures,
        with_lock=lambda fn, *a, **k: fn(*a, **k),
    )
    assert action == "give_up"
    assert "soft_restart" not in calls and "idle_stop" in calls


def test_advisory_mode_takes_no_action(monkeypatch, make_session, agent_config, tmp_logs):
    sess = make_session()
    calls = []
    for fn in ("do_soft_restart", "do_auto_restart", "do_destroy_and_restart", "do_idle_stop", "do_escalate"):
        monkeypatch.setattr(P, fn, (lambda name: (lambda *a, **k: calls.append(name)))(fn))
    agent_config.pulse.agent.advisory = True

    failures = tmp_logs / "sessions" / f".pulse_failures_{sess['session_id']}"
    failures.write_text("0")
    verdict = AgentVerdict(decision="hard_restart", root_cause="x", reason="y", confidence=0.9, used_agent=True)
    action = P._dispatch_agent_decision(
        verdict, sess, sess["session_id"], tmp_logs, agent_config,
        consecutive_failures=0, max_retries=5, failures_file=failures,
        with_lock=lambda fn, *a, **k: fn(*a, **k),
    )
    assert action == "advisory:hard_restart"
    assert calls == []        # nothing executed


# ---------------------------------------------------------------------------
# Full run_check integration — the real branch + history record
# ---------------------------------------------------------------------------

def test_run_check_routes_to_agent_and_records_history(
    monkeypatch, scripted_llm, mock_elemental, make_session, tmp_logs, tmp_path
):
    # beam.yaml at repo_root enables agent mode (run_check reloads config from there).
    (tmp_path / "beam.yaml").write_text(
        "project:\n  name: elemental\n"
        "pulse:\n  agent:\n    enabled: true\n"
        "    wandb_entity: elemental-ai\n    wandb_project: elemental\n"
    )
    mock_elemental(Scenario(job_state="crashed", log_tail=DISK_FULL_LOG, disk=(
        "Filesystem  Size  Used Avail Use% Mounted on\noverlay  100G  100G  0  100% /"
    )))
    sess = make_session()
    # Stub teardown so do_escalate doesn't touch the real crontab / provider.
    monkeypatch.setattr(P, "do_idle_stop", lambda *a, **k: None)
    scripted_llm([
        [("check_remote_disk", {})],
        [("submit_decision", {
            "decision": "escalate", "root_cause": "disk_full",
            "reason": "Disk is 100% full; restarting on the same node will fail again.",
            "confidence": 0.9,
        })],
    ])

    from beam.config import load_config
    cfg = load_config()  # for pulse_cfg arg; agent cfg is reloaded from beam.yaml inside run_check
    P.run_check(sess["session_id"], tmp_logs, auto_restart=True, max_retries=5, pulse_cfg=cfg.pulse)

    hist = (tmp_logs / "sessions" / f"pulse_history_{sess['session_id']}.jsonl").read_text().splitlines()
    last = json.loads(hist[-1])
    assert last["instance_status"] == "running"
    assert last["job_state"] == "crashed"
    assert last["action"] == "escalate"
    assert last["agent_used"] is True
    assert last["decision"] == "escalate"
    assert last["root_cause"] == "disk_full"
    assert "check_remote_disk" in last["tool_calls"]
