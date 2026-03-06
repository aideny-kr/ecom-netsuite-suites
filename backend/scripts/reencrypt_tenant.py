"""Re-encrypt tenant credentials from one Fernet key to another.

Usage:
    SOURCE_ENCRYPTION_KEY=<source-key> TARGET_ENCRYPTION_KEY=<target-key> \\
    python -m scripts.reencrypt_tenant --input export.json --output reencrypted.json

This script:
1. Reads the export JSON
2. Decrypts encrypted_credentials with SOURCE_ENCRYPTION_KEY
3. Re-encrypts with TARGET_ENCRYPTION_KEY
4. Writes a new JSON with the re-encrypted values
5. Never writes plaintext credentials to disk
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet


def reencrypt(input_path: str, output_path: str) -> None:
    """Re-encrypt all credential fields in an export JSON."""
    source_key = os.environ.get("SOURCE_ENCRYPTION_KEY")
    target_key = os.environ.get("TARGET_ENCRYPTION_KEY")

    if not source_key or not target_key:
        print("ERROR: Both SOURCE_ENCRYPTION_KEY and TARGET_ENCRYPTION_KEY must be set")
        sys.exit(1)

    source_fernet = Fernet(source_key.encode())
    target_fernet = Fernet(target_key.encode())

    data = json.loads(Path(input_path).read_text())
    reencrypted_count = 0

    # Re-encrypt connections.encrypted_credentials
    for row in data.get("tables", {}).get("connections", []):
        enc = row.get("encrypted_credentials")
        if enc and enc != "__EXCLUDED__":
            plaintext = source_fernet.decrypt(enc.encode())
            row["encrypted_credentials"] = target_fernet.encrypt(plaintext).decode()
            reencrypted_count += 1
            print(f"  Re-encrypted connection {str(row.get('id', '?'))[:8]}")

    # Re-encrypt tenant_configs.ai_api_key_encrypted
    for row in data.get("tables", {}).get("tenant_configs", []):
        enc = row.get("ai_api_key_encrypted")
        if enc:
            plaintext = source_fernet.decrypt(enc.encode())
            row["ai_api_key_encrypted"] = target_fernet.encrypt(plaintext).decode()
            reencrypted_count += 1
            print(f"  Re-encrypted AI key for config {str(row.get('id', '?'))[:8]}")

    # Re-encrypt mcp_connectors
    for row in data.get("tables", {}).get("mcp_connectors", []):
        enc = row.get("encrypted_credentials")
        if enc and enc != "__EXCLUDED__":
            plaintext = source_fernet.decrypt(enc.encode())
            row["encrypted_credentials"] = target_fernet.encrypt(plaintext).decode()
            reencrypted_count += 1
            print(f"  Re-encrypted MCP connector {str(row.get('id', '?'))[:8]}")

    Path(output_path).write_text(json.dumps(data, indent=2))
    print(f"\nRe-encrypted {reencrypted_count} fields → {output_path}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Re-encrypt tenant credentials between Fernet keys")
    parser.add_argument("--input", required=True, help="Input export JSON")
    parser.add_argument("--output", required=True, help="Output re-encrypted JSON")
    args = parser.parse_args()

    reencrypt(args.input, args.output)


if __name__ == "__main__":
    main()
