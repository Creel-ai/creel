"""creel init — interactive wizard and scaffolding for ~/.creel/."""

from __future__ import annotations

import getpass
import importlib.resources
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Literal

import pyrage
import yaml
from pydantic import BaseModel

from creel import paths

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class InitTelegramConfig(BaseModel):
    bot_token: str
    allowed_senders: list[str]


class InitLLMConfig(BaseModel):
    provider: Literal["anthropic", "openai", "google", "ollama"]
    model: str
    max_tokens: int = 4096
    api_key: str | None = None  # transient — never written to YAML
    ollama_url: str | None = None


class InitChannelConfig(BaseModel):
    type: Literal["telegram", "imessage", "whatsapp", "none"] = "none"
    telegram: InitTelegramConfig | None = None


class InitGuardianConfig(BaseModel):
    policy: bool = True
    audit: bool = True


class InitConfig(BaseModel):
    llm: InitLLMConfig
    channel: InitChannelConfig = InitChannelConfig()
    tools: list[str] = []
    enable_media: bool = False
    enable_guardian: bool = True
    guardian: InitGuardianConfig = InitGuardianConfig()


# ---------------------------------------------------------------------------
# Prompt helpers (interactive mode)
# ---------------------------------------------------------------------------

ProviderType = Literal["anthropic", "openai", "google", "ollama"]
ChannelType = Literal["telegram", "imessage", "whatsapp", "none"]

_PROVIDERS: tuple[ProviderType, ...] = ("anthropic", "openai", "google", "ollama")
_CHANNELS: tuple[ChannelType, ...] = ("telegram", "imessage", "whatsapp", "none")

_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "google": "gemini-2.0-flash",
    "ollama": "llama3",
}


def _prompt_choice(prompt: str, options: list[str], default: int = 0) -> int:
    """Display numbered options and return the selected index."""
    for i, opt in enumerate(options):
        marker = " (default)" if i == default else ""
        print(f"  [{i + 1}] {opt}{marker}")
    while True:
        raw = input(f"{prompt} [{default + 1}]: ").strip()
        if not raw:
            return default
        try:
            choice = int(raw) - 1
            if 0 <= choice < len(options):
                return choice
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {len(options)}.")


def _prompt_string(prompt: str, default: str = "", *, secret: bool = False) -> str:
    """Prompt for a string value. Uses getpass when *secret* is True."""
    suffix = f" [{default}]" if default and not secret else ""
    full_prompt = f"{prompt}{suffix}: "
    if secret:
        value = getpass.getpass(full_prompt)
    else:
        value = input(full_prompt)
    return value.strip() or default


def _prompt_yes_no(prompt: str, default: bool = True) -> bool:
    """Prompt for a yes/no answer."""
    hint = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{hint}]: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def _prompt_multi_select(prompt: str, options: list[str], ids: list[str]) -> list[str]:
    """Display numbered options and let the user select multiple (comma-separated).

    Returns the list of selected *ids*.
    """
    for i, opt in enumerate(options):
        print(f"  [{i + 1}] {opt}")
    print()
    raw = input(f"{prompt} (comma-separated, or 'all' / 'none') [all]: ").strip().lower()
    if not raw or raw == "all":
        return list(ids)
    if raw == "none":
        return []
    selected: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        try:
            idx = int(part) - 1
            if 0 <= idx < len(ids):
                selected.append(ids[idx])
        except ValueError:
            pass
    return selected


# ---------------------------------------------------------------------------
# Tool catalog (loaded from bundled YAML)
# ---------------------------------------------------------------------------

_catalog_cache: dict[str, Any] | None = None


def _load_catalog() -> dict[str, Any]:
    """Load the tool catalog from the bundled YAML template.

    Returns a dict mapping group IDs to dicts with ``label`` and ``tools`` keys.
    """
    global _catalog_cache  # noqa: PLW0603
    if _catalog_cache is not None:
        return _catalog_cache

    catalog_file = importlib.resources.files("creel") / "templates" / "tool_catalog.yaml"
    _catalog_cache = yaml.safe_load(catalog_file.read_text(encoding="utf-8"))
    return _catalog_cache


# ---------------------------------------------------------------------------
# Test message helpers
# ---------------------------------------------------------------------------


def _send_test_message(channel_type: str, config: InitChannelConfig) -> bool:
    """Send a test message through the configured channel.

    Returns True if successful, False otherwise.
    """
    import httpx

    if channel_type == "telegram" and config.telegram and config.telegram.allowed_senders:
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{config.telegram.bot_token}/sendMessage",
                json={
                    "chat_id": config.telegram.allowed_senders[0],
                    "text": "Creel setup complete! This is a test message.",
                },
                timeout=15.0,
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
    return False


# ---------------------------------------------------------------------------
# Wizard flow
# ---------------------------------------------------------------------------


def _run_wizard(existing: InitConfig | None = None) -> InitConfig:
    """Walk the user through an interactive configuration flow."""
    import creel.validation as _val

    print()
    print("Welcome to Creel setup!")
    print("=" * 40)

    # --- Step 1: LLM provider ---
    print()
    print("Step 1/4: LLM Provider")
    print("-" * 30)
    provider_labels = [
        "Anthropic (Claude)",
        "OpenAI (GPT)",
        "Google (Gemini)",
        "Ollama (local)",
    ]
    existing_idx = _PROVIDERS.index(existing.llm.provider) if existing else 0
    idx = _prompt_choice("Select provider", provider_labels, default=existing_idx)
    provider = _PROVIDERS[idx]

    # --- API key / connection ---
    api_key: str | None = None
    ollama_url: str | None = None

    if provider in ("anthropic", "openai", "google"):
        print()
        validators = {
            "anthropic": _val.validate_anthropic_key,
            "openai": _val.validate_openai_key,
            "google": _val.validate_google_key,
        }
        validator = validators[provider]
        label = {"anthropic": "Anthropic", "openai": "OpenAI", "google": "Google AI"}[provider]
        for attempt in range(3):
            api_key = _prompt_string(f"{label} API key", secret=True)
            if not api_key:
                print("  API key is required.")
                continue
            print(f"  Validating {label} key...", end=" ", flush=True)
            result = validator(api_key)
            if result.ok:
                print(result.message)
                break
            print(result.message)
            if attempt < 2:
                print("  Try again.")
        else:
            if not _prompt_yes_no("  Validation failed. Use this key anyway?", default=False):
                api_key = None
                print("  Skipped — no API key will be saved.")
    elif provider == "ollama":
        print()
        default_url = (
            existing.llm.ollama_url
            if existing and existing.llm.ollama_url
            else "http://localhost:11434"
        )
        ollama_url = _prompt_string("Ollama URL", default=default_url)
        print(f"  Checking Ollama at {ollama_url}...", end=" ", flush=True)
        result = _val.validate_ollama_reachable(ollama_url)
        if result.ok:
            print(result.message)
            if result.detail and result.detail.get("models"):
                print(f"  Available models: {', '.join(result.detail['models'])}")
        else:
            print(result.message)
            print("  Continuing anyway — make sure Ollama is running before using Creel.")

    # --- Model ---
    print()
    default_model = existing.llm.model if existing else _DEFAULT_MODELS.get(provider, "")
    model = _prompt_string("Model name", default=default_model)

    llm_config = InitLLMConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        ollama_url=ollama_url,
    )

    # --- Step 2: Tools ---
    print()
    print("Step 2/4: Tools")
    print("-" * 30)
    catalog = _load_catalog()
    tool_ids = list(catalog.keys())
    tool_labels = [catalog[tid]["label"] for tid in tool_ids]
    selected_tools = _prompt_multi_select(
        "Select tools to enable",
        tool_labels,
        tool_ids,
    )
    print(f"  Selected: {', '.join(selected_tools) if selected_tools else 'none'}")

    # --- Step 3: Channel ---
    print()
    print("Step 3/4: Channel (how you'll talk to Creel)")
    print("-" * 30)
    _CHANNEL_MENU: list[tuple[str, ChannelType]] = [
        ("Terminal (CLI only)", "none"),
        ("Telegram bot", "telegram"),
        ("iMessage (macOS only)", "imessage"),
        ("WhatsApp", "whatsapp"),
    ]
    channel_labels = [label for label, _ in _CHANNEL_MENU]
    ch_label_idx = _prompt_choice("Select channel", channel_labels, default=0)
    channel_type = _CHANNEL_MENU[ch_label_idx][1]

    telegram_cfg: InitTelegramConfig | None = None
    if channel_type == "telegram":
        print()
        bot_token = ""
        for attempt in range(3):
            bot_token = _prompt_string("Telegram bot token", secret=True)
            if not bot_token:
                print("  Bot token is required.")
                continue
            print("  Validating bot token...", end=" ", flush=True)
            result = _val.validate_telegram_token(bot_token)
            if result.ok:
                print(result.message)
                break
            print(result.message)
            if attempt < 2:
                print("  Try again.")
        else:
            if not _prompt_yes_no("  Validation failed. Use this token anyway?", default=False):
                bot_token = ""
                print("  Skipped — no bot token will be saved.")

        allowed_senders: list[str] = []
        while not allowed_senders:
            senders_raw = _prompt_string(
                "Allowed sender usernames (comma-separated)",
                default=existing.channel.telegram.allowed_senders[0]
                if existing and existing.channel.telegram
                else "",
            )
            allowed_senders = [s.strip() for s in senders_raw.split(",") if s.strip()]
            if not allowed_senders:
                print("  At least one allowed sender is required.")

        telegram_cfg = InitTelegramConfig(bot_token=bot_token, allowed_senders=allowed_senders)

        # Offer test message
        if bot_token and _prompt_yes_no("Send a test message?", default=False):
            ch_cfg = InitChannelConfig(type="telegram", telegram=telegram_cfg)
            print("  Sending test message...", end=" ", flush=True)
            if _send_test_message("telegram", ch_cfg):
                print("sent!")
            else:
                print("failed (check bot token and allowed sender).")

    channel_config = InitChannelConfig(
        type=channel_type,
        telegram=telegram_cfg,
    )

    # --- Step 4: Security ---
    print()
    print("Step 4/4: Security")
    print("-" * 30)
    enable_guardian = _prompt_yes_no("Enable Guardian security pipeline?", default=True)
    guardian_cfg = InitGuardianConfig()
    if enable_guardian:
        guardian_cfg.policy = _prompt_yes_no("  Enable policy engine (tool access control)?")
        guardian_cfg.audit = _prompt_yes_no("  Enable audit logging?")

    # --- Feature toggles ---
    print()
    enable_media = _prompt_yes_no("Enable media processing (images, voice)?", default=False)

    config = InitConfig(
        llm=llm_config,
        channel=channel_config,
        tools=selected_tools,
        enable_media=enable_media,
        enable_guardian=enable_guardian,
        guardian=guardian_cfg,
    )

    print()
    print("Configuration complete!")
    return config


# ---------------------------------------------------------------------------
# Age keypair management
# ---------------------------------------------------------------------------


def _ensure_age_keypair() -> tuple[Path, Path]:
    """Ensure an age keypair exists at ``~/.age/``.

    Returns ``(key_file, pub_file)``.  Creates both if missing.
    """
    age_dir = Path.home() / ".age"
    key_file = age_dir / "key.txt"
    pub_file = age_dir / "key.pub"

    if key_file.exists() and pub_file.exists():
        return key_file, pub_file

    age_dir.mkdir(parents=True, exist_ok=True)

    identity = pyrage.x25519.Identity.generate()
    recipient = identity.to_public()

    key_file.write_text(f"# created by creel init\n# public key: {recipient!s}\n{identity!s}\n")
    key_file.chmod(0o600)

    pub_file.write_text(f"{recipient!s}\n")

    return key_file, pub_file


# ---------------------------------------------------------------------------
# Secret encryption
# ---------------------------------------------------------------------------

_API_KEY_ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def _encrypt_secrets(config: InitConfig) -> dict[str, str]:
    """Encrypt API keys / tokens into age-encrypted ``.enc`` files.

    Returns a mapping of ``{label: relative_secret_path}`` for embedding
    in the generated ``agent.yaml``.  Plaintext never touches disk.
    """
    _key_file, pub_file = _ensure_age_keypair()
    pub_text = pub_file.read_text().strip()
    recipient = pyrage.x25519.Recipient.from_str(pub_text)

    secrets_dir = paths.secrets_dir()
    secrets_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}

    # LLM API key
    if config.llm.api_key:
        env_var = _API_KEY_ENV_VARS.get(config.llm.provider, "API_KEY")
        plaintext = f"{env_var}={config.llm.api_key}\n"
        enc_name = f"{config.llm.provider}.env.enc"
        ciphertext = pyrage.encrypt(plaintext.encode(), [recipient])
        (secrets_dir / enc_name).write_bytes(ciphertext)
        result["llm"] = f"secrets/{enc_name}"

    # Telegram bot token
    if config.channel.telegram:
        plaintext = f"TELEGRAM_BOT_TOKEN={config.channel.telegram.bot_token}\n"
        enc_name = "telegram.env.enc"
        ciphertext = pyrage.encrypt(plaintext.encode(), [recipient])
        (secrets_dir / enc_name).write_bytes(ciphertext)
        result["telegram"] = f"secrets/{enc_name}"

    return result


# ---------------------------------------------------------------------------
# YAML generation
# ---------------------------------------------------------------------------


def _build_tools_section(selected_tools: list[str]) -> dict[str, Any]:
    """Build the ``tools`` dict for agent.yaml from selected tool IDs."""
    catalog = _load_catalog()
    tools: dict[str, Any] = {}
    for tool_id in selected_tools:
        if tool_id in catalog:
            tools.update(catalog[tool_id]["tools"])
    return tools


def _generate_agent_yaml(config: InitConfig, secret_paths: dict[str, str]) -> str:
    """Build ``agent.yaml`` content from the wizard configuration.

    Only includes sections the user actually configured — no commented-out
    stubs or stale references.
    """
    doc: dict[str, Any] = {}

    # System prompt
    doc["system_prompt"] = (
        "You are a personal assistant running on Creel. Be concise and helpful.\nToday is {date}.\n"
    )

    # Tools
    tools = _build_tools_section(config.tools)
    doc["tools"] = tools

    # LLM
    llm: dict[str, Any] = {
        "model": config.llm.model,
        "max_tokens": config.llm.max_tokens,
    }
    if "llm" in secret_paths:
        llm["secrets"] = secret_paths["llm"]
    doc["llm"] = llm

    # Agent
    doc["agent"] = {"max_turns": 15}

    # Session
    doc["session"] = {
        "sessions_dir": "sessions",
        "summarize_on_trim": True,
    }

    # Workspace
    doc["workspace"] = {
        "path": "workspace",
        "timezone": "UTC",
    }

    # Channel — only include if configured
    if config.channel.type == "telegram" and config.channel.telegram:
        telegram_section: dict[str, Any] = {
            "bot_token": "$TELEGRAM_BOT_TOKEN",
            "allowed_senders": config.channel.telegram.allowed_senders,
        }
        if "telegram" in secret_paths:
            telegram_section["secrets"] = secret_paths["telegram"]
        doc["channels"] = {"telegram": telegram_section}
    elif config.channel.type == "imessage":
        doc["channels"] = {
            "imessage": {
                "listen_to": "$IMESSAGE_LISTEN_TO",
            },
        }
    elif config.channel.type == "whatsapp":
        doc["channels"] = {
            "whatsapp": {
                "phone_number": "$WHATSAPP_PHONE_NUMBER",
                "api_url": "$WHATSAPP_API_URL",
            },
        }

    # Media
    if config.enable_media:
        doc["media"] = {"enabled": True}

    # Guardian
    if config.enable_guardian:
        doc["guardian"] = {
            "enabled": True,
            "fast_classifier": {"enabled": False},
            "llm_judge": {"enabled": False},
            "policy": {"enabled": config.guardian.policy},
            "audit": {"enabled": config.guardian.audit},
        }

    output = yaml.dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True)

    # If no tools were selected, insert an example comment after the empty tools dict
    if not tools:
        example = (
            "# Example tool (requires the weather executor):\n"
            "#   check_weather:\n"
            "#     executor: weather\n"
            "#     network: true\n"
            "#     description: Get current weather and forecast\n"
            "#     parameters:\n"
            "#       location:\n"
            "#         type: string\n"
            "#         description: City name or coordinates\n"
            "#         required: true\n"
        )
        output = output.replace("tools: {}\n", f"tools: {{}}\n{example}", 1)

    return output


# ---------------------------------------------------------------------------
# Template copy (static scaffolding, backward compat)
# ---------------------------------------------------------------------------


def _copy_template(template_name: str, dest: Path, *, force: bool = False) -> bool:
    """Copy a bundled template file to *dest*.

    Returns True if the file was written, False if skipped (already exists
    and *force* is False).
    """
    if dest.exists() and not force:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)

    templates = importlib.resources.files("creel") / "templates"
    source = templates / template_name
    dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def _scaffold_dirs() -> list[str]:
    """Create the standard directory structure under ``creel_home()``.

    Returns human-readable status lines.
    """
    lines: list[str] = []
    for dir_fn in (
        paths.policies_dir,
        paths.secrets_dir,
        paths.sessions_dir,
        paths.workspace_dir,
        paths.tasks_dir,
        paths.cron_dir,
    ):
        d = dir_fn()
        already_existed = d.exists()
        d.mkdir(parents=True, exist_ok=True)
        lines.append(f"  {'exists' if already_existed else 'created'}: {d}")
    return lines


def _scaffold_static(*, force: bool = False) -> list[str]:
    """Create directories and copy static template files (old init behavior)."""
    lines = _scaffold_dirs()

    wrote = _copy_template("agent.yaml", paths.agent_config(), force=force)
    lines.append(
        f"  {'wrote' if wrote else 'exists (use --force to overwrite)'}: {paths.agent_config()}"
    )

    wrote = _copy_template(
        "policies/default.yaml",
        paths.policies_dir() / "default.yaml",
        force=force,
    )
    lines.append(
        f"  {'wrote' if wrote else 'exists (use --force to overwrite)'}: "
        f"{paths.policies_dir() / 'default.yaml'}"
    )

    return lines


# ---------------------------------------------------------------------------
# Docker image auto-pull
# ---------------------------------------------------------------------------


def _auto_pull_images(agent_config_path: Path) -> list[str]:
    """Pull pre-built Docker images referenced by the agent config.

    Returns status lines.  Silently returns empty list if Docker is
    unavailable or no remote images are configured.
    """
    try:
        from creel.containers import pull_required_images
        from creel.models import load_agent_config

        agent_def = load_agent_config(agent_config_path)
        return pull_required_images(agent_def)
    except FileNotFoundError:
        return []
    except (ImportError, RuntimeError, OSError) as exc:
        logger.debug("Auto-pull skipped: %s", exc)
        return [f"  skipped image pull ({exc})"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init(
    *,
    force: bool = False,
    interactive: bool = True,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    channel: str | None = None,
    bot_token: str | None = None,
    allowed_senders: str | None = None,
    tools: list[str] | None = None,
    enable_media: bool = False,
    enable_guardian: bool = True,
) -> list[str]:
    """Create the ~/.creel/ directory structure, optionally running the wizard.

    Returns a list of human-readable status lines.

    Modes:
    - TTY + ``interactive=True``  → run the interactive wizard
    - ``interactive=False``       → construct config from kwargs
    - Non-TTY + ``interactive=True`` → fall back to static template copy
    """
    # Check if already initialized
    if paths.is_initialized() and not force:
        if interactive and sys.stdin.isatty():
            if not _prompt_yes_no("Creel is already initialized. Reconfigure?", default=False):
                return ["  Already initialized. Use --force to overwrite."]

    # Determine mode
    use_wizard = interactive and sys.stdin.isatty()
    use_noninteractive = not interactive and provider is not None

    if not use_wizard and not use_noninteractive:
        # Fall back to static template copy (non-TTY or no provider specified)
        return _scaffold_static(force=force)

    # Build InitConfig — either from wizard or from kwargs
    if use_wizard:
        config = _run_wizard()
    else:
        # Non-interactive: build from CLI args
        resolved_model = model or _DEFAULT_MODELS.get(provider, "claude-sonnet-4-6")  # type: ignore[arg-type]
        parsed_senders = (
            [s.strip() for s in allowed_senders.split(",") if s.strip()] if allowed_senders else []
        )

        telegram_cfg = None
        if channel == "telegram" and bot_token:
            telegram_cfg = InitTelegramConfig(
                bot_token=bot_token,
                allowed_senders=parsed_senders,
            )

        config = InitConfig(
            llm=InitLLMConfig(
                provider=provider,  # type: ignore[arg-type]
                model=resolved_model,
                api_key=api_key,
                ollama_url=None,
            ),
            channel=InitChannelConfig(
                type=channel or "none",  # type: ignore[arg-type]
                telegram=telegram_cfg,
            ),
            tools=tools or [],
            enable_media=enable_media,
            enable_guardian=enable_guardian,
        )

    # Scaffold directories
    lines = _scaffold_dirs()

    # Copy default policy template
    wrote = _copy_template(
        "policies/default.yaml",
        paths.policies_dir() / "default.yaml",
        force=force,
    )
    lines.append(
        f"  {'wrote' if wrote else 'exists (use --force to overwrite)'}: "
        f"{paths.policies_dir() / 'default.yaml'}"
    )

    # Encrypt secrets
    secret_paths: dict[str, str] = {}
    if config.llm.api_key or (config.channel.telegram and config.channel.telegram.bot_token):
        try:
            secret_paths = _encrypt_secrets(config)
            for label, path in secret_paths.items():
                lines.append(f"  encrypted: {label} -> {path}")
        except Exception as exc:
            logger.warning("Failed to encrypt secrets: %s", exc)
            lines.append(f"  warning: could not encrypt secrets ({exc})")

    # Generate agent.yaml
    yaml_content = _generate_agent_yaml(config, secret_paths)
    agent_path = paths.agent_config()
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    agent_path.write_text(yaml_content, encoding="utf-8")
    lines.append(f"  wrote: {agent_path}")

    # Tool summary
    if config.tools:
        lines.append(f"  tools: {', '.join(config.tools)}")

    # Pull pre-built images if any tools use remote images
    pull_lines = _auto_pull_images(paths.agent_config())
    if pull_lines:
        lines.append("")
        lines.append("Docker images:")
        lines.extend(pull_lines)

    # Next steps
    lines.append("")
    if config.channel.type == "none":
        lines.append("Next steps:")
        lines.append("  1. Start a conversation: creel send 'Hello!'")
        lines.append("  2. Or start the daemon: creel daemon start")
    elif config.channel.type == "telegram":
        lines.append("Next steps:")
        lines.append("  1. Start the daemon: creel daemon start")
        lines.append("  2. Send a message to your Telegram bot")
    elif config.channel.type == "imessage":
        lines.append("Next steps:")
        lines.append(f"  1. Set IMESSAGE_LISTEN_TO in {paths.agent_config()}")
        lines.append("  2. Start the daemon: creel daemon start")
    elif config.channel.type == "whatsapp":
        lines.append("Next steps:")
        lines.append(f"  1. Set WHATSAPP_API_URL in {paths.agent_config()}")
        lines.append("  2. Start the daemon: creel daemon start")

    return lines


def migrate(repo_root: Path, *, force: bool = False) -> list[str]:
    """Copy existing repo-based config into ~/.creel/.

    Copies agent.yaml, policies/, secrets/, workspace/, and sessions/
    from *repo_root* into the creel home directory.

    Returns a list of human-readable status lines.
    """
    # Use static scaffold to ensure directories exist (skip wizard)
    lines = _scaffold_static(force=force)
    lines.append("")
    lines.append("Migration:")

    copies: list[tuple[Path, Path]] = []

    # agent.yaml
    src = repo_root / "agent.yaml"
    if src.exists():
        copies.append((src, paths.agent_config()))

    # Directory trees to copy
    dir_mappings = [
        ("policies", paths.policies_dir()),
        ("secrets", paths.secrets_dir()),
        ("workspace", paths.workspace_dir()),
        ("sessions", paths.sessions_dir()),
        ("tasks", paths.tasks_dir()),
    ]

    for dirname, dest_dir in dir_mappings:
        src_dir = repo_root / dirname
        if not src_dir.is_dir():
            continue
        for src_file in src_dir.rglob("*"):
            if src_file.is_file():
                rel = src_file.relative_to(src_dir)
                copies.append((src_file, dest_dir / rel))

    for src_file, dest_file in copies:
        if dest_file.exists() and not force:
            lines.append(f"  skipped (exists): {dest_file}")
            continue
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
        lines.append(f"  copied: {src_file} -> {dest_file}")

    if not copies:
        lines.append("  nothing to migrate (no config files found at repo root)")

    return lines
