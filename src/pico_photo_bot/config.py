from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from dotenv import load_dotenv


def parse_discord_ids(name: str, value: str) -> Tuple[int, ...]:
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part for part in parts):
        raise ValueError(f"{name} must contain one or more comma-separated Discord IDs")
    try:
        parsed = tuple(dict.fromkeys(int(part) for part in parts))
    except ValueError as exc:
        raise ValueError(f"{name} must contain only comma-separated integer Discord IDs") from exc
    if any(item <= 0 for item in parsed):
        raise ValueError(f"{name} must contain only positive Discord IDs")
    return parsed


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _positive_int(name: str, required: bool = True) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    if not raw and not required:
        return None
    if not raw:
        raise ValueError(f"Missing required environment variable: {name}")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer Discord ID") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive Discord ID")
    return value


def _required_positive_int(name: str) -> int:
    value = _positive_int(name)
    assert value is not None
    return value


def _nonempty(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _optional_text(name: str) -> Optional[str]:
    value = os.getenv(name, "").strip()
    return value or None


def parse_pico_album_names(value: str) -> Tuple[str, ...]:
    names = tuple(part.strip() for part in value.split(","))
    if not names or any(not name for name in names):
        raise ValueError(
            "PICO_ALBUM_NAMES must contain one or more non-blank comma-separated album names"
        )
    folded = tuple(name.casefold() for name in names)
    if len(set(folded)) != len(folded):
        raise ValueError("PICO_ALBUM_NAMES must not contain duplicate album names")
    if len(names) > 25:
        raise ValueError("PICO_ALBUM_NAMES supports at most 25 album names")
    return names


def parse_pico_archive_search_tag(value: str) -> str:
    tag = value.strip().casefold()
    if re.fullmatch(r"picoarc[a-f0-9]{12}", tag) is None:
        raise ValueError(
            "PICO_ARCHIVE_SEARCH_TAG must be picoarc followed by 12 hexadecimal characters"
        )
    return tag


@dataclass(frozen=True)
class PicoSettings:
    discord_bot_token: str
    discord_user_ids: Tuple[int, ...]
    discord_channel_ids: Tuple[int, ...]
    discord_guild_id: Optional[int]
    channel_id: int
    reddit_client_id: Optional[str]
    reddit_client_secret: Optional[str]
    reddit_user_agent: Optional[str]
    album_names: Tuple[str, ...]
    archive_search_tag: str
    data_path: Path
    media_path: Path
    google_credentials_path: Path
    google_token_path: Path
    log_level: str
    openrouter_api_key: Optional[str] = None
    openrouter_model: str = "openai/gpt-5-nano"

    @classmethod
    def from_env(cls) -> "PicoSettings":
        load_dotenv()
        discord_user_ids = parse_discord_ids(
            "DISCORD_USER_ID", _required("DISCORD_USER_ID")
        )
        discord_channel_ids = parse_discord_ids(
            "DISCORD_CHANNEL_ID", _required("DISCORD_CHANNEL_ID")
        )
        channel_id = _required_positive_int("PICO_CHANNEL_ID")
        if channel_id not in discord_channel_ids:
            raise ValueError("PICO_CHANNEL_ID must be present in DISCORD_CHANNEL_ID")
        reddit_client_id = _optional_text("PICO_REDDIT_CLIENT_ID")
        reddit_client_secret = _optional_text("PICO_REDDIT_CLIENT_SECRET")
        reddit_user_agent = _optional_text("PICO_REDDIT_USER_AGENT")
        if bool(reddit_client_id) != bool(reddit_client_secret):
            raise ValueError(
                "PICO_REDDIT_CLIENT_ID and PICO_REDDIT_CLIENT_SECRET must be set together"
            )
        if reddit_client_id and not reddit_user_agent:
            raise ValueError(
                "PICO_REDDIT_USER_AGENT is required when Reddit credentials are configured"
            )
        return cls(
            discord_bot_token=_required("PICO_DISCORD_BOT_TOKEN"),
            discord_user_ids=discord_user_ids,
            discord_channel_ids=discord_channel_ids,
            discord_guild_id=_positive_int("DISCORD_GUILD_ID", required=False),
            channel_id=channel_id,
            reddit_client_id=reddit_client_id,
            reddit_client_secret=reddit_client_secret,
            reddit_user_agent=reddit_user_agent,
            album_names=parse_pico_album_names(_required("PICO_ALBUM_NAMES")),
            archive_search_tag=parse_pico_archive_search_tag(
                _required("PICO_ARCHIVE_SEARCH_TAG")
            ),
            data_path=Path(
                _nonempty("PICO_DATA_PATH", "data/pico.db")
            ).expanduser(),
            media_path=Path(
                _nonempty("PICO_MEDIA_PATH", "data/pico-media")
            ).expanduser(),
            google_credentials_path=Path(
                _nonempty(
                    "PICO_GOOGLE_CREDENTIALS_PATH",
                    "data/google-photos-credentials.json",
                )
            ).expanduser(),
            google_token_path=Path(
                _nonempty(
                    "PICO_GOOGLE_TOKEN_PATH",
                    "data/google-photos-token.json",
                )
            ).expanduser(),
            log_level=_nonempty("LOG_LEVEL", "INFO").upper(),
            openrouter_api_key=_optional_text("OPENROUTER_API_KEY"),
            openrouter_model=_nonempty(
                "PICO_OPENROUTER_MODEL", "openai/gpt-5-nano"
            ),
        )
