"""Toolset for the pulse diagnosis agent.

Each tool is a function ``(ctx, **args) -> str`` returning a compact text/JSON
result for the model. Tools read remote logs, the pulse recovery history,
instance/spot details, disk usage, GPU status, wandb metrics, and provider
offers — the signals the deterministic monitor never looked at. ``dispatch``
routes a tool name to its function under try/except so a tool error becomes a
string the model can react to, never a crashed loop.

The terminal ``submit_decision`` tool is handled by the LLM loop itself (it ends
the loop) and is therefore present in ``build_tool_schemas`` but NOT in
``_TOOL_FUNCS``.
"""

from __future__ import annotations

import json
import math
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from beam import log
from beam.config import BeamConfig, VastAIMachineConfig
from beam.ssh import SSHClient, SSHInfo

# Decision vocabulary the agent may emit (mirrored in pulse._dispatch_agent_decision).
DECISIONS = [
    "soft_restart", "hard_restart", "outbid_restart", "destroy_restart",
    "idle_stop", "wait", "escalate", "give_up",
]


@dataclass
class ToolContext:
    sess: dict[str, Any]
    cfg: BeamConfig
    logs_dir: Path
    _ssh: Optional[SSHClient] = None

    @property
    def agent_cfg(self):
        return self.cfg.pulse.agent

    @property
    def ssh(self) -> Optional[SSHClient]:
        """Lazily build a single ControlMaster-multiplexed SSH client."""
        if self._ssh is not None:
            return self._ssh
        ip = self.sess.get("ip", "")
        if not ip:
            return None
        info = SSHInfo(
            host=ip,
            port=str(self.sess.get("port", "22")),
            user=self.sess.get("user", "root"),
            identity_file=self.sess.get("identity_file", ""),
        )
        self._ssh = SSHClient(info=info, cfg=self.cfg.ssh)
        return self._ssh


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _logfile(sess: dict[str, Any]) -> str:
    """Reconstruct the remote job log path (matches steps/run_remote.py)."""
    remote_logs_dir = sess.get("remote_logs_dir", "")
    tmux = sess.get("tmux_session", "") or sess.get("session_id", "")
    if not remote_logs_dir or not tmux:
        return ""
    return f"{remote_logs_dir}/{tmux}.log"


def _truncate(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= max_bytes:
        return text
    return data[-max_bytes:].decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def read_remote_log(ctx: ToolContext, tail_lines: int = 200) -> str:
    client = ctx.ssh
    if client is None:
        return "ERROR: no SSH connection (instance has no IP)."
    logfile = _logfile(ctx.sess)
    if not logfile:
        return "ERROR: cannot determine remote log path for this session."
    n = min(int(tail_lines or 200), ctx.agent_cfg.log_tail_lines)
    res = client.run(f"tail -n {n} {shlex.quote(logfile)} 2>/dev/null || true", check=False)
    out = res.stdout.strip()
    if not out:
        return f"(log {logfile} is empty or missing)"
    return _truncate(out, ctx.agent_cfg.log_max_bytes)


def grep_remote_log(ctx: ToolContext, pattern: str, context: int = 3, max_matches: int = 40) -> str:
    client = ctx.ssh
    if client is None:
        return "ERROR: no SSH connection (instance has no IP)."
    logfile = _logfile(ctx.sess)
    if not logfile:
        return "ERROR: cannot determine remote log path for this session."
    if not pattern:
        return "ERROR: empty pattern."
    ctx_n = max(0, min(int(context or 0), 6))
    cap = max(1, min(int(max_matches or 40), 200)) * (2 * ctx_n + 1)
    cmd = (
        f"grep -nEi -C{ctx_n} -- {shlex.quote(pattern)} {shlex.quote(logfile)} "
        f"2>/dev/null | head -n {cap} || true"
    )
    res = client.run(cmd, check=False)
    out = res.stdout.strip()
    if not out:
        return f"(no matches for /{pattern}/ in {logfile})"
    return _truncate(out, ctx.agent_cfg.log_max_bytes)


def read_job_log(ctx: ToolContext, tail_lines: int = 300) -> str:
    """Tail the unified per-job log: all `beam run` + `beam pulse` steps since start."""
    sid = ctx.sess.get("session_id", "")
    path_str = ctx.sess.get("log_file") or str(
        ctx.logs_dir / "sessions" / f"{sid}.log"
    )
    p = Path(path_str)
    if not p.is_file():
        return "(no unified job log found yet)"
    n = min(int(tail_lines or 300), ctx.agent_cfg.job_log_tail_lines)
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError as exc:
        return f"ERROR reading job log: {exc}"
    return _truncate("\n".join(lines), ctx.agent_cfg.log_max_bytes)


def read_pulse_history(ctx: ToolContext, limit: int = 50) -> str:
    sid = ctx.sess.get("session_id", "")
    hist = ctx.logs_dir / "sessions" / f"pulse_history_{sid}.jsonl"
    if not hist.exists():
        return "(no pulse history yet)"
    n = min(int(limit or 50), ctx.agent_cfg.history_lookback)
    lines = hist.read_text().splitlines()[-n:]
    records = []
    for ln in lines:
        try:
            records.append(json.loads(ln))
        except ValueError:
            continue
    # Summarise: counts by (action / root_cause) help the model spot crash-loops.
    action_counts: dict[str, int] = {}
    cause_counts: dict[str, int] = {}
    for r in records:
        action_counts[r.get("action", "?")] = action_counts.get(r.get("action", "?"), 0) + 1
        if r.get("root_cause"):
            cause_counts[r["root_cause"]] = cause_counts.get(r["root_cause"], 0) + 1
    return json.dumps({
        "records_shown": len(records),
        "action_counts": action_counts,
        "root_cause_counts": cause_counts,
        "recent": records[-15:],
    }, default=str)


def get_instance_details(ctx: ToolContext) -> str:
    sess = ctx.sess
    mc = sess.get("machine_config") or {}
    provider = sess.get("provider", "")
    spot = bool(mc.get("spot")) or mc.get("offer_type") == "bid"
    details: dict[str, Any] = {
        "provider": provider,
        "pod_id": sess.get("pod_id", ""),
        "interruptible": spot,
        "offer_type": mc.get("offer_type"),
        "runpod_spot": mc.get("spot"),
        "dph_total": sess.get("dph_total"),
        "created_at": sess.get("created_at"),
        "recovery_policy": sess.get("recovery_policy") or mc.get("recovery_policy"),
    }
    # Live provider status + outbid check (best-effort).
    pod_id = sess.get("pod_id", "")
    try:
        if provider in ("vast.ai", "vastai") and pod_id:
            from beam.providers.vastai import VastAIProvider
            p = VastAIProvider(api_key=sess.get("vast_api_key"))
            details["live_status"] = p.get_status(pod_id)
            details["is_outbid"] = p.is_outbid(pod_id)
        elif provider == "runpod" and pod_id:
            from beam.providers.runpod import RunPodProvider
            p = RunPodProvider(api_key=sess.get("runpod_api_key"))
            details["live_status"] = p.get_status(pod_id)
    except Exception as exc:  # noqa: BLE001
        details["live_status_error"] = str(exc)
    return json.dumps(details, default=str)


def check_remote_disk(ctx: ToolContext, path: str = "/") -> str:
    client = ctx.ssh
    if client is None:
        return "ERROR: no SSH connection (instance has no IP)."
    safe = shlex.quote(path or "/")
    logs_dir = ctx.sess.get("remote_logs_dir", "")
    cmd = f"df -h {safe} 2>/dev/null; echo '---'; df -h 2>/dev/null | tail -n +2"
    if logs_dir:
        cmd += f"; echo '---logsdir---'; du -sh {shlex.quote(logs_dir)} 2>/dev/null || true"
    res = client.run(cmd, check=False)
    return _truncate(res.stdout.strip() or "(df returned nothing)", ctx.agent_cfg.log_max_bytes)


def get_gpu_status(ctx: ToolContext) -> str:
    from beam.pulse import check_gpu_usage
    util = check_gpu_usage(ctx.sess, ctx.cfg.pulse)
    if util < 0:
        return json.dumps({"gpu_peak_util": None, "note": "nvidia-smi unavailable"})
    return json.dumps({
        "gpu_peak_util": util,
        "idle_threshold": ctx.cfg.pulse.gpu_idle_threshold,
        "idle": util < ctx.cfg.pulse.gpu_idle_threshold,
    })


def query_wandb_run(ctx: ToolContext, metric: str = "loss", last_n: int = 20) -> str:
    agent_cfg = ctx.agent_cfg
    if not agent_cfg.use_wandb:
        return "ERROR: wandb querying disabled (pulse.agent.use_wandb=false)."
    entity = agent_cfg.wandb_entity
    project = agent_cfg.wandb_project
    if not (entity and project):
        return "ERROR: wandb entity/project not configured (pulse.agent.wandb_entity/wandb_project)."
    api_key = (ctx.sess.get("env_vars") or {}).get("WANDB_API_KEY", "")
    try:
        import os as _os

        import wandb  # type: ignore
        if api_key:
            _os.environ["WANDB_API_KEY"] = api_key
        api = wandb.Api(timeout=30)
        name = ctx.sess.get("name", "") or ""
        tmux = ctx.sess.get("tmux_session", "") or ctx.sess.get("session_id", "")
        runs = api.runs(f"{entity}/{project}", order="-created_at", per_page=20)
        match = None
        for r in runs:
            rname = (r.name or "")
            if (name and (name in rname or rname in name)) or (tmux and (rname in tmux or tmux in rname)):
                match = r
                break
            if match is None:
                match = r  # newest as a fallback candidate
        if match is None:
            return "(no wandb runs found for this project)"
        hist = []
        try:
            for row in match.history(samples=int(last_n or 20), keys=[metric], pandas=False):
                hist.append(row)
        except Exception:  # noqa: BLE001
            hist = []
        values = [row.get(metric) for row in hist if metric in row]
        bad = any(
            isinstance(v, (int, float)) and (math.isnan(v) or math.isinf(v))
            for v in values
        )
        return json.dumps({
            "run_name": match.name,
            "state": match.state,
            "matched_session": match is not None,
            "metric": metric,
            "recent_values": values[-int(last_n or 20):],
            "diverged_nan_or_inf": bad,
            "runtime_s": (match.summary.get("_runtime") if hasattr(match, "summary") else None),
        }, default=str)
    except ImportError:
        return "ERROR: wandb not installed — pip install 'beam-deploy[agent]'."
    except Exception as exc:  # noqa: BLE001
        return f"ERROR querying wandb: {exc}"


def list_provider_offers(ctx: ToolContext, max_results: int = 5) -> str:
    if not ctx.agent_cfg.allow_server_switch:
        return "ERROR: server switching disabled (pulse.agent.allow_server_switch=false)."
    provider = ctx.sess.get("provider", "")
    mc = ctx.sess.get("machine_config") or {}
    if provider not in ("vast.ai", "vastai"):
        return f"(offer listing only supported for vast.ai; provider={provider})"
    try:
        from beam.providers.vastai import VastAIProvider
        cfg = VastAIMachineConfig(**mc) if mc else VastAIMachineConfig()
        p = VastAIProvider(api_key=ctx.sess.get("vast_api_key"))
        offers = p.list_compatible_offers(cfg)
        top = []
        for o in offers[: max(1, min(int(max_results or 5), 20))]:
            top.append({
                "id": o.get("id"),
                "gpu_name": o.get("gpu_name"),
                "num_gpus": o.get("num_gpus"),
                "dph_total": o.get("dph_total"),
                "min_bid": o.get("min_bid"),
                "computed_bid": o.get("_computed_bid"),
                "dlperf": o.get("dlperf"),
                "reliability": o.get("reliability"),
            })
        return json.dumps({"offer_count": len(offers), "top": top}, default=str)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR listing offers: {exc}"


# ---------------------------------------------------------------------------
# Dispatch + schemas
# ---------------------------------------------------------------------------

_TOOL_FUNCS: dict[str, Callable[..., str]] = {
    "read_remote_log": read_remote_log,
    "grep_remote_log": grep_remote_log,
    "read_job_log": read_job_log,
    "read_pulse_history": read_pulse_history,
    "get_instance_details": get_instance_details,
    "check_remote_disk": check_remote_disk,
    "get_gpu_status": get_gpu_status,
    "query_wandb_run": query_wandb_run,
    "list_provider_offers": list_provider_offers,
}


def make_dispatch(ctx: ToolContext) -> Callable[[str, dict[str, Any]], str]:
    """Return a ``dispatch(name, args)`` bound to *ctx*, error-isolating each tool."""
    def dispatch(name: str, args: dict[str, Any]) -> str:
        fn = _TOOL_FUNCS.get(name)
        if fn is None:
            return f"ERROR: unknown tool '{name}'."
        try:
            return fn(ctx, **(args or {}))
        except TypeError as exc:
            return f"ERROR: bad arguments for {name}: {exc}"
        except Exception as exc:  # noqa: BLE001
            log.debug(f"agent tool {name} failed: {exc}")
            return f"ERROR running {name}: {exc}"
    return dispatch


def _fn_schema(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


def build_tool_schemas(agent_cfg) -> list[dict[str, Any]]:
    """Build the OpenAI function-schema list, honoring config toggles."""
    schemas: list[dict[str, Any]] = []

    if agent_cfg.read_logs:
        schemas.append(_fn_schema(
            "read_remote_log",
            "Read the tail of the remote job's stdout/stderr log.",
            {"tail_lines": {"type": "integer", "description": "Number of trailing lines (default 200)."}},
        ))
        schemas.append(_fn_schema(
            "grep_remote_log",
            "Search the remote job log for a regex (case-insensitive). Use for tracebacks, "
            "'CUDA out of memory', 'no space left', 'nan'/'inf', import errors.",
            {
                "pattern": {"type": "string", "description": "Extended-regex pattern to search for."},
                "context": {"type": "integer", "description": "Lines of context around each match (default 3)."},
                "max_matches": {"type": "integer", "description": "Max matches (default 40)."},
            },
            required=["pattern"],
        ))

    if agent_cfg.read_job_log:
        schemas.append(_fn_schema(
            "read_job_log",
            "Read the unified per-job log: the full timeline of this job — every `beam run` "
            "provisioning step and every prior `beam pulse` check (incl. restarts) since the start. "
            "Use it for the broader story behind the current failure.",
            {"tail_lines": {"type": "integer", "description": "Number of trailing lines (default 300)."}},
        ))

    schemas.append(_fn_schema(
        "read_pulse_history",
        "Read recent pulse recovery history for this session (actions, root_causes, restart counts). "
        "Essential for detecting a crash-loop: repeated restarts for the same root_cause.",
        {"limit": {"type": "integer", "description": "Number of recent records (default 50)."}},
    ))

    schemas.append(_fn_schema(
        "get_instance_details",
        "Get provider/instance details: whether it is interruptible (spot/bid), price, "
        "live provider status, and whether it was outbid/preempted.",
        {},
    ))

    if agent_cfg.disk_check:
        schemas.append(_fn_schema(
            "check_remote_disk",
            "Check remote disk usage (df -h) — detects 'no space left on device' failures.",
            {"path": {"type": "string", "description": "Path to check (default '/')."}},
        ))

    schemas.append(_fn_schema(
        "get_gpu_status",
        "Get peak GPU utilization and whether the GPU is idle.",
        {},
    ))

    if agent_cfg.use_wandb:
        schemas.append(_fn_schema(
            "query_wandb_run",
            "Query the training run's wandb metrics (run state + recent metric values, and whether "
            "the metric diverged to NaN/Inf). Use to tell a config/pipeline bug from a transient failure.",
            {
                "metric": {"type": "string", "description": "Metric key (default 'loss')."},
                "last_n": {"type": "integer", "description": "Number of recent points (default 20)."},
            },
        ))

    if agent_cfg.allow_server_switch:
        schemas.append(_fn_schema(
            "list_provider_offers",
            "List currently available alternative server offers matching this job's spec "
            "(vast.ai). Use to confirm a better/cheaper machine exists before destroy_restart.",
            {"max_results": {"type": "integer", "description": "Max offers (default 5)."}},
        ))

    # Terminal decision tool — always present. Handled by the LLM loop, not dispatch.
    schemas.append(_fn_schema(
        "submit_decision",
        "Record your final recovery decision. Call this exactly once when done investigating.",
        {
            "decision": {"type": "string", "enum": DECISIONS, "description": "The recovery action to take."},
            "root_cause": {"type": "string", "description": "Short tag, e.g. spot_preempted, cuda_oom, nan_loss, disk_full, import_error, crash_loop, network_blip, completed."},
            "reason": {"type": "string", "description": "One-paragraph human-readable explanation grounded in the evidence you gathered."},
            "confidence": {"type": "number", "description": "Confidence in [0,1]."},
            "project_doc_update": {"type": "string", "description": "OPTIONAL. Markdown for the agent-maintained section of the project doc (BEAM_AGENT.md). Provide ONLY if this check taught you something durable and generalizable about the project; it REPLACES that section, so keep it tight and cumulative. Omit otherwise."},
        },
        required=["decision", "root_cause", "reason", "confidence"],
    ))

    return schemas
