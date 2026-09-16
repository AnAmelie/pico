import asyncio
import sqlite3
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


@pytest.mark.asyncio
async def test_legacy_database_migrates_without_data_or_foreign_key_loss(
    tmp_path: Path,
):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE pico_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submitter_id INTEGER NOT NULL,
                source_kind TEXT NOT NULL CHECK(source_kind IN ('reddit', 'direct')),
                source_url TEXT NOT NULL,
                attribution_label TEXT NOT NULL,
                attribution_sentence TEXT NOT NULL,
                state TEXT NOT NULL,
                album_name TEXT,
                album_id TEXT,
                album_url TEXT,
                control_channel_id INTEGER,
                control_message_id INTEGER,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX pico_imports_state_idx ON pico_imports(state, updated_at);
            CREATE TABLE pico_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER NOT NULL REFERENCES pico_imports(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                path TEXT NOT NULL,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                state TEXT NOT NULL,
                upload_token TEXT,
                upload_token_at TEXT,
                media_item_id TEXT,
                media_item_url TEXT,
                last_error TEXT,
                UNIQUE(import_id, ordinal),
                UNIQUE(import_id, filename)
            );
            CREATE TABLE pico_albums (
                name TEXT PRIMARY KEY,
                album_id TEXT NOT NULL,
                product_url TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO pico_imports VALUES (
                7, 11, 'direct', 'https://example.com/image', 'example.com',
                'Source: example.com', 'ready', NULL, NULL, NULL, 21, 101,
                NULL, '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00'
            );
            INSERT INTO pico_files VALUES (
                9, 7, 1, '/tmp/image.jpg', 'image.jpg', 'image/jpeg', 10,
                'pending', NULL, NULL, NULL, NULL, NULL
            );
            """
        )

    store = PicoStore(path)
    await store.initialize()

    item = await store.get_import(7)
    assert item is not None and item.files[0].id == 9
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        db.execute(
            """
            INSERT INTO pico_imports(
                submitter_id, source_kind, source_url, attribution_label,
                attribution_sentence, state, created_at, updated_at
            ) VALUES (11, 'webpage', 'https://example.com/story', 'credit',
                      'Photo credit: credit', 'ready', 'now', 'now')
            """
        )


@pytest.mark.asyncio
async def test_webpage_children_lock_and_delete_bytes_independently(tmp_path: Path):
    store = PicoStore(tmp_path / "pico.db")
    await store.initialize()
    original = await store.create_import(11, "https://publisher.example/story")
    media = tmp_path / "media" / "batch"
    media.mkdir(parents=True)
    files = []
    images = []
    for ordinal, credit in ((1, "Credit One"), (2, "Credit Two")):
        path = media / f"{ordinal}.jpg"
        path.write_bytes(bytes([ordinal]))
        files.append(PreparedFile(ordinal, path, path.name, "image/jpeg", 1))
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
    prepared = PreparedImport(
        "batch",
        SourcePost(
            "webpage",
            "https://publisher.example/story",
            "publisher.example",
            "Source: publisher.example — https://publisher.example/story",
            tuple(images),
        ),
        tuple(files),
    )

    children = await store.mark_webpage_ready(original.id, prepared)

    assert len(children) == 2
    assert all(len(child.files) == 1 for child in children)
    assert (await store.claim_upload(children[0].id, "Family"))[0]
    assert (await store.claim_upload(children[1].id, "Reference"))[0]
    await store.delete_local_bytes(children[0].id)
    assert not files[0].path.exists()
    assert files[1].path.exists()
    assert media.exists()

    await store.mark_upload_failed(children[1].id, "retry")
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE pico_imports SET created_at = ? WHERE id = ?",
            (
                (datetime.now(UTC) - timedelta(days=8)).isoformat(),
                children[1].id,
            ),
        )
    expired = await store.expire_old_imports()
    assert [item.id for item in expired] == [children[1].id]
    assert not files[1].path.exists()
    assert not media.exists()
