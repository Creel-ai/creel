# Creel Installation Spec

## Problem

Today Creel is installed by cloning the repo and running `pip install -e .` (or `uv pip install -e .`), which:

1. **Stale `.egg-info`** — editable installs leave `llm_taskrunner.egg-info/` that goes stale when entry points or packages change, causing broken imports or missing CLI commands.
2. **Must run from repo** — `creel daemon install` hardcodes `WorkingDirectory` to the repo root; the daemon resolves `agent.yaml`, `tools/`, `tasks/`, `secrets/` relative to cwd.
3. **No clean install-from-anywhere story** — can't `pipx install creel` from a release because runtime files (workspace, configs) are tangled with the source tree.

## Goals

- `pipx install creel` (or `uv tool install creel`) works from a published package or a git URL.
- Running `creel init` scaffolds `~/.creel/` with everything the daemon needs.
- The daemon runs against `~/.creel/` — no dependency on the source repo at runtime.
- Developers can still use editable installs (`uv pip install -e .`) without conflict.

## Design

### 1. `~/.creel/` as the canonical home

All runtime state lives under `CREEL_HOME` (default `~/.creel/`):

```
~/.creel/
├── agent.yaml          # agent config (identity, model, channels)
├── workspace/          # agent workspace (SOUL.md, MEMORY.md, etc.)
├── tools/              # tool definitions (YAML)
├── tasks/              # scheduled tasks (YAML)
├── policies/           # guardian policies
├── secrets/            # encrypted secrets
├── sessions/           # conversation history
├── daemon.sock         # runtime: unix socket
├── daemon.pid          # runtime: pid file
└── daemon.log          # runtime: log output
```

**Resolution order** (for every path the daemon needs):
1. Explicit CLI flag (`--config /path/to/agent.yaml`)
2. `CREEL_HOME` env var
3. `~/.creel/`

This means the daemon's launchd plist no longer needs `WorkingDirectory` pointed at the repo. It just uses `~/.creel/`.

### 2. `creel init`

Interactive-ish scaffolding command (not a full wizard — just the essentials):

```
$ creel init
Creating ~/.creel/ ...
  ✓ agent.yaml (default config)
  ✓ workspace/
  ✓ tools/
  ✓ tasks/
  ✓ policies/
  ✓ secrets/

Edit ~/.creel/agent.yaml to configure your agent.
Then run: creel daemon install
```

Behavior:
- Creates `~/.creel/` and subdirectories
- Copies **default templates** from package data (bundled in the wheel)
  - `agent.yaml` — minimal working config with placeholder values
  - `workspace/SOUL.md`, `workspace/AGENTS.md` — starter files
  - `policies/default.yaml` — default guardian policy
- **Never overwrites** existing files (prints "already exists, skipping")
- `--force` flag to overwrite everything
- `--home /custom/path` to use a non-default location

### 3. Package structure changes

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "creel"                      # rename from llm-taskrunner
version = "0.1.0"

[project.scripts]
creel = "creel.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/creel", "src/guardian", "src/bridge"]
```

**Key changes:**
- Rename `taskrunner` → `creel` (the package name should match the command)
- Bundle default templates as package data:
  ```toml
  [tool.hatch.build.targets.wheel.force-include]
  "defaults/" = "creel/defaults/"
  ```
- Templates live in `defaults/` in the repo, get bundled into the wheel under `creel/defaults/`

### 4. Path resolution in code

Add a single module `creel/paths.py`:

```python
from pathlib import Path
import os

def creel_home() -> Path:
    """Return the Creel home directory."""
    return Path(os.environ.get("CREEL_HOME", Path.home() / ".creel"))

def agent_config(override: str | None = None) -> Path:
    if override:
        return Path(override)
    return creel_home() / "agent.yaml"

def workspace_dir() -> Path:
    return creel_home() / "workspace"

def tools_dir() -> Path:
    return creel_home() / "tools"

# ... etc for tasks, policies, secrets, sessions
```

Every module that currently does `Path("agent.yaml")` or `Path(__file__).parents[2]` switches to calling these functions.

### 5. Installation methods

#### End user (recommended)
```bash
# Install
pipx install creel              # from PyPI (future)
pipx install git+https://github.com/creel-ai/creel.git  # from repo

# Setup
creel init
creel daemon install
```

#### Developer
```bash
git clone https://github.com/creel-ai/creel.git
cd creel
uv pip install -e ".[dev]"

# Still need init for runtime dirs
creel init

# Or point at repo workspace for dev:
export CREEL_HOME=./  # use repo root as home
```

#### One-liner (stretch goal)
```bash
curl -fsSL https://creel-ai.com/install.sh | bash
```
This would:
1. Install `pipx` if missing (via `brew` or `apt`)
2. `pipx install creel`
3. Run `creel init`
4. Print next steps

### 6. Daemon launchd plist (updated)

```xml
<plist>
  <dict>
    <key>Label</key>
    <string>com.creel.daemon</string>
    <key>ProgramArguments</key>
    <array>
      <string>/path/to/creel</string>  <!-- pipx-managed path -->
      <string>daemon</string>
      <string>run</string>
    </array>
    <!-- No WorkingDirectory needed — creel resolves from CREEL_HOME -->
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>~/.creel/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>~/.creel/daemon.log</string>
  </dict>
</plist>
```

The plist points at the `creel` binary wherever pipx put it. No `PYTHONPATH` hacks, no `WorkingDirectory`.

### 7. Migration from current setup

For existing installs (us):

```bash
creel init --migrate
```

This would:
1. Create `~/.creel/` structure
2. Copy/symlink existing files from the repo into `~/.creel/`:
   - `agent.yaml` → `~/.creel/agent.yaml`
   - `tools/` → `~/.creel/tools/`
   - `tasks/` → `~/.creel/tasks/`
   - `workspace/` → `~/.creel/workspace/` (already symlinked)
   - `secrets/` → `~/.creel/secrets/`
   - `policies/` → `~/.creel/policies/`
3. Reinstall the launchd service pointing at the new paths
4. Print what changed

## Migration plan (implementation order)

1. **Add `creel/paths.py`** — central path resolution with `CREEL_HOME`
2. **Update all imports** — replace hardcoded paths with `paths.*` calls
3. **Add `creel init`** — scaffold command with default templates
4. **Bundle templates** — move defaults into package data
5. **Rename package** — `taskrunner` → `creel`
6. **Update `daemon install`** — generate plist without `WorkingDirectory`/`PYTHONPATH`
7. **Add `--migrate`** — for existing users
8. **Test with `pipx install .`** — verify clean install from repo
9. **Publish** — PyPI when ready

## Open questions

- **Symlinks vs copies for migrate?** Symlinks are easier but break if repo moves. Copies are safer but diverge. Leaning copies + a note saying "your repo files are no longer used at runtime."
- **Config format changes?** Good time to version `agent.yaml` with a `version: 1` field if we want future migration support.
- **Linux support?** `daemon install` is macOS-only (launchd). Should `creel init` also generate a systemd unit on Linux? (Not urgent but worth noting.)
