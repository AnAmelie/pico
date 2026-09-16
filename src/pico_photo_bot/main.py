from __future__ import annotations

import logging

from .config import PicoSettings
from .bot import PicoBot
from .metadata import PicoMetadataError, PicoMetadataWriter
from .photos import GooglePhotosClient, PhotosAuthorizationError
from .sources import PicoMediaLoader, RedditPostClient
from .store import PicoStore

LOGGER = logging.getLogger(__name__)


def main() -> None:
    try:
        settings = PicoSettings.from_env()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    store = PicoStore(settings.data_path)
    try:
        metadata = PicoMetadataWriter()
        photos = GooglePhotosClient(
            settings.google_credentials_path,
            settings.google_token_path,
            store,
        )
    except (PicoMetadataError, PhotosAuthorizationError) as exc:
        raise SystemExit(str(exc)) from exc
    reddit = RedditPostClient(
        settings.reddit_client_id,
        settings.reddit_client_secret,
        settings.reddit_user_agent,
    )
    if not settings.reddit_client_id:
        LOGGER.warning(
            "Pico Reddit credentials are not configured; running in direct-image-only mode"
        )
    media = PicoMediaLoader(reddit, metadata, settings.archive_search_tag)
    LOGGER.info("Configured Pico albums: %s", ", ".join(settings.album_names))
    bot = PicoBot(settings, store, media, photos)
    bot.run(settings.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
