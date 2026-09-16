from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable

import discord

from .config import PicoSettings
from .models import PhotosUploadResult, PicoFile, PicoImport, PreparedFile
from .photos import GooglePhotosClient, PhotosApiError
from .sources import PicoMediaLoader, PicoSourceError
from .store import PicoStore
from .public_url import PublicUrlError, normalize_public_http_url

LOGGER = logging.getLogger(__name__)
MAX_DISCORD_ATTACHMENTS = 10
MAX_DISCORD_MESSAGE_BYTES = 24_000_000
_LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)


class PicoSubmissionError(ValueError):
    pass


def parse_submission_url(content: str) -> str | None:
    clean = content.strip()
    if not clean:
        return None
    candidate = clean[1:-1].strip() if clean.startswith("<") and clean.endswith(">") else clean
    links = _LINK_RE.findall(candidate)
    if not links:
        if "://" in candidate:
            raise PicoSubmissionError(
                "Send one public HTTP(S) Reddit post or direct image URL per message."
            )
        return None
    if len(links) != 1 or links[0] != candidate or any(char.isspace() for char in candidate):
        raise PicoSubmissionError(
            "Send one URL per message with no extra text."
        )
    try:
        return normalize_public_http_url(candidate)
    except PublicUrlError as exc:
        raise PicoSubmissionError(str(exc)) from exc


def attachment_batches(
    files: Iterable[PreparedFile | PicoFile],
) -> tuple[tuple[PreparedFile | PicoFile, ...], ...]:
    batches: list[tuple[PreparedFile | PicoFile, ...]] = []
    current: list[PreparedFile | PicoFile] = []
    current_size = 0
    for file in files:
        if file.size > MAX_DISCORD_MESSAGE_BYTES:
            raise ValueError(f"{file.filename} exceeds Pico's Discord attachment limit")
        if current and (
            len(current) == MAX_DISCORD_ATTACHMENTS
            or current_size + file.size > MAX_DISCORD_MESSAGE_BYTES
        ):
            batches.append(tuple(current))
            current = []
            current_size = 0
        current.append(file)
        current_size += file.size
    if current:
        batches.append(tuple(current))
    return tuple(batches)


class PicoControlView(discord.ui.View):
    def __init__(
        self,
        bot: "PicoBot",
        import_id: int,
        *,
        disabled: bool = False,
        album_url: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.import_id = import_id
        button = self.children[0]
        if isinstance(button, discord.ui.Button):
            button.disabled = disabled
        if album_url:
            self.add_item(
                discord.ui.Button(
                    label="Open album",
                    style=discord.ButtonStyle.link,
                    url=album_url,
                )
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.bot.settings.discord_user_ids:
            return True
        await interaction.response.send_message(
            "Only configured Pico users can upload these images.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="Add to Google Photos",
        style=discord.ButtonStyle.primary,
        custom_id="pico:add-to-google-photos",
    )
    async def add_to_photos(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.bot.offer_album_selection(interaction, self.import_id)


class PicoAlbumSelect(discord.ui.Select):
    def __init__(self, bot: "PicoBot", import_id: int) -> None:
        self.bot = bot
        self.import_id = import_id
        super().__init__(
            placeholder="Choose a Google Photos album",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=name, value=name) for name in bot.settings.album_names],
            custom_id=f"pico:album:{import_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.upload_to_album(interaction, self.import_id, self.values[0])


class PicoAlbumSelectView(discord.ui.View):
    def __init__(self, bot: "PicoBot", import_id: int) -> None:
        super().__init__(timeout=300)
        self.add_item(PicoAlbumSelect(bot, import_id))


class PicoBot(discord.Client):
    def __init__(
        self,
        settings: PicoSettings,
        store: PicoStore,
        media_loader: PicoMediaLoader,
        photos: GooglePhotosClient,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.store = store
        self.media_loader = media_loader
        self.photos = photos
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._cleanup_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        await self.store.initialize()
        await self.store.recover_startup(self.settings.media_path)
        for item in await self.store.persistent_imports():
            if item.control_message_id is not None:
                self.add_view(
                    PicoControlView(self, item.id),
                    message_id=item.control_message_id,
                )
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(), name="pico-daily-cleanup"
        )

    async def close(self) -> None:
        tasks = set(self._active_tasks)
        if self._cleanup_task is not None:
            tasks.add(self._cleanup_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.media_loader.close()
        await self.media_loader.reddit.close()
        await self.photos.close()
        await super().close()

    async def on_ready(self) -> None:
        LOGGER.info("Pico connected to Discord as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id != self.settings.channel_id:
            return
        if message.author.id not in self.settings.discord_user_ids:
            return
        if getattr(message.author, "bot", False):
            return
        try:
            url = parse_submission_url(message.content)
        except PicoSubmissionError as exc:
            await message.reply(str(exc), mention_author=False)
            return
        if url is None:
            return
        task = asyncio.create_task(
            self._prepare_message(message, url),
            name=f"pico-prepare-{message.id}",
        )
        self._active_tasks.add(task)
        task.add_done_callback(self._preparation_done)

    def _preparation_done(self, task: asyncio.Task[None]) -> None:
        self._active_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            LOGGER.error(
                "Unhandled Pico preparation failure",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _prepare_message(self, message: discord.Message, url: str) -> None:
        item = await self.store.create_import(message.author.id, url)
        try:
            async with message.channel.typing():
                prepared = await self.media_loader.prepare(url, self.settings.media_path)
            ready = await self.store.mark_ready(item.id, prepared)
            if ready is None:
                raise PicoSourceError("Prepared import could not be persisted.")
            for batch in attachment_batches(prepared.files):
                await message.channel.send(
                    files=[discord.File(file.path, filename=file.filename) for file in batch]
                )
            control = await message.channel.send(
                self._control_content(ready),
                view=PicoControlView(self, item.id),
            )
            if not await self.store.set_control_message(
                item.id, message.channel.id, control.id
            ):
                raise PicoSourceError("Pico could not persist its upload control.")
        except (PicoSourceError, ValueError) as exc:
            LOGGER.info("Pico rejected import %s: %s", item.id, exc)
            current = await self.store.get_import(item.id)
            if current is not None and current.state == "preparing":
                await self.store.mark_preparation_failed(item.id, str(exc))
            elif current is not None and current.state == "ready":
                await self.store.mark_delivery_failed(item.id, str(exc))
                await self.store.delete_local_bytes(item.id)
            await message.reply(str(exc)[:500], mention_author=False)
        except discord.HTTPException as exc:
            LOGGER.warning("Pico Discord delivery failed for import %s: %s", item.id, exc)
            current = await self.store.get_import(item.id)
            if current is not None and current.state == "ready":
                await self.store.mark_delivery_failed(
                    item.id,
                    "Discord rejected an attachment; paste the URL again after checking the channel upload limit.",
                )
                await self.store.delete_local_bytes(item.id)
            await message.reply(
                "Discord rejected an attachment; paste the URL again after checking the channel upload limit.",
                mention_author=False,
            )
        except Exception as exc:
            LOGGER.exception("Pico preparation failed for import %s", item.id)
            current = await self.store.get_import(item.id)
            if current is not None and current.state == "preparing":
                await self.store.mark_preparation_failed(item.id, str(exc))
            await message.reply(
                "Pico could not prepare that source; check the link and try again.",
                mention_author=False,
            )

    async def offer_album_selection(
        self, interaction: discord.Interaction, import_id: int
    ) -> None:
        item = await self.store.get_import(import_id)
        if item is None:
            await interaction.response.send_message(
                "This Pico import no longer exists.", ephemeral=True
            )
            return
        if item.state == "uploaded":
            await interaction.response.send_message(
                f"Already added to {item.album_url}.", ephemeral=True
            )
            return
        if item.state == "uploading":
            await interaction.response.send_message(
                "This upload is already running.", ephemeral=True
            )
            return
        if item.state == "expired":
            await interaction.response.send_message(
                "Expired; paste the URL again.", ephemeral=True
            )
            return
        if item.state == "failed" and item.album_name:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.upload_to_album(interaction, import_id, item.album_name)
            return
        if item.state != "ready":
            await interaction.response.send_message(
                item.last_error or "This import is not ready to upload.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Choose the Pico-created Google Photos album for this import.",
            view=PicoAlbumSelectView(self, import_id),
            ephemeral=True,
        )

    async def upload_to_album(
        self, interaction: discord.Interaction, import_id: int, album_name: str
    ) -> None:
        if interaction.user.id not in self.settings.discord_user_ids:
            await interaction.followup.send(
                "Only configured Pico users can upload these images.", ephemeral=True
            )
            return
        claimed, item = await self.store.claim_upload(import_id, album_name)
        if not claimed:
            if item is None:
                message = "This Pico import no longer exists."
            elif item.state == "uploading":
                message = "This upload is already running."
            elif item.state == "uploaded":
                message = f"Already added to {item.album_url}."
            elif item.state == "expired":
                message = "Expired; paste the URL again."
            else:
                message = item.last_error or "This import cannot be uploaded."
            await interaction.followup.send(message, ephemeral=True)
            return
        assert item is not None and item.album_name is not None
        try:
            result = await self.photos.upload_import(import_id, item.album_name)
        except PhotosApiError as exc:
            LOGGER.warning("Pico Photos upload failed for import %s: %s", import_id, exc)
            await self._refresh_control(import_id)
            await interaction.followup.send(str(exc)[:500], ephemeral=True)
            return
        await self._refresh_control(import_id, result)
        if result.complete:
            count = result.completed_count
            await interaction.followup.send(
                f"Added {count} image(s) to {result.album.product_url}. "
                f'Search Google Photos for "{self.settings.archive_search_tag}" '
                "(including the quotation marks), select the newly added results, "
                "and archive them to hide them from the main timeline.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Added {result.completed_count} image(s); {result.remaining_count} remain. "
                "Press Add to Google Photos again to retry the locked album.",
                ephemeral=True,
            )

    async def _refresh_control(
        self, import_id: int, result: PhotosUploadResult | None = None
    ) -> None:
        item = await self.store.get_import(import_id)
        if (
            item is None
            or item.control_channel_id is None
            or item.control_message_id is None
        ):
            return
        try:
            channel = self.get_channel(item.control_channel_id)
            if channel is None:
                channel = await self.fetch_channel(item.control_channel_id)
            message = await channel.fetch_message(item.control_message_id)
            if item.state == "uploaded":
                content = self._control_content(item) + "\nUpload complete."
                view = PicoControlView(
                    self,
                    item.id,
                    disabled=True,
                    album_url=item.album_url,
                )
            else:
                status = (
                    f"\nUpload incomplete: {item.uploaded_count} added, "
                    f"{item.remaining_count} remaining."
                    if result is not None
                    else f"\nUpload failed: {(item.last_error or 'retry the import.')[:300]}"
                )
                content = self._control_content(item) + status
                view = PicoControlView(self, item.id)
            await message.edit(content=content, view=view)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            LOGGER.exception("Pico could not refresh control message for import %s", import_id)

    async def _cleanup_loop(self) -> None:
        while not self.is_closed():
            try:
                await self.cleanup_expired()
            except Exception:
                LOGGER.exception("Pico daily cleanup failed")
            await asyncio.sleep(24 * 60 * 60)

    async def cleanup_expired(self) -> None:
        for item in await self.store.expire_old_imports():
            if item.control_channel_id is None or item.control_message_id is None:
                continue
            try:
                channel = self.get_channel(item.control_channel_id)
                if channel is None:
                    channel = await self.fetch_channel(item.control_channel_id)
                message = await channel.fetch_message(item.control_message_id)
                await message.edit(
                    content="Expired; paste the URL again.",
                    view=PicoControlView(self, item.id, disabled=True),
                )
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                LOGGER.exception(
                    "Pico could not expire control message for import %s", item.id
                )

    @staticmethod
    def _control_content(item: PicoImport) -> str:
        return (
            f"Source: {item.source_url}\n"
            f"Attribution: {item.attribution_label}\n"
            f"Images: {len(item.files)}"
        )
