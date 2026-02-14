"""stdin/stdout channel for interactive CLI chat."""

from __future__ import annotations

import sys
from typing import Callable

from taskrunner.channels import Channel


class StdinChannel(Channel):
    """Interactive CLI channel using stdin/stdout."""

    SENDER_ID = "cli"

    def listen(self, callback: Callable[[str, str], str]) -> None:
        """Read from stdin, send responses to stdout."""
        print("Chat started. Type 'quit' or 'exit' to stop. 'clear' to reset session.")
        print()

        while True:
            try:
                text = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break

            if not text:
                continue
            if text.lower() in ("quit", "exit"):
                print("Goodbye!")
                break

            response = callback(self.SENDER_ID, text)
            print(f"Assistant: {response}")
            print()

    def send(self, recipient: str, text: str) -> None:
        """Print to stdout."""
        print(f"Assistant: {text}")
