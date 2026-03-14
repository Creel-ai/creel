# Authentication

Creel needs Anthropic API credentials to call Claude. Two methods are supported:

| Method | Env var | How to get it |
|--------|---------|---------------|
| API key | `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com/) |
| Claude Code setup token | `ANTHROPIC_AUTH_TOKEN` | `claude setup-token` |

If both are set, `ANTHROPIC_AUTH_TOKEN` takes precedence.

## Using an API Key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
creel run weather_check
```

## Using a Claude Code Setup Token

Claude Code can generate OAuth tokens that work with the Anthropic API:

```bash
# Generate a setup token (requires Claude Code CLI)
claude setup-token
# Copy the sk-ant-oat01-... value

export ANTHROPIC_AUTH_TOKEN=sk-ant-oat01-...
creel run weather_check
```

## Encrypting Credentials with age

For persistent, secure storage:

```bash
# One-time setup
brew install age
mkdir -p ~/.age
age-keygen -o ~/.age/key.txt 2> ~/.age/key.pub

# Encrypt your credentials
echo 'ANTHROPIC_API_KEY=sk-ant-...' > secrets/anthropic.env
creel encrypt secrets/anthropic.env
rm secrets/anthropic.env   # always delete the plaintext
```

Reference the encrypted file in your task YAML:

```yaml
llm:
  model: claude-sonnet-4-20250514
  max_tokens: 4096
  secrets: secrets/anthropic.env.enc
```

## Root `.env` File

Creel loads a root `.env` file (gitignored) at startup for non-secret configuration:

```bash
# .env (project root — gitignored, never committed)
PHONE=+1234567890
```

Values are available as environment variables and can be referenced in task YAMLs with `$VAR` syntax:

```yaml
output:
  type: imessage
  to: "$PHONE"
```

Real environment variables take precedence over `.env` values.
