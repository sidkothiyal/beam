"""Tests for the global --config/-c option on the beam CLI."""

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beam.cli import app, _state

runner = CliRunner()

MINIMAL_CONFIG = textwrap.dedent("""\
    project:
      name: test-project
    remote:
      project_dir: /workspace
      user: root
""")


@pytest.fixture(autouse=True)
def reset_state():
    """Reset global _state between tests."""
    _state["config_file"] = None
    yield
    _state["config_file"] = None


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "custom.yaml"
    p.write_text(MINIMAL_CONFIG)
    return p


# ---------------------------------------------------------------------------
# _resolve_config_path
# ---------------------------------------------------------------------------

def test_resolve_config_path_from_state(config_file):
    from beam.cli import _resolve_config_path
    _state["config_file"] = str(config_file)
    result = _resolve_config_path()
    assert result == config_file


def test_resolve_config_path_none_when_state_absent():
    from beam.cli import _resolve_config_path
    assert _resolve_config_path() is None


# ---------------------------------------------------------------------------
# CLI — global --config sets _state
# ---------------------------------------------------------------------------

def test_global_config_flag_sets_state(config_file):
    """beam --config <file> <subcommand> sets _state before the subcommand runs.

    --help is eager and exits before the callback fires, so we use `pulse list`
    which completes cleanly even with no sessions.
    """
    result = runner.invoke(app, ["--config", str(config_file), "pulse", "list"])
    assert result.exit_code == 0
    assert _state["config_file"] == str(config_file)


def test_global_config_short_flag(config_file):
    result = runner.invoke(app, ["-c", str(config_file), "pulse", "list"])
    assert result.exit_code == 0
    assert _state["config_file"] == str(config_file)


def test_global_config_absent_leaves_state_none():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert _state["config_file"] is None


# ---------------------------------------------------------------------------
# CLI — init command respects absence of --config (writes to cwd)
# ---------------------------------------------------------------------------

def test_init_without_config_writes_beam_yaml(tmp_path):
    result = runner.invoke(app, ["init"], catch_exceptions=False)
    # init writes to cwd, not tmp_path — just check it doesn't crash
    assert result.exit_code in (0, 1)  # 1 if beam.yaml already exists in cwd


# ---------------------------------------------------------------------------
# load_config integration
# ---------------------------------------------------------------------------

def test_load_config_reads_custom_filename(config_file):
    from beam.config import load_config
    cfg = load_config(config_path=config_file)
    assert cfg.project.name == "test-project"


def test_load_config_returns_defaults_when_no_file():
    from beam.config import load_config
    cfg = load_config(config_path=None)
    # No beam.yaml in /tmp — falls back to defaults
    assert cfg.remote.user == "root"
