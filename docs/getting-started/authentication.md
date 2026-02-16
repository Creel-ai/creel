# Authentication

The runner supports two ways to authenticate with the Anthropic API:

| Method | Env var | How to get it |
|--------|---------|---------------|
| API key | `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com/) |
| Claude Code setup token | `ANTHROPIC_AUTH_TOKEN` | `claude setup-token` |

If both are set, `ANTHROPIC_AUTH_TOKEN` takes precedence.

## Using an API Key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
./runner.py run weather_check
```

## Using a Claude Code Setup Token

Claude Code can generate OAuth tokens that work with the Anthropic API:

```bash
# Generate a setup token (requires Claude Code CLI)
claude setup-token
# Copy the sk-ant-oat01-... value

export ANTHROPIC_AUTH_TOKEN=sk-ant-oat01-...
./runner.py run weather_check
```

## Storing Credentials in a Secrets File

Either variable can go in an age-encrypted secrets file:

```bash
# Create the plaintext .env
echo 'ANTHROPIC_AUTH_TOKEN=sk-ant-oat01-...' > secrets/anthropic.env

# Encrypt and delete plaintext
./scripts/encrypt-secret.sh secrets/anthropic.env
rm secrets/anthropic.env
```

Then reference it in your task YAML under `llm.secrets`. See [Secrets Management](../configuration/secrets.md) for details.

## Root `.env` File

The runner loads a root `.env` file (gitignored) at startup for non-secret configuration like phone numbers:

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
