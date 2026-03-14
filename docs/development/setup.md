# Development Setup

Requires [pyenv](https://github.com/pyenv/pyenv) and [uv](https://github.com/astral-sh/uv).

## First-Time Setup

```bash
# Install Python 3.12 (.python-version pins this)
pyenv install 3.12.11

# Create virtual environment using pyenv's Python
uv venv
source .venv/bin/activate

# Install with dev dependencies
uv pip install -e ".[dev]"

# Optional: required for live ONNX export + classifier smoke tests
uv pip install -e ".[guardian]"
```

## Running Tests

```bash
pytest
```

Coverage is configured automatically via `pyproject.toml` (`--cov=creel --cov=guardian --cov=bridge --cov=executors --cov-report=term-missing`).

## Building Documentation

```bash
# Install docs dependencies
uv pip install -e ".[docs]"

# Serve locally with hot reload
mkdocs serve

# Build static site
mkdocs build
```

The documentation site will be available at `http://localhost:8000` when using `mkdocs serve`.
