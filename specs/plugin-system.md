# SPEC: Plugin System

## What It Does

Creel already has a plugin pattern for channels (entry points in `pyproject.toml`). This spec extends that pattern into a general plugin system so third-party developers can add:

- **New channels** (already works — formalize it)
- **New executors** (tools the agent can call)
- **New delivery targets** (where cron output goes)

The key insight: Creel's container isolation makes third-party executors **safe by default**. Each executor plugin runs in its own Docker container with only its own credentials. This is a major advantage over OpenClaw's skill system where everything runs in the same process.

## How It Works

```
┌──────────────────────────────────┐
│         Plugin Registry          │
│                                  │
│  Discovers plugins via:          │
│  • pip entry points              │
│  • ~/.creel/plugins/ directory   │
│  • creel plugin install <pkg>    │
└──────────┬───────────────────────┘
           │
     ┌─────┼──────────┐
     ▼     ▼          ▼
 Channels  Executors  Delivery
 (existing)(new)      (new)
```

### Executor plugins

An executor plugin provides:
1. A **tool definition** (name, description, parameters) — what the agent sees
2. A **Dockerfile** (or base image reference) — the container it runs in
3. A **run function** — takes args + secrets, returns result
4. A **secrets declaration** — which env vars it needs

When the agent calls the tool, Creel:
1. Builds/pulls the executor container (cached after first run)
2. Mounts only the declared secrets
3. Runs the executor with the tool args
4. Returns the result through the Guardian pipeline

### Installation

```
creel plugin install creel-plugin-spotify     # from PyPI
creel plugin install ./my-local-plugin        # from local directory
creel plugin install gh:user/creel-plugin-x   # from GitHub
creel plugin list                             # show installed plugins
creel plugin remove creel-plugin-spotify
```

### Plugin package structure

```
creel-plugin-spotify/
├── pyproject.toml          # entry point registration
├── creel_spotify/
│   ├── __init__.py
│   ├── executor.py         # SpotifyExecutor + register_plugin()
│   └── Dockerfile          # container definition
└── README.md
```

`pyproject.toml`:
```toml
[project.entry-points."creel.executors"]
spotify = "creel_spotify.executor:register_plugin"
```

### Plugin metadata

Each plugin declares:
```python
PluginMeta(
    id="spotify",
    label="Spotify",
    kind="executor",           # executor | channel | delivery
    version="1.0.0",
    secrets=["SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"],
    description="Control Spotify playback and search",
)
```

## Config Surface

### Enabling a plugin

After installation, add credentials to config:

```yaml
plugins:
  spotify:
    enabled: true
    secrets: secrets/spotify.env.enc
```

That's it. The plugin's tool automatically appears in the agent's tool list, and its container is built on first use.

### CLI commands

```
creel plugin list                           # installed plugins + status
creel plugin install <package>              # install from PyPI/local/GitHub
creel plugin remove <name>                  # uninstall
creel plugin info <name>                    # show metadata, secrets needed, health
creel plugin build <name>                   # pre-build the executor container
```

### Plugin registry (future)

A `creel plugin search <query>` command that searches a central registry. Not in scope for v1 — just PyPI and local installs.

## Acceptance Criteria

### Installation
- [ ] `creel plugin install creel-plugin-example` installs from PyPI and registers entry points
- [ ] `creel plugin install ./local-plugin` installs from local directory
- [ ] `creel plugin list` shows installed plugins with name, version, kind, status
- [ ] `creel plugin remove <name>` uninstalls cleanly

### Executor plugins
- [ ] Installed executor plugin appears as an available tool in the agent's tool list
- [ ] Agent calls the plugin tool → executor runs in its own container
- [ ] Plugin only receives its declared secrets (not other executors' secrets)
- [ ] Plugin result passes through Guardian pipeline (coherence, credential scanning)
- [ ] Container is cached after first build (fast subsequent runs)

### Channel plugins
- [ ] Existing channel plugin pattern (entry points) continues to work unchanged
- [ ] New channel plugins installed via `creel plugin install` are auto-discovered

### Security
- [ ] Plugin executor container has no network access by default (opt-in via `network: true` in metadata)
- [ ] Plugin container is read-only filesystem with size-limited tmpfs
- [ ] Plugin container has resource limits (memory, CPU) matching existing executor containers
- [ ] Malicious plugin that tries to access other secrets → blocked by container isolation

### Developer experience
- [ ] `creel plugin init <name>` scaffolds a new plugin project with template files
- [ ] Plugin README documents required secrets and setup steps
- [ ] `creel plugin info <name>` shows what secrets are needed and whether they're configured

### Edge cases
- [ ] Plugin installed but secrets not configured → tool hidden from agent, warning logged
- [ ] Plugin Dockerfile fails to build → clear error, other plugins unaffected
- [ ] Two plugins declare the same tool name → second one rejected with error
- [ ] Plugin installed but `enabled: false` in config → tool not loaded
- [ ] Daemon restart → all plugins rediscovered from entry points (no state lost)
