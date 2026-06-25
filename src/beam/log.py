"""Rich-based logging with step banners, coloured output, and verbosity control.

Optionally mirrors all output to a per-job log file via ``attach_job_log`` — used
to build one unified timeline for a job spanning ``beam run`` and every subsequent
``beam pulse`` check (read by the pulse agent during diagnosis).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import IO

from rich.console import Console
from rich.theme import Theme

_theme = Theme(
    {
        "step": "bold yellow",
        "done": "bold green",
        "warn": "bold yellow",
        "err": "bold red",
        "info": "bold blue",
        "banner": "bold cyan",
        "dim": "dim",
    }
)

console = Console(theme=_theme, highlight=False)

_verbose = False
_quiet = False

# Per-job log file sink (set via attach_job_log). Mirrors all emitters to a file
# regardless of quiet/verbose so the file is a complete record.
_job_log_fh: IO[str] | None = None


def set_verbosity(*, verbose: bool = False, quiet: bool = False) -> None:
    global _verbose, _quiet
    _verbose = verbose
    _quiet = quiet


# ---------------------------------------------------------------------------
# Per-job log file sink
# ---------------------------------------------------------------------------

def attach_job_log(path: Path | str) -> None:
    """Mirror all subsequent log output to *path* (append) until detached."""
    global _job_log_fh
    detach_job_log()
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _job_log_fh = open(p, "a", encoding="utf-8")
    except OSError:
        _job_log_fh = None


def detach_job_log() -> None:
    """Stop mirroring to the job log file and close the handle."""
    global _job_log_fh
    if _job_log_fh is not None:
        try:
            _job_log_fh.close()
        except OSError:
            pass
        _job_log_fh = None


def _write_job_log(level: str, msg: str) -> None:
    if _job_log_fh is None:
        return
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _job_log_fh.write(f"[{ts}] {level:<5} {msg}\n")
        _job_log_fh.flush()
    except (OSError, ValueError):
        pass


def info(msg: str) -> None:
    _write_job_log("INFO", msg)
    if not _quiet:
        console.print(f"[info]{msg}[/info]")


def success(msg: str) -> None:
    _write_job_log("INFO", msg)
    if not _quiet:
        console.print(f"[done]{msg}[/done]")


def warn(msg: str) -> None:
    _write_job_log("WARN", msg)
    console.print(f"[warn]{msg}[/warn]")


def error(msg: str) -> None:
    _write_job_log("ERROR", msg)
    console.print(f"[err]Error:[/err] {msg}")


def step(msg: str) -> None:
    _write_job_log("STEP", msg)
    if not _quiet:
        console.print(f"[step]{msg}[/step]")


def done(msg: str) -> None:
    _write_job_log("DONE", msg)
    if not _quiet:
        console.print(f"[done]{msg}[/done]")


def debug(msg: str) -> None:
    _write_job_log("DEBUG", msg)
    if _verbose:
        console.print(f"[dim]{msg}[/dim]")


def banner(step_num: int, total: int, title: str) -> None:
    """Print a step banner matching the original run.sh style."""
    _write_job_log("STEP", f"=== Step {step_num}/{total} -- {title} ===")
    if _quiet:
        return
    console.print()
    console.print(f"[banner]{'=' * 49}[/banner]")
    console.print(f"[banner]  Step {step_num}/{total} -- {title}[/banner]")
    console.print(f"[banner]{'=' * 49}[/banner]")


def final_banner(msg: str = "All done!") -> None:
    _write_job_log("DONE", msg)
    if _quiet:
        return
    console.print()
    console.print(f"[done]{'=' * 50}[/done]")
    console.print(f"[done]  {msg}[/done]")
    console.print(f"[done]{'=' * 50}[/done]")


def kv(key: str, value: str) -> None:
    """Print a key-value pair."""
    _write_job_log("INFO", f"{key}: {value}")
    if not _quiet:
        console.print(f"  [info]{key}:[/info]  {value}")
