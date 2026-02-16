# Contributing to Creel

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

1. Clone the repo:
   ```bash
   git clone https://github.com/creel-ai/creel.git
   cd creel
   ```

2. Install dependencies (requires [uv](https://docs.astral.sh/uv/)):
   ```bash
   uv sync
   ```

3. Run the test suite:
   ```bash
   uv run pytest
   ```

## Making Changes

1. Fork the repo and create a feature branch from `main`
2. Make your changes
3. Add or update tests as needed
4. Run the full test suite to make sure nothing is broken
5. Open a pull request against `main`

## Code Style

- Follow existing patterns in the codebase
- Use type hints
- Keep functions focused and well-documented

## Reporting Issues

Open an issue on GitHub with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Your environment (OS, Python version)

## License

By contributing, you agree that your contributions will be licensed under the project's existing license.
