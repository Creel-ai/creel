#!/usr/bin/env python3
"""One-time Google OAuth2 setup for supported services.

This script performs the OAuth dance to obtain a refresh token for a
Google API. The resulting credentials are saved to a .env file that
should then be encrypted with age.

Supported services:
  gcal  — Google Calendar (read-only)
  gmail — Gmail (read-only)

Usage:
    1. Create a GCP project and enable the relevant API
    2. Create OAuth2 credentials (desktop app type)
    3. Download the credentials JSON and save as client_secret.json
    4. Run: python scripts/setup-google-oauth.py <service>
    5. Encrypt: ./scripts/encrypt-secret.sh secrets/<service>.env

Requires: google-auth-oauthlib
"""

from __future__ import annotations

import argparse
import json
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
        "api_name": "Google Calendar",
    },
    "gmail": {
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "output": "secrets/gmail.env",
        "api_name": "Gmail",
    },
}

CLIENT_SECRET_FILE = "client_secret.json"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set up Google OAuth2 credentials for a service.",
    )
    parser.add_argument(
        "service",
        choices=sorted(SERVICES.keys()),
        help="Google service to authenticate (e.g. gcal, gmail)",
    )
    parser.add_argument(
        "--client-secret",
        default=CLIENT_SECRET_FILE,
        help=f"Path to OAuth client secret JSON (default: {CLIENT_SECRET_FILE})",
    )
    args = parser.parse_args()

    service = SERVICES[args.service]
    client_secret = args.client_secret
    output_file = Path(service["output"])

    if not Path(client_secret).exists():
        print(f"Error: {client_secret} not found.")
        print("")
        print("Download your OAuth2 credentials from the Google Cloud Console:")
        print("  1. Go to https://console.cloud.google.com/apis/credentials")
        print("  2. Create OAuth2 credentials (Desktop app type)")
        print(f"  3. Download the JSON and save as {client_secret}")
        sys.exit(1)

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
    print("")
    print("Next steps:")
    print(f"  1. Encrypt: ./scripts/encrypt-secret.sh {output_file}")
    print(f"  2. Delete plaintext: rm {output_file}")
    print(f"  3. Delete client_secret.json: rm {client_secret}")


if __name__ == "__main__":
    main()
