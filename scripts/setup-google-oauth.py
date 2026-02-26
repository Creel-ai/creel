#!/usr/bin/env python3
"""One-time Google OAuth2 setup for supported services.

This script performs the OAuth dance to obtain a refresh token for a
Google API. The resulting credentials are saved to a .env file that
can then be encrypted with age (automatically via --encrypt, or
manually with encrypt-secret.sh).

Supported services:
  gcal          — Google Calendar (read-only)
  gcal_write    — Google Calendar (write events)
  gmail         — Gmail (read-only)
  gmail_send    — Gmail (send emails)
  gmail_modify  — Gmail (modify/trash/delete)
  drive         — Google Drive (read-only)
  drive_write   — Google Drive (upload files)
  docs_read     — Google Docs (read-only)
  docs_write    — Google Docs (read/write)
  sheets_read   — Google Sheets (read-only)
  sheets_write  — Google Sheets (read/write)
  slides_read   — Google Slides (read-only)
  slides_write  — Google Slides (read/write)

Usage:
    # Single service
    python scripts/setup-google-oauth.py gmail

    # Multiple services
    python scripts/setup-google-oauth.py gmail gmail_send gmail_modify

    # All services, with automatic encryption
    python scripts/setup-google-oauth.py --all --encrypt

Requires: google-auth-oauthlib
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Error: google-auth-oauthlib not installed.")
    print("Install with: pip install google-auth-oauthlib")
    sys.exit(1)


SERVICES: dict[str, dict[str, str]] = {
    "gcal": {
        "scope": "https://www.googleapis.com/auth/calendar.readonly",
        "output": "secrets/gcal.env",
        "api_name": "Google Calendar (read-only)",
    },
    "gcal_write": {
        "scope": "https://www.googleapis.com/auth/calendar.events",
        "output": "secrets/gcal_write.env",
        "api_name": "Google Calendar (write)",
    },
    "gmail": {
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "output": "secrets/gmail.env",
        "api_name": "Gmail (read-only)",
    },
    "gmail_send": {
        "scope": "https://www.googleapis.com/auth/gmail.send",
        "output": "secrets/gmail_send.env",
        "api_name": "Gmail (send)",
    },
    "gmail_modify": {
        "scope": "https://www.googleapis.com/auth/gmail.modify",
        "output": "secrets/gmail_modify.env",
        "api_name": "Gmail (modify)",
    },
    "drive": {
        "scope": "https://www.googleapis.com/auth/drive.readonly",
        "output": "secrets/drive.env",
        "api_name": "Google Drive (read-only)",
    },
    "drive_write": {
        "scope": "https://www.googleapis.com/auth/drive.file",
        "output": "secrets/drive_write.env",
        "api_name": "Google Drive (write)",
    },
    "docs_read": {
        "scope": "https://www.googleapis.com/auth/documents.readonly",
        "output": "secrets/docs_read.env",
        "api_name": "Google Docs (read-only)",
    },
    "docs_write": {
        "scope": "https://www.googleapis.com/auth/documents",
        "output": "secrets/docs_write.env",
        "api_name": "Google Docs (read/write)",
    },
    "sheets_read": {
        "scope": "https://www.googleapis.com/auth/spreadsheets.readonly",
        "output": "secrets/sheets_read.env",
        "api_name": "Google Sheets (read-only)",
    },
    "sheets_write": {
        "scope": "https://www.googleapis.com/auth/spreadsheets",
        "output": "secrets/sheets_write.env",
        "api_name": "Google Sheets (read/write)",
    },
    "slides_read": {
        "scope": "https://www.googleapis.com/auth/presentations.readonly",
        "output": "secrets/slides_read.env",
        "api_name": "Google Slides (read-only)",
    },
    "slides_write": {
        "scope": "https://www.googleapis.com/auth/presentations",
        "output": "secrets/slides_write.env",
        "api_name": "Google Slides (read/write)",
    },
}

CLIENT_SECRET_FILE = "client_secret.json"


def encrypt_file(env_path: Path) -> bool:
    """Encrypt an .env file with age and delete the plaintext.

    Returns True if encryption succeeded, False otherwise.
    On failure the plaintext .env is left in place.
    """
    if not shutil.which("age"):
        print("  Warning: age is not installed, skipping encryption.")
        print("  Install from https://github.com/FiloSottile/age")
        return False

    recipient_file = Path(
        os.environ.get("AGE_RECIPIENT_FILE", Path.home() / ".age" / "key.pub")
    )
    if not recipient_file.exists():
        print(f"  Warning: recipient file not found ({recipient_file}), skipping encryption.")
        return False

    recipient_text = recipient_file.read_text()
    match = re.search(r"age1[a-z0-9]+", recipient_text)
    if not match:
        print(f"  Warning: no age recipient found in {recipient_file}, skipping encryption.")
        return False

    recipient = match.group(0)
    enc_path = Path(str(env_path) + ".enc")

    result = subprocess.run(
        ["age", "-e", "-r", recipient, "-o", str(enc_path), str(env_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  Warning: age encryption failed: {result.stderr.strip()}")
        return False

    env_path.unlink()
    print(f"  Encrypted: {env_path} -> {enc_path}")
    print(f"  Deleted plaintext: {env_path}")
    return True


def setup_service(service_name: str, client_secret: str, encrypt: bool) -> None:
    """Run the OAuth flow for a single service."""
    service = SERVICES[service_name]
    output_file = Path(service["output"])

    print(f"Setting up OAuth2 for {service['api_name']}...")
    print(f"Scope: {service['scope']}")
    print("")

    flow = InstalledAppFlow.from_client_secrets_file(
        client_secret, [service["scope"]]
    )
    creds = flow.run_local_server(port=0)

    # Read client_id and client_secret from the original file
    with open(client_secret) as f:
        client_config = json.load(f)

    # Handle both "installed" and "web" credential types
    app_config = client_config.get("installed") or client_config.get("web", {})

    creds_data = {
        "refresh_token": creds.refresh_token,
        "client_id": app_config["client_id"],
        "client_secret": app_config["client_secret"],
    }

    # Write as .env file with JSON value
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w") as f:
        f.write(f'GOOGLE_CREDENTIALS_JSON={json.dumps(json.dumps(creds_data))}\n')

    print(f"Credentials saved to {output_file}")

    if encrypt:
        encrypt_file(output_file)
    else:
        print("")
        print("Next steps:")
        print(f"  1. Encrypt: ./scripts/encrypt-secret.sh {output_file}")
        print(f"  2. Delete plaintext: rm {output_file}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set up Google OAuth2 credentials for one or more services.",
    )
    parser.add_argument(
        "services",
        nargs="*",
        choices=sorted(SERVICES.keys()),
        metavar="SERVICE",
        help=f"Services to authenticate ({', '.join(sorted(SERVICES.keys()))})",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_services",
        help="Set up all services",
    )
    parser.add_argument(
        "--encrypt",
        action="store_true",
        help="Encrypt each .env with age and delete the plaintext",
    )
    parser.add_argument(
        "--encrypt-all",
        action="store_true",
        dest="encrypt_all",
        help="Encrypt all existing .env files in secrets/ (no OAuth flow)",
    )
    parser.add_argument(
        "--client-secret",
        default=CLIENT_SECRET_FILE,
        help=f"Path to OAuth client secret JSON (default: {CLIENT_SECRET_FILE})",
    )
    args = parser.parse_args()

    # Handle --encrypt-all as a standalone action
    if args.encrypt_all:
        secrets_dir = Path("secrets")
        env_files = sorted(secrets_dir.glob("*.env"))
        env_files = [f for f in env_files if not f.name.endswith(".env.enc")]
        if not env_files:
            print("No .env files found in secrets/")
            sys.exit(0)
        print(f"Encrypting {len(env_files)} .env file(s)...")
        for f in env_files:
            encrypt_file(f)
        print(f"\nDone — {len(env_files)} file(s) processed.")
        sys.exit(0)

    if args.all_services and args.services:
        parser.error("Cannot use --all with explicit service names.")
    if not args.all_services and not args.services:
        parser.error("Specify one or more services, or use --all.")

    # --all implies --encrypt
    if args.all_services:
        args.encrypt = True

    # Validate service names (argparse choices check doesn't work with nargs="*"
    # when metavar is set, so validate manually)
    if args.services:
        for svc in args.services:
            if svc not in SERVICES:
                parser.error(
                    f"invalid service '{svc}' (choose from {', '.join(sorted(SERVICES.keys()))})"
                )

    selected = sorted(SERVICES.keys()) if args.all_services else args.services

    client_secret = args.client_secret
    if not Path(client_secret).exists():
        print(f"Error: {client_secret} not found.")
        print("")
        print("Download your OAuth2 credentials from the Google Cloud Console:")
        print("  1. Go to https://console.cloud.google.com/apis/credentials")
        print("  2. Create OAuth2 credentials (Desktop app type)")
        print(f"  3. Download the JSON and save as {client_secret}")
        sys.exit(1)

    total = len(selected)
    for i, svc in enumerate(selected, 1):
        if total > 1:
            print(f"\n{'='*60}")
            print(f"  [{i}/{total}] {svc}")
            print(f"{'='*60}\n")
        setup_service(svc, client_secret, args.encrypt)

    if total > 1:
        print(f"\nDone — {total} services configured.")
    if not args.encrypt:
        print(f"\nTip: use --encrypt to automatically encrypt with age.")


if __name__ == "__main__":
    main()
