from __future__ import annotations

import os
from pathlib import Path

from .oauth import bootstrap_google_oauth


def main() -> None:
    credentials_path = Path(
        os.getenv(
            "PICO_GOOGLE_CREDENTIALS_PATH",
            "/app/data/google-photos-credentials.json",
        )
    )
    token_path = Path(
        os.getenv(
            "PICO_GOOGLE_TOKEN_PATH",
            "/app/data/google-photos-token.json",
        )
    )
    bootstrap_google_oauth(
        credentials_variable="PICO_GOOGLE_CREDENTIALS_BASE64",
        token_variable="PICO_GOOGLE_TOKEN_BASE64",
        credentials_path=credentials_path,
        token_path=token_path,
    )
    from .main import main as run_pico

    run_pico()


if __name__ == "__main__":
    main()
