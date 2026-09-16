from pathlib import Path

import pytest

from pico_photo_bot.config import PicoSettings

BASE_ENV = {
    "PICO_DISCORD_BOT_TOKEN": "pico-token",
    "PICO_CHANNEL_ID": "21",
    "PICO_REDDIT_CLIENT_ID": "client-id",
    "PICO_REDDIT_CLIENT_SECRET": "client-secret",
    "PICO_REDDIT_USER_AGENT": "pico-photo-bot/test",
    "PICO_ALBUM_NAMES": " Family ,Reference Photos ",
    "PICO_ARCHIVE_SEARCH_TAG": " PICOARC0123456789AB ",
    "DISCORD_USER_ID": "11,12",
    "DISCORD_CHANNEL_ID": "20,21",
    "DISCORD_GUILD_ID": "31",
    "LOG_LEVEL": "debug",
}
OPTIONAL_ENV = (
    "PICO_DATA_PATH",
    "PICO_MEDIA_PATH",
    "PICO_GOOGLE_CREDENTIALS_PATH",
    "PICO_GOOGLE_TOKEN_PATH",
)


def set_env(monkeypatch, **overrides):
    monkeypatch.setattr("pico_photo_bot.config.load_dotenv", lambda: None)
    values = BASE_ENV | overrides
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in OPTIONAL_ENV:
        if name not in values:
            monkeypatch.delenv(name, raising=False)


def test_pico_settings_are_isolated_and_apply_defaults(monkeypatch):
    set_env(monkeypatch)

    settings = PicoSettings.from_env()

    assert settings.discord_bot_token == "pico-token"
    assert settings.discord_user_ids == (11, 12)
    assert settings.channel_id == 21
    assert settings.album_names == ("Family", "Reference Photos")
    assert settings.archive_search_tag == "picoarc0123456789ab"
    assert settings.data_path == Path("data/pico.db")
    assert settings.media_path == Path("data/pico-media")
    assert settings.google_credentials_path == Path("data/google-photos-credentials.json")
    assert settings.google_token_path == Path("data/google-photos-token.json")
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    "name",
    [
        "PICO_DISCORD_BOT_TOKEN",
        "PICO_ALBUM_NAMES",
        "PICO_ARCHIVE_SEARCH_TAG",
    ],
)
def test_pico_settings_require_core_service_configuration(monkeypatch, name):
    set_env(monkeypatch, **{name: ""})
    with pytest.raises(ValueError, match=name):
        PicoSettings.from_env()


@pytest.mark.parametrize(
    "value",
    [
        "picoarc0123456789a",
        "picoarc0123456789ag",
        "picoarc0123456789a!",
        "archive0123456789ab",
    ],
)
def test_pico_archive_search_tag_rejects_invalid_values(monkeypatch, value):
    set_env(monkeypatch, PICO_ARCHIVE_SEARCH_TAG=value)
    with pytest.raises(
        ValueError,
        match="^PICO_ARCHIVE_SEARCH_TAG must be picoarc followed by 12 hexadecimal characters$",
    ):
        PicoSettings.from_env()


def test_pico_settings_allow_direct_image_only_mode(monkeypatch):
    set_env(
        monkeypatch,
        PICO_REDDIT_CLIENT_ID="",
        PICO_REDDIT_CLIENT_SECRET="",
        PICO_REDDIT_USER_AGENT="",
    )

    settings = PicoSettings.from_env()

    assert settings.reddit_client_id is None
    assert settings.reddit_client_secret is None
    assert settings.reddit_user_agent is None


@pytest.mark.parametrize(
    ("client_id", "client_secret"),
    [("client-id", ""), ("", "client-secret")],
)
def test_pico_settings_reject_partial_reddit_credentials(
    monkeypatch, client_id, client_secret
):
    set_env(
        monkeypatch,
        PICO_REDDIT_CLIENT_ID=client_id,
        PICO_REDDIT_CLIENT_SECRET=client_secret,
    )
    with pytest.raises(ValueError, match="must be set together"):
        PicoSettings.from_env()


def test_pico_settings_require_user_agent_with_reddit_credentials(monkeypatch):
    set_env(monkeypatch, PICO_REDDIT_USER_AGENT="")
    with pytest.raises(ValueError, match="PICO_REDDIT_USER_AGENT is required"):
        PicoSettings.from_env()


def test_pico_channel_must_be_in_shared_allowlist(monkeypatch):
    set_env(monkeypatch, PICO_CHANNEL_ID="99")
    with pytest.raises(ValueError, match="must be present in DISCORD_CHANNEL_ID"):
        PicoSettings.from_env()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("Family,,Reference", "non-blank"),
        ("Family,fAMILY", "duplicate"),
        (",".join(f"Album {index}" for index in range(26)), "at most 25"),
    ],
)
def test_pico_album_list_rejects_ambiguous_select_options(monkeypatch, value, message):
    set_env(monkeypatch, PICO_ALBUM_NAMES=value)
    with pytest.raises(ValueError, match=message):
        PicoSettings.from_env()
