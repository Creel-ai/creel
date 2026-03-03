# Secrets Management

Secrets are encrypted at rest using [age](https://github.com/FiloSottile/age). The Python side uses [pyrage](https://pypi.org/project/pyrage/) for decryption.

## Automatic Setup via `creel init`

The easiest way to set up secrets is through the init wizard:

```bash
creel init
```

This automatically:

1. Creates an age keypair at `~/.age/key.txt` if one doesn't exist
2. Prompts for your API key (hidden input) and validates it
3. Encrypts the key into `~/.creel/secrets/<provider>.env.enc`
4. Wires the encrypted path into `agent.yaml`

You never need to handle plaintext key files manually.

## Manual Setup

If you prefer to manage secrets yourself:

```bash
# Generate an age key pair (one-time)
mkdir -p ~/.age
age-keygen -o ~/.age/key.txt 2> ~/.age/key.pub
```

### Encrypting Secrets

```bash
# Encrypt a .env file
./scripts/encrypt-secret.sh secrets/anthropic.env
```

This produces `secrets/anthropic.env.enc` and deletes the plaintext file.

## Key Location

The decryption key path defaults to `~/.age/key.txt`. Override with the `AGE_IDENTITY` environment variable:

```bash
export AGE_IDENTITY=/path/to/key.txt
```

## `.env` File Format

The `.env` format supports `KEY=value`, quoted values, and comments:

```
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_CREDENTIALS_JSON='{"refresh_token": "...", "client_id": "...", "client_secret": "..."}'
GH_TOKEN=github_pat_xxx
NOTION_API_KEY=secret_xxx
```

## How Secrets Are Used

1. Task YAML files reference encrypted secrets: `secrets: secrets/gcal.env.enc`
2. At runtime, the orchestrator decrypts the file using the age key
3. Decrypted values are injected as environment variables into the executor process
4. The LLM never sees the decrypted secrets — only executors receive them
