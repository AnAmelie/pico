from __future__ import annotations

from google_auth_oauthlib.flow import InstalledAppFlow

from .config import PicoSettings
from .oauth import write_private_token
from .photos import PHOTOS_SCOPES


def main() -> None:
    try:
        settings = PicoSettings.from_env()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    if not settings.google_credentials_path.is_file():
        raise SystemExit(
            f"Google OAuth credentials are missing at {settings.google_credentials_path}"
        )
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(settings.google_credentials_path), PHOTOS_SCOPES
        )
        credentials = flow.run_local_server(port=0)
        write_private_token(settings.google_token_path, credentials.to_json())
    except Exception as exc:
        raise SystemExit(f"Google Photos authorization failed: {exc}") from exc
    print(f"Google Photos token saved to {settings.google_token_path}")


if __name__ == "__main__":
    main()
