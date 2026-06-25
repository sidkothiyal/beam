"""Shared fixtures for beam pulse agent tests.

The core idea: an elemental training run on a Vast.ai spot instance is *fully
mocked* — every signal the agent can observe (remote logs, GPU, disk, provider
status/outbid, wandb metrics, pulse history) is synthesized from a ``Scenario``.
Tests then either drive the agent with a scripted (offline) LLM to exercise the
mechanics, or with the live NIM model to exercise real decision-making.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import types
from dataclasses import dataclass, field

import pytest


# ---------------------------------------------------------------------------
# Scenario — a mocked elemental training state
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    """Everything the agent's tools can observe about a (mocked) training run."""
    name: str = "elemental"
    job_state: str = "crashed"           # finished | failed | crashed | running | unknown
    exit_code: str = "1"
    log_tail: str = ""                   # remote {session}.log tail
    disk: str = (
        "Filesystem      Size  Used Avail Use% Mounted on\n"
        "overlay         100G   42G   58G  42% /"
    )
    gpu_compute_procs: int = 0           # nvidia-smi compute-apps count
    gpu_utils: list = field(default_factory=lambda: [0, 0, 0, 0, 0])
    provider_status: str = "running"     # vast cur_state
    is_outbid: bool = False
    offers: list = field(default_factory=list)
    wandb_state: str = "crashed"
    wandb_metric_values: list = field(default_factory=list)
    wandb_runtime: float = 1800.0


def _sentinel_block(job_state: str) -> str:
    """Synthesize the combined output of pulse.check_job_state's SSH probe."""
    finished = failed = running = ""
    tmux = "dead"
    if job_state == "finished":
        finished = "1700000000"
    elif job_state == "failed":
        failed = "1|1700000000"
    elif job_state == "crashed":
        running = "12345|1700000000"   # RUNNING present but tmux dead -> crashed
    elif job_state == "running":
        running = "12345|1700000000"
        tmux = "alive"
    return (
        f"---FINISHED---\n{finished}\n"
        f"---FAILED---\n{failed}\n"
        f"---RUNNING---\n{running}\n"
        f"---TMUX---\n{tmux}\n"
    )


def _fake_ssh_run(scn: Scenario):
    """Build a replacement for SSHClient.run that answers from *scn*."""
    def fake_run(self, command, *, check=True, input=None):  # noqa: A002
        c = command
        out = ""
        if "---FINISHED---" in c:                      # check_job_state probe
            out = _sentinel_block(scn.job_state)
        elif c.lstrip().startswith("grep"):            # grep_remote_log
            pattern = None
            try:
                parts = shlex.split(c)
                if "--" in parts:
                    pattern = parts[parts.index("--") + 1]
            except ValueError:
                pattern = None
            if pattern:
                try:
                    out = "\n".join(
                        ln for ln in scn.log_tail.splitlines()
                        if re.search(pattern, ln, re.I)
                    )
                except re.error:
                    out = "\n".join(
                        ln for ln in scn.log_tail.splitlines()
                        if pattern.lower() in ln.lower()
                    )
        elif "tail -n" in c and ".log" in c:            # read_remote_log
            out = scn.log_tail
        elif "df -h" in c:                              # check_remote_disk
            out = scn.disk
        elif "--query-compute-apps" in c:              # check_gpu_usage step 1
            out = str(scn.gpu_compute_procs)
        elif "--query-gpu=utilization" in c:           # check_gpu_usage step 2
            out = "\n".join(str(u) for u in scn.gpu_utils)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=out, stderr="")
    return fake_run


@pytest.fixture
def mock_elemental(monkeypatch):
    """Return ``apply(scenario)`` that patches SSH / provider / wandb to simulate it."""
    def apply(scn: Scenario) -> Scenario:
        from beam.ssh import SSHClient
        monkeypatch.setattr(SSHClient, "run", _fake_ssh_run(scn))
        monkeypatch.setattr(SSHClient, "check", lambda self: True)

        from beam.providers.vastai import VastAIProvider
        monkeypatch.setattr(VastAIProvider, "get_status", lambda self, pod_id: scn.provider_status)
        monkeypatch.setattr(VastAIProvider, "is_outbid", lambda self, pod_id: scn.is_outbid)
        monkeypatch.setattr(VastAIProvider, "list_compatible_offers", lambda self, cfg: scn.offers)

        fake_run_obj = types.SimpleNamespace(
            name=scn.name,
            state=scn.wandb_state,
            summary={"_runtime": scn.wandb_runtime},
            history=lambda samples=20, keys=None, pandas=True: [
                {(keys[0] if keys else "loss"): v}
                for v in scn.wandb_metric_values[-samples:]
            ],
        )

        class _FakeApi:
            def __init__(self, *a, **k):
                pass

            def runs(self, path, order=None, per_page=20):
                return [fake_run_obj]

        import wandb
        monkeypatch.setattr(wandb, "Api", _FakeApi)
        return scn
    return apply


# ---------------------------------------------------------------------------
# Session + config fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_logs(tmp_path):
    logs = tmp_path / ".beam" / "logs"
    (logs / "sessions").mkdir(parents=True)
    return logs


@pytest.fixture
def make_session(tmp_logs, tmp_path):
    def _make(session_id: str = "compression-ae-1778297511", **over):
        sess = {
            "session_id": session_id,
            "name": "compression-ae",
            "provider": "vast.ai",
            "pod_id": "33124282",
            "ip": "ssh5.vast.ai",
            "port": "19028",
            "user": "root",
            "identity_file": "",
            "remote_logs_dir": "/workspace/logs/elemental",
            "remote_state_dir": f"/workspace/logs/elemental/.beam_state/{session_id}",
            "remote_code_dir": "/workspace/elemental",
            "remote_home": "/root",
            "tmux_session": session_id,
            "repo_root": str(tmp_path),
            "log_file": str(tmp_logs / "sessions" / f"{session_id}.log"),
            "command": "uv run python -m elemental.train",
            "env_vars": {"WANDB_API_KEY": "wbkey"},
            "machine_config": {
                "name": "default", "offer_type": "bid", "gpu_name": "A100",
                "num_gpus": 1, "price": 0.16, "price_max": 0.225,
            },
            "dph_total": 0.2,
            "vast_api_key": "vk",
            "created_at": "2026-03-19T03:00:00+00:00",
        }
        sess.update(over)
        (tmp_logs / "sessions" / f"{session_id}.json").write_text(json.dumps(sess))
        return sess
    return _make


@pytest.fixture
def agent_config():
    """A BeamConfig with agent mode enabled and wandb wired (offline-safe)."""
    from beam.config import load_config
    cfg = load_config()
    cfg.pulse.agent.enabled = True
    cfg.pulse.agent.wandb_entity = "elemental-ai"
    cfg.pulse.agent.wandb_project = "elemental"
    return cfg


# ---------------------------------------------------------------------------
# Pulse history seeding (for crash-loop tests)
# ---------------------------------------------------------------------------

@pytest.fixture
def seed_history(tmp_logs):
    def _seed(session_id: str, records: list[dict]):
        p = tmp_logs / "sessions" / f"pulse_history_{session_id}.jsonl"
        with open(p, "a") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        return p
    return _seed


@pytest.fixture
def seed_job_log(tmp_logs):
    def _seed(session_id: str, text: str):
        p = tmp_logs / "sessions" / f"{session_id}.log"
        p.write_text(text, encoding="utf-8")
        return p
    return _seed


@pytest.fixture
def write_project_doc(tmp_path):
    """Write a BEAM_AGENT.md at the (mock) repo root and return its path."""
    def _write(content: str):
        p = tmp_path / "BEAM_AGENT.md"
        p.write_text(content, encoding="utf-8")
        return p
    return _write


# ---------------------------------------------------------------------------
# Scripted (offline) LLM
# ---------------------------------------------------------------------------

@pytest.fixture
def scripted_llm(monkeypatch):
    """Patch ``litellm.completion`` to replay scripted tool-call turns.

    ``turns`` is a list of turns; each turn is a list of ``(tool_name, args)``.
    The final turn should call ``submit_decision``. Also sets a dummy
    NVIDIA_API_KEY so ``AgentLLM.available()`` is True without the real key.
    """
    def _script(turns: list[list[tuple]]):
        import litellm
        monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
        state = {"i": 0}

        def completion(*args, **kwargs):
            assert kwargs.get("tools"), "tools must be passed to completion()"
            calls = turns[state["i"]]
            state["i"] += 1
            tool_calls = []
            for j, (name, cargs) in enumerate(calls):
                fn = types.SimpleNamespace(name=name, arguments=json.dumps(cargs))
                tool_calls.append(types.SimpleNamespace(id=f"c{state['i']}_{j}", function=fn))
            msg = types.SimpleNamespace(content="", tool_calls=tool_calls)
            usage = types.SimpleNamespace(prompt_tokens=50, completion_tokens=10)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)

        monkeypatch.setattr(litellm, "completion", completion)
    return _script


# ---------------------------------------------------------------------------
# Canned elemental scenarios (drawn from real failure modes in the logs)
# ---------------------------------------------------------------------------

CUDA_OOM_LOG = """\
Train Epoch 0:  37%|███       | 204/546 [01:12<02:01,  2.81it/s, loss=3.41]
Traceback (most recent call last):
  File "/workspace/elemental/train.py", line 188, in <module>
    main()
  File "/workspace/elemental/train.py", line 142, in train_step
    loss.backward()
torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.10 GiB.
"""

IMPORT_ERROR_LOG = """\
Traceback (most recent call last):
  File "/workspace/elemental/train.py", line 12, in <module>
    from elemental.models.residual_encoder import ResidualEncoder
ModuleNotFoundError: No module named 'elemental.models.residual_encoder'
"""

DISK_FULL_LOG = """\
[2026-06-13 23:45:12] uploading checkpoint to wandb artifacts ...
OSError: [Errno 28] No space left on device: '/root/.cache/wandb/artifacts/tmp/2324546641'
"""

WANDB_TIMEOUT_LOG = """\
Train Epoch 3: 128400it [10:22:11,  3.44it/s, loss=1.88]
wandb: WARNING api: retrying error: Post "https://api.wandb.ai/graphql": context deadline exceeded
Train Epoch 3: 128500it [10:22:41,  3.42it/s, loss=1.87]
"""

HEALTHY_LOG = """\
Train Epoch 5: 64000it [05:01:22,  3.55it/s, loss=1.42]
Train Epoch 5: 64100it [05:01:51,  3.56it/s, loss=1.41]
"""
