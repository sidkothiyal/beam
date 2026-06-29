"""Tests for the project doc + unified per-job log features (offline)."""

from __future__ import annotations

import json
import types

import beam.pulse as P
from beam import log
from beam.agent import project_doc as pd
from beam.agent.diagnose import diagnose_session
from beam.config import load_config
from conftest import IMPORT_ERROR_LOG, Scenario


# ---------------------------------------------------------------------------
# Unit: project_doc marker-splice
# ---------------------------------------------------------------------------

def test_project_doc_create_from_template(tmp_path):
    cfg = load_config()
    assert pd.read_project_doc(tmp_path, cfg) == ""
    assert pd.update_agent_section(tmp_path, cfg, "- on outbid: restart")
    text = (tmp_path / "BEAM_AGENT.md").read_text()
    assert pd.AGENT_NOTES_START in text and pd.AGENT_NOTES_END in text
    assert "on outbid: restart" in text and "Operator-authored" in text


def test_project_doc_preserves_human_and_replaces_section(tmp_path):
    cfg = load_config()
    human = "# My Project\n\nHUMAN: batch>64 OOMs.\n\n"
    (tmp_path / "BEAM_AGENT.md").write_text(
        human + pd.AGENT_NOTES_START + "\nold notes\n" + pd.AGENT_NOTES_END + "\n"
    )
    pd.update_agent_section(tmp_path, cfg, "new durable notes")
    text = (tmp_path / "BEAM_AGENT.md").read_text()
    assert "HUMAN: batch>64 OOMs." in text          # human content preserved
    assert "new durable notes" in text and "old notes" not in text  # section replaced


def test_project_doc_appends_markers_when_absent(tmp_path):
    cfg = load_config()
    (tmp_path / "BEAM_AGENT.md").write_text("# Plain doc\nno markers here\n")
    pd.update_agent_section(tmp_path, cfg, "appended notes")
    text = (tmp_path / "BEAM_AGENT.md").read_text()
    assert "no markers here" in text and pd.AGENT_NOTES_START in text and "appended notes" in text


def test_project_doc_read_cap(tmp_path):
    cfg = load_config()
    (tmp_path / "BEAM_AGENT.md").write_text("x" * 20000)
    assert len(pd.read_project_doc(tmp_path, cfg).encode()) <= cfg.pulse.agent.project_doc_max_bytes


def test_project_doc_disabled_when_blank(tmp_path):
    cfg = load_config()
    cfg.pulse.agent.project_doc = ""
    assert pd.doc_path(tmp_path, cfg) is None
    assert pd.update_agent_section(tmp_path, cfg, "x") is False


# ---------------------------------------------------------------------------
# Unit: log file sink
# ---------------------------------------------------------------------------

def test_log_sink_mirrors_even_in_quiet(tmp_path):
    p = tmp_path / "job.log"
    log.set_verbosity(quiet=True)
    try:
        log.attach_job_log(p)
        log.step("provisioning")
        log.info("ip=1.2.3.4")
        log.banner(3, 7, "Sync code")
        log.detach_job_log()
    finally:
        log.set_verbosity(quiet=False)
    content = p.read_text()
    assert "STEP  provisioning" in content
    assert "INFO  ip=1.2.3.4" in content
    assert "Sync code" in content


# ---------------------------------------------------------------------------
# Project doc is injected into the agent prompt
# ---------------------------------------------------------------------------

def test_project_doc_injected_into_prompt(
    monkeypatch, mock_elemental, make_session, agent_config, tmp_logs, write_project_doc
):
    import litellm
    monkeypatch.setenv("NVIDIA_API_KEY", "test")
    write_project_doc("# Proj\nHINT: on outbid just restart, never escalate.\n")
    mock_elemental(Scenario(log_tail="ok\n"))
    sess = make_session()

    captured = {}

    def completion(*a, **k):
        captured["messages"] = k["messages"]
        fn = types.SimpleNamespace(name="submit_decision", arguments=json.dumps(
            {"decision": "wait", "root_cause": "x", "reason": "y", "confidence": 0.5}))
        msg = types.SimpleNamespace(content="", tool_calls=[types.SimpleNamespace(id="c1", function=fn)])
        usage = types.SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)

    monkeypatch.setattr(litellm, "completion", completion)

    v = diagnose_session(sess, {"instance_status": "running", "job_state": "crashed"}, tmp_logs, agent_config)
    assert v.used_agent
    user_msg = captured["messages"][1]["content"]
    assert "Project knowledge" in user_msg
    assert "on outbid just restart" in user_msg


# ---------------------------------------------------------------------------
# read_job_log tool
# ---------------------------------------------------------------------------

def test_read_job_log_tool(make_session, agent_config, tmp_logs, seed_job_log):
    from beam.agent.tools import ToolContext, read_job_log
    sess = make_session()
    seed_job_log(sess["session_id"], "[2026-03-19 03:00:00] STEP  Ensure machine\n"
                                     "[2026-03-19 03:01:00] DONE  Remote session started.\n")
    out = read_job_log(ToolContext(sess=sess, cfg=agent_config, logs_dir=tmp_logs))
    assert "Ensure machine" in out and "Remote session started" in out


# ---------------------------------------------------------------------------
# run_check integration — doc written on success, unified log appended
# ---------------------------------------------------------------------------

def _write_beam_yaml(tmp_path, *, advisory: bool):
    (tmp_path / "beam.yaml").write_text(
        "project:\n  name: elemental\n"
        "pulse:\n  agent:\n    enabled: true\n"
        f"    advisory: {'true' if advisory else 'false'}\n"
        "    wandb_entity: elemental-ai\n    wandb_project: elemental\n"
    )


def test_run_check_writes_doc_on_success_and_appends_job_log(
    monkeypatch, scripted_llm, mock_elemental, make_session, tmp_logs, tmp_path, write_project_doc
):
    _write_beam_yaml(tmp_path, advisory=False)
    write_project_doc("# Elemental\nHuman: spot training; compression AE.\n")
    mock_elemental(Scenario(job_state="crashed", log_tail=IMPORT_ERROR_LOG))
    sess = make_session()
    monkeypatch.setattr(P, "do_idle_stop", lambda *a, **k: None)   # escalate teardown stub
    scripted_llm([
        [("read_job_log", {})],
        [("submit_decision", {
            "decision": "escalate", "root_cause": "import_error",
            "reason": "Recurring ModuleNotFoundError.",
            "confidence": 0.9,
            "project_doc_update": "- import_error on launch ⇒ deterministic; escalate, do not restart.",
        })],
    ])

    cfg = load_config()
    P.run_check(sess["session_id"], tmp_logs, auto_restart=True, max_retries=5, pulse_cfg=cfg.pulse)

    # Doc updated in the agent-managed section, human content preserved.
    doc = (tmp_path / "BEAM_AGENT.md").read_text()
    assert "import_error on launch" in doc
    assert "Human: spot training" in doc

    # History records the doc update + the unified job log got this check's output.
    hist = (tmp_logs / "sessions" / f"pulse_history_{sess['session_id']}.jsonl").read_text().splitlines()
    last = json.loads(hist[-1])
    assert last["decision"] == "escalate"
    assert last["project_doc_updated"] is True
    job_log = (tmp_logs / "sessions" / f"{sess['session_id']}.log").read_text()
    assert "instance=running job=crashed" in job_log


def test_run_check_advisory_does_not_write_doc(
    monkeypatch, scripted_llm, mock_elemental, make_session, tmp_logs, tmp_path, write_project_doc
):
    _write_beam_yaml(tmp_path, advisory=True)
    write_project_doc("# Elemental\nHuman notes.\n")
    mock_elemental(Scenario(job_state="crashed", log_tail=IMPORT_ERROR_LOG))
    sess = make_session()
    scripted_llm([
        [("submit_decision", {
            "decision": "escalate", "root_cause": "import_error", "reason": "r", "confidence": 0.9,
            "project_doc_update": "- should NOT be written in advisory mode.",
        })],
    ])

    cfg = load_config()
    P.run_check(sess["session_id"], tmp_logs, auto_restart=True, max_retries=5, pulse_cfg=cfg.pulse)

    doc = (tmp_path / "BEAM_AGENT.md").read_text()
    assert "should NOT be written" not in doc       # advisory => no doc write
    hist = (tmp_logs / "sessions" / f"pulse_history_{sess['session_id']}.jsonl").read_text().splitlines()
    last = json.loads(hist[-1])
    assert last["action"] == "advisory:escalate"
    assert last["project_doc_updated"] is False
