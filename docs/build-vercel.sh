#!/usr/bin/env bash
set -euo pipefail

# Build outside docs_dir, then copy into a Vercel-served directory under docs/.
rm -rf .vercel-static ../.mkdocs-site .vercel-venv
python3 -m venv .vercel-venv
.vercel-venv/bin/pip install --disable-pip-version-check --no-cache-dir \
  "mkdocs>=1.6.0" \
  "mkdocs-material>=9.5.0"
# With -f ../mkdocs.yml, -d is resolved from repo root.
.vercel-venv/bin/mkdocs build -f ../mkdocs.yml -d .mkdocs-site
mkdir -p .vercel-static
cp -a ../.mkdocs-site/. .vercel-static/
