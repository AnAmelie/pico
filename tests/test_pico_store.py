import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pico_photo_bot.models import PreparedFile, PreparedImport, SourceImage, SourcePost
from pico_photo_bot.store import PicoStore

UTC = timezone.utc


async def ready_import(store: PicoStore, root: Path, *, count=2):
    record = await store.create_import(11, "https://example.com/image")
    media = root / "media" / f"import-{record.id}"
    media.mkdir(parents=True)
    files = []
    images = []
    for ordinal in range(1, count + 1):
        path = media / f"example.com-{ordinal:02d}.png"
        path.write_bytes(b"png")
        files.append(PreparedFile(ordinal, path, path.name, "image/png", 3))
        images.append(SourceImage(ordinal, "https://example.com/image"))
    source = SourcePost(
        "direct",
        "https://example.com/image",
        "example.com",
        "Source: example.com — https://example.com/image",
        tuple(images),
    )
    ready = await store.mark_ready(record.id, PreparedImport(str(record.id), source, tuple(files)))
    assert ready is not None
    return ready, media


@pytest.mark.asyncio
async def test_first_album_selection_atomically_locks_destination(tmp_path: Path):
    store = PicoStore(tmp_path / "pico.db")
    await store.initialize()
    item, _ = await ready_import(store, tmp_path)

    claims = await asyncio.gather(
        store.claim_upload(item.id, "Family"),
        store.claim_upload(item.id, "Reference"),
    )

    assert sum(claimed for claimed, _ in claims) == 1
    current = await store.get_import(item.id)
    assert current.state == "uploading"
    assert current.album_name in {"Family", "Reference"}
    losing_name = "Reference" if current.album_name == "Family" else "Family"
    await store.mark_upload_failed(item.id, "retry")
    claimed, unchanged = await store.claim_upload(item.id, losing_name)
    assert not claimed and unchanged.album_name == current.album_name


@pytest.mark.asyncio
async def test_restart_preserves_tokens_results_and_restores_failed_control(tmp_path: Path):
    store = PicoStore(tmp_path / "pico.db")
    await store.initialize()
    item, _ = await ready_import(store, tmp_path)
    await store.set_control_message(item.id, 21, 101)
    await store.claim_upload(item.id, "Family")
    await store.save_upload_token(item.files[0].id, "token-1")
    await store.mark_file_uploaded(item.files[0].id, "media-1", "https://photos/item-1")

    preparing = await store.create_import(11, "https://example.com/other")
    staging = tmp_path / "media" / ".staging-interrupted"
    staging.mkdir()
    counts = await store.recover_startup(tmp_path / "media")

    recovered = await store.get_import(item.id)
    abandoned = await store.get_import(preparing.id)
    persistent = await store.persistent_imports()
    assert counts == (1, 1)
    assert recovered.state == "failed" and recovered.album_name == "Family"
    assert recovered.files[0].upload_token == "token-1"
    assert recovered.files[0].media_item_id == "media-1"
    assert abandoned.state == "expired"
    assert [entry.id for entry in persistent] == [item.id]
    assert not staging.exists()


@pytest.mark.asyncio
async def test_completion_is_idempotent_and_expiry_deletes_local_bytes(tmp_path: Path):
    store = PicoStore(tmp_path / "pico.db")
    await store.initialize()
    complete_item, complete_media = await ready_import(store, tmp_path, count=1)
    await store.claim_upload(complete_item.id, "Family")
    await store.mark_file_uploaded(complete_item.files[0].id, "media-1", None)
    assert await store.complete_import_if_ready(complete_item.id)
    assert not await store.complete_import_if_ready(complete_item.id)
    await store.delete_local_bytes(complete_item.id)
    assert not complete_media.exists()

    expiring, expiring_media = await ready_import(store, tmp_path, count=1)
    expired = await store.expire_old_imports(datetime.now(UTC) + timedelta(days=8))
    assert [item.id for item in expired] == [expiring.id]
    assert (await store.get_import(expiring.id)).state == "expired"
    assert not expiring_media.exists()
