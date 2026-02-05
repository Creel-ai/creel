"""Tests for output routing."""

from __future__ import annotations

from pathlib import Path

from taskrunner.models import OutputConfig
from taskrunner.outputs import send_output


def test_stdout_output(capsys) -> None:
    config = OutputConfig(type="stdout", to="")
    send_output("Hello, world!", config)
    captured = capsys.readouterr()
    assert captured.out.strip() == "Hello, world!"


def test_file_output(tmp_path: Path) -> None:
    out_file = tmp_path / "output.txt"
    config = OutputConfig(type="file", to=str(out_file))
    send_output("Test output", config)
    assert out_file.read_text() == "Test output"


def test_file_output_creates_parent_dirs(tmp_path: Path) -> None:
    out_file = tmp_path / "sub" / "dir" / "output.txt"
    config = OutputConfig(type="file", to=str(out_file))
    send_output("Nested output", config)
    assert out_file.read_text() == "Nested output"
