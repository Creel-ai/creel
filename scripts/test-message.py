#!/usr/bin/env python3
"""Send a test iMessage to verify your setup.

Loads $PHONE from the root .env and sends a short test message.

Usage:
    python scripts/test-message.py
    python scripts/test-message.py "Custom message here"
    python scripts/test-message.py --to +1234567890
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from taskrunner.models import OutputConfig
from taskrunner.outputs import send_output
from taskrunner.secrets import parse_env_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a test iMessage.")
    parser.add_argument(
        "message",
        nargs="?",
        default="Hello from LLM Task Runner! If you see this, iMessage output is working.",
        help="Message to send (default: test message)",
    )
    parser.add_argument(
        "--to",
        default=None,
        help="Phone number (default: $PHONE from .env)",
    )
    args = parser.parse_args()

    # Load root .env
    root_env = Path(".env")
    if root_env.exists():
        for key, value in parse_env_file(root_env).items():
            os.environ.setdefault(key, value)

    phone = args.to or os.environ.get("PHONE")
    if not phone:
        print("Error: No phone number provided.")
        print("")
        print("Either:")
        print("  1. Set PHONE in .env")
        print("  2. Pass --to +1234567890")
        sys.exit(1)

    print(f"Sending to: {phone}")
    print(f"Message: {args.message}")

    config = OutputConfig(type="imessage", to=phone)
    send_output(args.message, config)
    print("Sent!")


if __name__ == "__main__":
    main()
