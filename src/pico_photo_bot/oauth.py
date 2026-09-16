from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path


def write_private_token(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content)
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


class BootstrapError(ValueError):
    pass


def bootstrap_google_oauth(
    *,
    credentials_variable: str,
    token_variable: str,
    credentials_path: Path,
    token_path: Path,
) -> None:
    files = (
        (credentials_variable, credentials_path, _validate_credentials),
        (token_variable, token_path, _validate_token),
    )
    missing: list[str] = []
    for variable, path, validator in files:
        if path.is_file():
            continue
        encoded = os.getenv(variable, "").strip()
        if not encoded:
            missing.append(variable)
            continue
        try:
            content = base64.b64decode(encoded, validate=True)
            payload = json.loads(content)
            validator(payload)
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, BootstrapError) as exc:
            raise SystemExit(f"Invalid {variable}: {exc}") from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(content)
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
    if missing:
        raise SystemExit(
            "Missing Coolify secret environment variable(s): " + ", ".join(missing)
        )
    print("Google OAuth files are present in persistent storage")


def _validate_credentials(payload: object) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("installed"), dict):
        raise BootstrapError("expected Google Desktop OAuth client JSON")


def _validate_token(payload: object) -> None:
    required = {"refresh_token", "token_uri", "client_id", "client_secret"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise BootstrapError("expected an authorized-user token JSON")
