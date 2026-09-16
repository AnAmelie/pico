import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from pico_photo_bot.config import PicoSettings
from pico_photo_bot.bot import PicoBot, PicoControlView, PicoSubmissionError, parse_submission_url
from pico_photo_bot.models import (
    PhotosAlbum,
    PhotosUploadResult,
    PreparedFile,
    PreparedImport,
    SourceImage,
    SourcePost,
)
from pico_photo_bot.store import PicoStore

UTC = timezone.utc


def settings(root: Path):
    return PicoSettings(
        "token",
        (11,),
        (21,),
        None,
        21,
        "reddit-id",
        "reddit-secret",
        "pico-test",
        ("Family", "Reference"),
        "picoarc0123456789ab",
        root / "pico.db",
        root / "media",
        root / "credentials.json",
        root / "token.json",
        "INFO",
    )


def prepared_import(root: Path, *, count=2, declared_size=3):
    media = root / "media" / "prepared"
    media.mkdir(parents=True, exist_ok=True)
    files = []
    images = []
    for ordinal in range(1, count + 1):
        path = media / f"example.com-{ordinal:02d}.png"
        path.write_bytes(b"png")
        files.append(PreparedFile(ordinal, path, path.name, "image/png", declared_size))
        images.append(SourceImage(ordinal, "https://example.com/image"))
    source = SourcePost(
        "direct",
        "https://example.com/image",
        "example.com",
        "Source: example.com — https://example.com/image",
        tuple(images),
    )
    return PreparedImport("prepared", source, tuple(files))


def prepared_webpage(root: Path, *, count=3, omitted=0, skipped=0):
    media = root / "media" / "webpage"
    media.mkdir(parents=True, exist_ok=True)
    files = []
    images = []
    for ordinal in range(1, count + 1):
        path = media / f"photo-{ordinal:02d}.jpg"
        path.write_bytes(f"photo-{ordinal}".encode())
        credit = f"Photographer {ordinal}"
        files.append(
            PreparedFile(
                ordinal, path, path.name, "image/jpeg", path.stat().st_size
            )
        )
        images.append(
            SourceImage(
                ordinal,
                f"https://cdn.example/{ordinal}.jpg",
                attribution_label=credit,
                attribution_sentence=(
                    f"Photo credit: {credit} — https://publisher.example/story"
                ),
            )
        )
    source = SourcePost(
        "webpage",
        "https://publisher.example/story",
        "publisher.example",
        "Source: publisher.example — https://publisher.example/story",
        tuple(images),
    )
    return PreparedImport(
        "webpage", source, tuple(files), omitted_count=omitted, skipped_count=skipped
    )


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class DiscordMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class Channel:
    def __init__(self, channel_id=21):
        self.id = channel_id
        self.sent = []
        self.messages = {}

    def typing(self):
        return Typing()

    async def send(self, content=None, **kwargs):
        for file in kwargs.get("files", []):
            file.close()
        message = DiscordMessage(100 + len(self.sent) + 1)
        self.sent.append((content, kwargs))
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id):
        return self.messages[message_id]


class IncomingMessage:
    def __init__(self, content, channel, *, user_id=11, bot=False, message_id=50):
        self.id = message_id
        self.content = content
        self.channel = channel
        self.author = SimpleNamespace(id=user_id, bot=bot)
        self.replies = []

    async def reply(self, content, **kwargs):
        self.replies.append((content, kwargs))


class Loader:
    def __init__(self, prepared):
        self.prepared = prepared
        self.closed = False
        self.reddit = SimpleNamespace(close=self._close_reddit)
        self.reddit_closed = False

    async def prepare(self, url, destination):
        return self.prepared

    async def close(self):
        self.closed = True

    async def _close_reddit(self):
        self.reddit_closed = True


class UploadPhotos:
    def __init__(self, store, outcomes=(2,), delay=0):
        self.store = store
        self.outcomes = list(outcomes)
        self.delay = delay
        self.calls = []
        self.closed = False

    async def upload_import(self, import_id, album_name):
        self.calls.append(album_name)
        if self.delay:
            await asyncio.sleep(self.delay)
        item = await self.store.get_import(import_id)
        album = PhotosAlbum("album-id", album_name, "https://photos/album")
        await self.store.set_import_album(import_id, album)
        unresolved = [file for file in item.files if file.media_item_id is None]
        upload_count = min(self.outcomes.pop(0), len(unresolved))
        for file in unresolved[:upload_count]:
            await self.store.mark_file_uploaded(file.id, f"media-{file.ordinal}", f"https://photos/{file.ordinal}")
        if upload_count == len(unresolved):
            await self.store.complete_import_if_ready(import_id)
            await self.store.delete_local_bytes(import_id)
        else:
            await self.store.mark_upload_failed(import_id, "temporary failure")
        refreshed = await self.store.get_import(import_id)
        return PhotosUploadResult(import_id, album, refreshed.uploaded_count, refreshed.remaining_count)

    async def close(self):
        self.closed = True


class HarnessPicoBot(PicoBot):
    def __init__(self, *args, channels=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        return self.channels[channel_id]


class Response:
    def __init__(self):
        self.sent = []
        self.deferred = False

    async def send_message(self, content, **kwargs):
        self.sent.append((content, kwargs))

    async def defer(self, **kwargs):
        self.deferred = True


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))


class Interaction:
    def __init__(self, user_id=11):
        self.user = SimpleNamespace(id=user_id)
        self.response = Response()
        self.followup = Followup()


@pytest.mark.asyncio
async def test_message_filters_and_strict_one_link_intake(tmp_path: Path):
    config = settings(tmp_path)
    store = PicoStore(config.data_path)
    await store.initialize()
    prepared = prepared_import(tmp_path, count=1)
    channel = Channel()
    bot = HarnessPicoBot(config, store, Loader(prepared), UploadPhotos(store), channels=(channel,))

    await bot.on_message(IncomingMessage("https://example.com/image", Channel(99)))
    await bot.on_message(IncomingMessage("https://example.com/image", channel, user_id=12))
    await bot.on_message(IncomingMessage("https://example.com/image", channel, bot=True))
    await bot.on_message(IncomingMessage("ordinary conversation", channel))
    invalid = IncomingMessage("https://a.example/x https://b.example/y", channel)
    await bot.on_message(invalid)
    valid = IncomingMessage("<https://example.com/image>", channel)
    await bot.on_message(valid)
    await asyncio.gather(*tuple(bot._active_tasks))

    assert invalid.replies[0][0] == "Send one URL per message with no extra text."
    assert channel.sent[0][1].get("files")
    assert isinstance(channel.sent[1][1].get("view"), PicoControlView)
    assert valid.replies == []


@pytest.mark.asyncio
async def test_attachments_are_batched_before_control_message(tmp_path: Path):
    config = settings(tmp_path)
    store = PicoStore(config.data_path)
    await store.initialize()
    prepared = prepared_import(tmp_path, count=3, declared_size=10_000_000)
    channel = Channel()
    bot = HarnessPicoBot(config, store, Loader(prepared), UploadPhotos(store), channels=(channel,))

    await bot._prepare_message(IncomingMessage("https://example.com/image", channel), "https://example.com/image")

    assert [len(payload[1].get("files", [])) for payload in channel.sent] == [2, 1, 0]
    assert isinstance(channel.sent[-1][1]["view"], PicoControlView)


@pytest.mark.asyncio
async def test_concurrent_album_selections_run_one_upload(tmp_path: Path):
    config = settings(tmp_path)
    store = PicoStore(config.data_path)
    await store.initialize()
    prepared = prepared_import(tmp_path)
    record = await store.create_import(11, prepared.source.canonical_url)
    await store.mark_ready(record.id, prepared)
    photos = UploadPhotos(store, delay=0.02)
    bot = HarnessPicoBot(config, store, Loader(prepared), photos)
    interactions = (Interaction(), Interaction())

    await asyncio.gather(
        bot.upload_to_album(interactions[0], record.id, "Family"),
        bot.upload_to_album(interactions[1], record.id, "Family"),
    )

    assert photos.calls == ["Family"]
    messages = [interaction.followup.sent[0][0] for interaction in interactions]
    assert any(message == "This upload is already running." for message in messages)
    assert any(
        message
        == 'Added 2 image(s) to https://photos/album. Search Google Photos for '
        '"picoarc0123456789ab" (including the quotation marks), select the newly '
        "added results, and archive them to hide them from the main timeline."
        for message in messages
    )


@pytest.mark.asyncio
async def test_partial_upload_button_retries_locked_album_and_finishes_with_link(tmp_path: Path):
    config = settings(tmp_path)
    store = PicoStore(config.data_path)
    await store.initialize()
    prepared = prepared_import(tmp_path)
    record = await store.create_import(11, prepared.source.canonical_url)
    ready = await store.mark_ready(record.id, prepared)
    channel = Channel()
    control = DiscordMessage(101)
    channel.messages[101] = control
    await store.set_control_message(ready.id, channel.id, control.id)
    photos = UploadPhotos(store, outcomes=(1, 1))
    bot = HarnessPicoBot(config, store, Loader(prepared), photos, channels=(channel,))

    first = Interaction()
    await bot.upload_to_album(first, record.id, "Family")
    assert first.followup.sent[0][0].startswith("Added 1 image(s); 1 remain")
    retry = Interaction()
    await bot.offer_album_selection(retry, record.id)

    assert retry.response.deferred
    assert photos.calls == ["Family", "Family"]
    assert retry.followup.sent[0][0] == (
        'Added 2 image(s) to https://photos/album. Search Google Photos for '
        '"picoarc0123456789ab" (including the quotation marks), select the newly '
        "added results, and archive them to hide them from the main timeline."
    )
    final_view = control.edits[-1]["view"]
    buttons = {child.label: child for child in final_view.children}
    assert buttons["Add to Google Photos"].disabled
    assert buttons["Open album"].url == "https://photos/album"


@pytest.mark.asyncio
async def test_setup_restores_persistent_view_and_cleanup_expires_control(tmp_path: Path):
    config = settings(tmp_path)
    store = PicoStore(config.data_path)
    await store.initialize()
    prepared = prepared_import(tmp_path, count=1)
    record = await store.create_import(11, prepared.source.canonical_url)
    ready = await store.mark_ready(record.id, prepared)
    await store.set_control_message(ready.id, 21, 101)
    channel = Channel()
    control = DiscordMessage(101)
    channel.messages[101] = control
    loader = Loader(prepared)
    photos = UploadPhotos(store)
    bot = HarnessPicoBot(config, store, loader, photos, channels=(channel,))

    await bot.setup_hook()
    assert any(isinstance(view, PicoControlView) for view in bot.persistent_views)
    expired = await store.expire_old_imports(datetime.now(UTC) + timedelta(days=8))

    async def return_expired():
        return expired

    store.expire_old_imports = return_expired
    await bot.cleanup_expired()
    await bot.close()

    assert control.edits[-1]["content"] == "Expired; paste the URL again."
    assert control.edits[-1]["view"].children[0].disabled
    assert loader.closed and loader.reddit_closed and photos.closed


def test_submission_parser_accepts_wrappers_and_ignores_conversation():
    assert parse_submission_url("hello Pico") is None
    assert parse_submission_url("<https://Example.com/image#fragment>") == "https://example.com/image"
    with pytest.raises(PicoSubmissionError, match="one URL"):
        parse_submission_url("caption https://example.com/image")


@pytest.mark.asyncio
async def test_album_selection_rechecks_user_allowlist(tmp_path: Path):
    config = settings(tmp_path)
    store = PicoStore(config.data_path)
    bot = HarnessPicoBot(
        config,
        store,
        Loader(prepared_import(tmp_path, count=1)),
        UploadPhotos(store),
    )
    interaction = Interaction(user_id=12)

    await bot.upload_to_album(interaction, 1, "Family")

    assert interaction.followup.sent[0][0] == (
        "Only configured Pico users can upload these images."
    )


@pytest.mark.asyncio
async def test_webpage_delivers_independent_controls_and_album_uploads(
    tmp_path: Path,
):
    config = settings(tmp_path)
    store = PicoStore(config.data_path)
    await store.initialize()
    prepared = prepared_webpage(tmp_path, count=3, omitted=2, skipped=1)
    channel = Channel()
    photos = UploadPhotos(store, outcomes=(1, 1))
    bot = HarnessPicoBot(
        config, store, Loader(prepared), photos, channels=(channel,)
    )

    await bot._prepare_message(
        IncomingMessage("https://publisher.example/story", channel),
        "https://publisher.example/story",
    )

    photo_messages = channel.sent[:3]
    views = [payload[1]["view"] for payload in photo_messages]
    assert all(len(payload[1]["files"]) == 1 for payload in photo_messages)
    assert all(isinstance(view, PicoControlView) for view in views)
    assert len({view.import_id for view in views}) == 3
    assert channel.sent[3][0] == (
        "Webpage import: 3 prepared, 3 delivered, 1 skipped, "
        "2 omitted by the 20-photo limit."
    )

    first = Interaction()
    second = Interaction()
    await bot.upload_to_album(first, views[0].import_id, "Family")
    await bot.upload_to_album(second, views[1].import_id, "Reference")

    uploaded_first = await store.get_import(views[0].import_id)
    uploaded_second = await store.get_import(views[1].import_id)
    untouched = await store.get_import(views[2].import_id)
    assert uploaded_first.state == uploaded_second.state == "uploaded"
    assert uploaded_first.album_name == "Family"
    assert uploaded_second.album_name == "Reference"
    assert untouched.state == "ready" and untouched.album_name is None
    assert photos.calls == ["Family", "Reference"]
    for uploaded in (uploaded_first, uploaded_second):
        control = channel.messages[uploaded.control_message_id]
        buttons = {child.label: child for child in control.edits[-1]["view"].children}
        assert buttons["Open album"].url == uploaded.album_url
    assert photo_messages[0][1]["view"].import_id != photo_messages[1][1]["view"].import_id

    restored_loader = Loader(prepared)
    restored_photos = UploadPhotos(store)
    restored = HarnessPicoBot(
        config,
        store,
        restored_loader,
        restored_photos,
        channels=(channel,),
    )
    await restored.setup_hook()
    assert len(restored.persistent_views) == 1
    assert restored.persistent_views[0].import_id == untouched.id
    await restored.close()


class FailingSecondPhotoChannel(Channel):
    def __init__(self):
        super().__init__()
        self.photo_count = 0

    async def send(self, content=None, **kwargs):
        files = kwargs.get("files", [])
        if files:
            self.photo_count += 1
        if files and self.photo_count == 2:
            for file in files:
                file.close()
            response = SimpleNamespace(status=413, reason="Payload Too Large")
            raise discord.HTTPException(response, "attachment rejected")
        return await super().send(content, **kwargs)


@pytest.mark.asyncio
async def test_webpage_child_delivery_failure_preserves_sibling_controls_and_bytes(
    tmp_path: Path,
):
    config = settings(tmp_path)
    store = PicoStore(config.data_path)
    await store.initialize()
    prepared = prepared_webpage(tmp_path, count=3)
    channel = FailingSecondPhotoChannel()
    bot = HarnessPicoBot(
        config,
        store,
        Loader(prepared),
        UploadPhotos(store),
        channels=(channel,),
    )

    await bot._prepare_message(
        IncomingMessage("https://publisher.example/story", channel),
        "https://publisher.example/story",
    )

    delivered = [
        payload
        for payload in channel.sent
        if isinstance(payload[1].get("view"), PicoControlView)
    ]
    assert len(delivered) == 2
    assert channel.sent[-1][0].startswith("Webpage import: 3 prepared, 2 delivered")
    first_id = delivered[0][1]["view"].import_id
    failed = await store.get_import(first_id + 1)
    assert failed.state == "failed"
    assert not prepared.files[1].path.exists()
    assert prepared.files[0].path.exists()
    assert prepared.files[2].path.exists()
