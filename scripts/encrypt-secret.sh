#!/usr/bin/env bash
# Encrypt a plaintext .env file using age.
#
# Usage:
#   ./scripts/encrypt-secret.sh secrets/gcal.env
#
# Requires: age (https://github.com/FiloSottile/age)
#
# The encrypted file will be written to <input>.enc.
# The plaintext file will NOT be deleted automatically - do that yourself.

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <env-file> [recipient-file]"
    echo ""
    echo "Encrypts an .env file with age."
    echo "Default recipient: ~/.age/key.pub or AGE_RECIPIENT_FILE"
    exit 1
fi

INPUT="$1"
RECIPIENT_FILE="${2:-${AGE_RECIPIENT_FILE:-$HOME/.age/key.pub}}"
OUTPUT="${INPUT}.enc"

if [ ! -f "$INPUT" ]; then
    echo "Error: Input file not found: $INPUT"
    exit 1
fi

if [ ! -f "$RECIPIENT_FILE" ]; then
    echo "Error: Recipient file not found: $RECIPIENT_FILE"
    echo ""
    echo "Generate an age key pair with:"
    echo "  mkdir -p ~/.age"
    echo "  age-keygen -o ~/.age/key.txt 2> ~/.age/key.pub"
    exit 1
fi

RECIPIENT=$(grep -oE 'age1[a-z0-9]+' "$RECIPIENT_FILE")

age -e -r "$RECIPIENT" -o "$OUTPUT" "$INPUT"

echo "Encrypted: $INPUT -> $OUTPUT"
echo ""
echo "IMPORTANT: Delete the plaintext file:"
echo "  rm $INPUT"
