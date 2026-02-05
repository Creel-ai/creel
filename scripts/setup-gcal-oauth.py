#!/usr/bin/env python3
"""One-time Google Calendar OAuth2 setup.

This script performs the OAuth dance to obtain a refresh token for the
Google Calendar API. The resulting credentials are saved to a .env file
that should then be encrypted with age.

Usage:
    1. Create a GCP project and enable the Calendar API
    2. Create OAuth2 credentials (desktop app type)
    3. Download the credentials JSON and save as client_secret.json
    4. Run: python scripts/setup-gcal-oauth.py
    5. Encrypt: ./scripts/encrypt-secret.sh secrets/gcal.env

Requires: google-auth-oauthlib
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Error: google-auth-oauthlib not installed.")
    print("Install with: pip install google-auth-oauthlib")
    sys.exit(1)


SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CLIENT_SECRET_FILE = "client_secret.json"
OUTPUT_FILE = Path("secrets/gcal.env")


def main() -> None:
    if not Path(CLIENT_SECRET_FILE).exists():
        print(f"Error: {CLIENT_SECRET_FILE} not found.")
        print("")
        print("Download your OAuth2 credentials from the Google Cloud Console:")
        print("  1. Go to https://console.cloud.google.com/apis/credentials")
        print("  2. Create OAuth2 credentials (Desktop app type)")
        print("  3. Download the JSON and save as client_secret.json")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    # Read client_id and client_secret from the original file
    with open(CLIENT_SECRET_FILE) as f:
        client_config = json.load(f)

    # Handle both "installed" and "web" credential types
    app_config = client_config.get("installed") or client_config.get("web", {})

    creds_data = {
        "refresh_token": creds.refresh_token,
        "client_id": app_config["client_id"],
        "client_secret": app_config["client_secret"],
    }

    # Write as .env file with JSON value
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        f.write(f'GOOGLE_CREDENTIALS_JSON={json.dumps(json.dumps(creds_data))}\n')

    print(f"Credentials saved to {OUTPUT_FILE}")
    print("")
    print("Next steps:")
    print(f"  1. Encrypt: ./scripts/encrypt-secret.sh {OUTPUT_FILE}")
    print(f"  2. Delete plaintext: rm {OUTPUT_FILE}")
    print(f"  3. Delete client_secret.json: rm {CLIENT_SECRET_FILE}")


if __name__ == "__main__":
    main()
