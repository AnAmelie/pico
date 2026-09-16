from __future__ import annotations

import logging
from importlib.util import find_spec

from .attribution import PhotographerFinder
from .config import PicoSettings
from .bot import PicoBot
from .metadata import PicoMetadataError, PicoMetadataWriter
from .photos import GooglePhotosClient, PhotosAuthorizationError
from .sources import PicoMediaLoader, RedditPostClient
from .store import PicoStore
from .webpage import PlaywrightWebpageRenderer

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
            "Pico Reddit credentials are not configured; Reddit links are unavailable"
        )
    renderer = None
    if find_spec("playwright") is None:
        LOGGER.warning(
            "Playwright is not installed; webpage links are unavailable, while "
            "Reddit and direct-image imports remain enabled"
        )
    else:
        renderer = PlaywrightWebpageRenderer()
    finder = None
    if settings.openrouter_api_key:
        finder = PhotographerFinder(
            settings.openrouter_api_key,
            settings.openrouter_model,
        )
    else:
        LOGGER.info(
            "OPENROUTER_API_KEY is not configured; webpage credits use publisher "
            "metadata and source-only fallback"
        )
    media = PicoMediaLoader(
        reddit,
        metadata,
        settings.archive_search_tag,
        webpage_renderer=renderer,
        photographer_finder=finder,
    )
    LOGGER.info("Configured Pico albums: %s", ", ".join(settings.album_names))
    bot = PicoBot(settings, store, media, photos)
    bot.run(settings.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
