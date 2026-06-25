"""Canonical per-project doc for the pulse agent.

A committed, human-editable markdown file (default ``BEAM_AGENT.md`` at the repo
root) that gives the agent durable project knowledge. Humans write prerequisites
freely; the agent maintains only its own marker-delimited section, so human
content is never clobbered. The whole doc is fed to the agent at the start of
every pulse call; after a successful pulse call the agent may propose an update
to its section.
"""

from __future__ import annotations

from pathlib import Path

from beam.config import BeamConfig

AGENT_NOTES_START = "<!-- BEAM AGENT NOTES (auto-maintained — do not edit below) -->"
AGENT_NOTES_END = "<!-- END BEAM AGENT NOTES -->"

_TEMPLATE = """\
# Beam Agent — Project Knowledge

Operator-authored guidance for the pulse recovery agent. Edit this section freely:
describe expected behavior, known failure signatures and the right response, the
wandb project, normal loss ranges, GPU/cost constraints, etc. The agent reads this
at the start of every pulse check and trusts it.

{start}
_(empty — the agent maintains durable learnings here after successful checks)_
{end}
"""


def doc_path(repo_root: Path, cfg: BeamConfig) -> Path | None:
    """Resolve the project doc path, or None if disabled."""
    rel = cfg.pulse.agent.project_doc
    if not rel:
        return None
    return repo_root / rel


def read_project_doc(repo_root: Path, cfg: BeamConfig) -> str:
    """Return the doc contents (truncated to project_doc_max_bytes), or ""."""
    p = doc_path(repo_root, cfg)
    if not p or not p.is_file():
        return ""
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return ""
    cap = cfg.pulse.agent.project_doc_max_bytes
    if len(text.encode("utf-8")) > cap:
        text = text.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
    return text


def update_agent_section(repo_root: Path, cfg: BeamConfig, notes: str) -> bool:
    """Write *notes* into the agent-managed section, preserving human content.

    Creates the doc from a template if it does not exist. If the markers are
    absent (e.g. a human-only doc), appends a fresh marker block at EOF. Returns
    True if the file was written.
    """
    p = doc_path(repo_root, cfg)
    if not p or not notes.strip():
        return False

    notes = notes.strip()
    cap = cfg.pulse.agent.project_doc_max_bytes
    if len(notes.encode("utf-8")) > cap:
        notes = notes.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
    block = f"{AGENT_NOTES_START}\n{notes}\n{AGENT_NOTES_END}"

    try:
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_TEMPLATE.format(start=AGENT_NOTES_START, end=AGENT_NOTES_END), encoding="utf-8")
        text = p.read_text(encoding="utf-8")

        if AGENT_NOTES_START in text and AGENT_NOTES_END in text:
            head, rest = text.split(AGENT_NOTES_START, 1)
            _, tail = rest.split(AGENT_NOTES_END, 1)
            new_text = f"{head}{block}{tail}"
        else:
            sep = "" if text.endswith("\n") else "\n"
            new_text = f"{text}{sep}\n{block}\n"

        p.write_text(new_text, encoding="utf-8")
        return True
    except OSError:
        return False
