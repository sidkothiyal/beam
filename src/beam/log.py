"""Rich-based logging with step banners, coloured output, and verbosity control."""

from __future__ import annotations

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


def set_verbosity(*, verbose: bool = False, quiet: bool = False) -> None:
    global _verbose, _quiet
    _verbose = verbose
    _quiet = quiet


def info(msg: str) -> None:
    if not _quiet:
        console.print(f"[info]{msg}[/info]")


def success(msg: str) -> None:
    if not _quiet:
        console.print(f"[done]{msg}[/done]")


def warn(msg: str) -> None:
    console.print(f"[warn]{msg}[/warn]")


def error(msg: str) -> None:
    console.print(f"[err]Error:[/err] {msg}")


def step(msg: str) -> None:
    if not _quiet:
        console.print(f"[step]{msg}[/step]")


def done(msg: str) -> None:
    if not _quiet:
        console.print(f"[done]{msg}[/done]")


def debug(msg: str) -> None:
    if _verbose:
        console.print(f"[dim]{msg}[/dim]")


def banner(step_num: int, total: int, title: str) -> None:
    """Print a step banner matching the original run.sh style."""
    if _quiet:
        return
    console.print()
    console.print(f"[banner]{'=' * 49}[/banner]")
    console.print(f"[banner]  Step {step_num}/{total} -- {title}[/banner]")
    console.print(f"[banner]{'=' * 49}[/banner]")


def final_banner(msg: str = "All done!") -> None:
    if _quiet:
        return
    console.print()
    console.print(f"[done]{'=' * 50}[/done]")
    console.print(f"[done]  {msg}[/done]")
    console.print(f"[done]{'=' * 50}[/done]")


def kv(key: str, value: str) -> None:
    """Print a key-value pair."""
    if not _quiet:
        console.print(f"  [info]{key}:[/info]  {value}")
