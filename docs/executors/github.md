# GitHub

Manages GitHub repositories via the [`gh` CLI](https://cli.github.com/) — issues, PRs, CI runs, and code search.

## Authentication

The executor authenticates using a `GH_TOKEN` environment variable, which `gh` picks up natively. The token is stored in an age-encrypted secrets file and injected into the executor at runtime by the orchestrator. The executor never sees the encryption key or other credentials.

### Setup

Create a [fine-grained personal access token](https://github.com/settings/tokens?type=beta) with the minimum scopes your workflows need (e.g., `issues:read`, `pull_requests:read` for read-only use). Then encrypt it:

```bash
cat > secrets/github.env <<'EOF'
GH_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxx
EOF

./scripts/encrypt-secret.sh secrets/github.env
```

The script produces `secrets/github.env.enc` and deletes the plaintext file.

### Host Authentication

If you already have `gh` authenticated locally, you can skip the PAT setup and mount your host credentials into the container instead:

```yaml
github:
  executor: github
  network: true
  host_auth: true
  # no secrets needed
```

When `host_auth: true` is set, the orchestrator mounts `~/.config/gh` into the container read-only at `/home/executor/.config/gh`. The `gh` CLI inside the container picks up the existing auth state — no token creation or encryption required.

**Inline mode:** When running executors as subprocesses (development mode), `host_auth` has no effect — the executor inherits the host environment automatically.

**Trade-offs vs encrypted PATs:**

| | `host_auth` | `secrets` (PAT) |
|---|---|---|
| Setup | Just `gh auth login` | Create PAT, encrypt with age |
| Scope control | Whatever `gh auth login` granted | Fine-grained PAT scopes |
| Token rotation | Handled by `gh` | Manual re-encrypt |
| Isolation | Read-only mount of host config | Fully decoupled from host |

Use `host_auth` for personal/dev setups. Prefer encrypted PATs for shared or production deployments where you want explicit scope control.

### How the Token Reaches the Container

1. Orchestrator decrypts `secrets/github.env.enc` using the age key at `~/.age/key.txt`
2. `GH_TOKEN` is written to a temporary `--env-file` passed to `docker run`
3. The `gh` CLI reads `GH_TOKEN` from the environment — no `gh auth login` required
4. The temp file is deleted after the container exits

Unlike Google executors, no token minting or refresh-token swapping is needed. GitHub PATs are used directly.

## Configuration

```yaml
github:
  executor: github
  network: true
  secrets: secrets/github.env.enc
  args:
    command: "issue list --state open"
    repo: "owner/repo"               # optional, uses current context if omitted
```

## Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `command` | yes | `gh` subcommand to run (e.g., `issue list`, `pr view 42`, `run list`) |
| `repo` | no | Repository in `owner/repo` format |

## Allowed Commands

The executor enforces a built-in allowlist (see `src/executors/github/executor.py`).

### Always allowed (read-only)

`issue list`, `issue view`, `pr list`, `pr view`, `run list`, `run view`, `run watch`, `search code`, `search issues`, `search prs`

### Require review (write operations)

`issue create`, `issue comment`, `issue close`, `issue reopen`, `pr create`, `pr comment`, `pr merge`, `pr close`, `pr reopen`

These are also gated by Guardian policy rules in `policies/default.yaml`.

### Always blocked (destructive)

`repo delete`, `issue delete`, `pr merge --admin`, and `api` calls using `DELETE` or `PUT` methods.

## Policy

Read-only `github` commands are auto-allowed in `policies/default.yaml`. Write operations (`pr merge`, `issue create`, `pr create`, `issue close`, `pr close`, `issue comment`, `pr comment`) require human review. Destructive operations are hard-denied at both the policy and executor level.

## Network Access

This executor runs with `network: true` since it needs to reach the GitHub API. Most other executors run with `--network=none`.
