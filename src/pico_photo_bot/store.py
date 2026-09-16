from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from .models import (
    PhotosAlbum,
    PicoFile,
    PicoImport,
    PreparedImport,
)

UTC = timezone.utc
SCHEMA_VERSION = 1
_CREATE_SCHEMA = """
CREATE TABLE pico_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter_id INTEGER NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('reddit', 'direct', 'webpage')),
    source_url TEXT NOT NULL,
    attribution_label TEXT NOT NULL,
    attribution_sentence TEXT NOT NULL,
    state TEXT NOT NULL CHECK(
        state IN ('preparing', 'ready', 'uploading', 'failed', 'uploaded', 'expired')
    ),
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
    state TEXT NOT NULL CHECK(
        state IN ('pending', 'tokenized', 'failed', 'uploaded')
    ),
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
"""


class PicoStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout = 5000")
        await db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
        finally:
            await db.close()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            await db.execute("PRAGMA journal_mode = WAL")
            table = await (
                await db.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'pico_imports'"
                )
            ).fetchone()
            version_row = await (await db.execute("PRAGMA user_version")).fetchone()
            version = int(version_row[0])
            if table is None:
                await db.executescript(_CREATE_SCHEMA)
                await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                await db.commit()
            elif version == 0:
                await self._migrate_legacy_schema(db)
            elif version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported Pico database schema version {version}; "
                    f"expected {SCHEMA_VERSION}."
                )

    async def _migrate_legacy_schema(self, db: aiosqlite.Connection) -> None:
        await db.execute("PRAGMA foreign_keys = OFF")
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                CREATE TABLE pico_imports_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submitter_id INTEGER NOT NULL,
                    source_kind TEXT NOT NULL CHECK(
                        source_kind IN ('reddit', 'direct', 'webpage')
                    ),
                    source_url TEXT NOT NULL,
                    attribution_label TEXT NOT NULL,
                    attribution_sentence TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(
                        state IN (
                            'preparing', 'ready', 'uploading',
                            'failed', 'uploaded', 'expired'
                        )
                    ),
                    album_name TEXT,
                    album_id TEXT,
                    album_url TEXT,
                    control_channel_id INTEGER,
                    control_message_id INTEGER,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                INSERT INTO pico_imports_new(
                    id, submitter_id, source_kind, source_url,
                    attribution_label, attribution_sentence, state,
                    album_name, album_id, album_url, control_channel_id,
                    control_message_id, last_error, created_at, updated_at
                )
                SELECT
                    id, submitter_id, source_kind, source_url,
                    attribution_label, attribution_sentence, state,
                    album_name, album_id, album_url, control_channel_id,
                    control_message_id, last_error, created_at, updated_at
                FROM pico_imports
                """
            )
            await db.execute("DROP INDEX IF EXISTS pico_imports_state_idx")
            await db.execute("DROP TABLE pico_imports")
            await db.execute("ALTER TABLE pico_imports_new RENAME TO pico_imports")
            await db.execute(
                "CREATE INDEX pico_imports_state_idx "
                "ON pico_imports(state, updated_at)"
            )
            await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.execute("PRAGMA foreign_keys = ON")
        problems = await (await db.execute("PRAGMA foreign_key_check")).fetchall()
        if problems:
            raise RuntimeError("Pico database migration failed its foreign-key check.")

    async def create_import(self, submitter_id: int, source_url: str) -> PicoImport:
        now = _now_text()
        async with self._connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO pico_imports(
                    submitter_id, source_kind, source_url, attribution_label,
                    attribution_sentence, state, created_at, updated_at
                ) VALUES (?, 'direct', ?, '', '', 'preparing', ?, ?)
                """,
                (submitter_id, source_url.strip(), now, now),
            )
            await db.commit()
            import_id = int(cursor.lastrowid)
        result = await self.get_import(import_id)
        assert result is not None
        return result

    async def mark_ready(
        self, import_id: int, prepared: PreparedImport
    ) -> PicoImport | None:
        now = _now_text()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE pico_imports
                SET source_kind = ?, source_url = ?, attribution_label = ?,
                    attribution_sentence = ?, state = 'ready', last_error = NULL,
                    updated_at = ?
                WHERE id = ? AND state = 'preparing'
                """,
                (
                    prepared.source.kind,
                    prepared.source.canonical_url,
                    prepared.source.attribution_label,
                    prepared.source.attribution_sentence,
                    now,
                    import_id,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            await db.executemany(
                """
                INSERT INTO pico_files(
                    import_id, ordinal, path, filename, mime_type, size, state
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    (
                        import_id,
                        item.ordinal,
                        str(item.path),
                        item.filename,
                        item.mime_type,
                        item.size,
                    )
                    for item in prepared.files
                ),
            )
            await db.commit()
        return await self.get_import(import_id)
    async def mark_webpage_ready(
        self, import_id: int, prepared: PreparedImport
    ) -> tuple[PicoImport, ...]:
        if prepared.source.kind != "webpage":
            raise ValueError("Prepared source is not a webpage.")
        files_by_ordinal = {item.ordinal: item for item in prepared.files}
        images_by_ordinal = {item.ordinal: item for item in prepared.source.images}
        ordinals = [item.ordinal for item in prepared.source.images]
        if (
            not ordinals
            or len(files_by_ordinal) != len(prepared.files)
            or len(images_by_ordinal) != len(prepared.source.images)
            or set(files_by_ordinal) != set(images_by_ordinal)
            or any(
                not image.attribution_label or not image.attribution_sentence
                for image in prepared.source.images
            )
        ):
            raise ValueError(
                "Each webpage photo must map to one uniquely attributed prepared file."
            )
        now = _now_text()
        child_ids: list[int] = []
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            original = await (
                await db.execute(
                    "SELECT * FROM pico_imports WHERE id = ? AND state = 'preparing'",
                    (import_id,),
                )
            ).fetchone()
            if original is None:
                await db.rollback()
                return ()
            try:
                for index, ordinal in enumerate(ordinals):
                    image = images_by_ordinal[ordinal]
                    file = files_by_ordinal[ordinal]
                    if index == 0:
                        cursor = await db.execute(
                            """
                            UPDATE pico_imports
                            SET source_kind = 'webpage', source_url = ?,
                                attribution_label = ?, attribution_sentence = ?,
                                state = 'ready', last_error = NULL, updated_at = ?
                            WHERE id = ? AND state = 'preparing'
                            """,
                            (
                                prepared.source.canonical_url,
                                image.attribution_label,
                                image.attribution_sentence,
                                now,
                                import_id,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError("Original webpage import changed during preparation.")
                        child_id = import_id
                    else:
                        cursor = await db.execute(
                            """
                            INSERT INTO pico_imports(
                                submitter_id, source_kind, source_url,
                                attribution_label, attribution_sentence, state,
                                created_at, updated_at
                            ) VALUES (?, 'webpage', ?, ?, ?, 'ready', ?, ?)
                            """,
                            (
                                original["submitter_id"],
                                prepared.source.canonical_url,
                                image.attribution_label,
                                image.attribution_sentence,
                                original["created_at"],
                                now,
                            ),
                        )
                        child_id = int(cursor.lastrowid)
                    await db.execute(
                        """
                        INSERT INTO pico_files(
                            import_id, ordinal, path, filename, mime_type, size, state
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                        """,
                        (
                            child_id,
                            file.ordinal,
                            str(file.path),
                            file.filename,
                            file.mime_type,
                            file.size,
                        ),
                    )
                    child_ids.append(child_id)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        children = []
        for child_id in child_ids:
            child = await self.get_import(child_id)
            assert child is not None
            children.append(child)
        return tuple(children)


    async def mark_preparation_failed(self, import_id: int, error: str) -> bool:
        return await self._transition(
            import_id, ("preparing",), "failed", error=_bounded_error(error)
        )

    async def mark_delivery_failed(self, import_id: int, error: str) -> bool:
        return await self._transition(
            import_id, ("ready",), "failed", error=_bounded_error(error)
        )

    async def set_control_message(
        self, import_id: int, channel_id: int, message_id: int
    ) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE pico_imports
                SET control_channel_id = ?, control_message_id = ?, updated_at = ?
                WHERE id = ? AND state IN ('ready', 'failed', 'uploading', 'uploaded')
                """,
                (channel_id, message_id, _now_text(), import_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def get_import(self, import_id: int) -> PicoImport | None:
        async with self._connect() as db:
            row = await (
                await db.execute("SELECT * FROM pico_imports WHERE id = ?", (import_id,))
            ).fetchone()
            if row is None:
                return None
            file_rows = await (
                await db.execute(
                    "SELECT * FROM pico_files WHERE import_id = ? ORDER BY ordinal",
                    (import_id,),
                )
            ).fetchall()
        return _import_from_rows(row, file_rows)

    async def persistent_imports(self) -> tuple[PicoImport, ...]:
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT * FROM pico_imports
                    WHERE state IN ('ready', 'failed')
                      AND control_channel_id IS NOT NULL
                      AND control_message_id IS NOT NULL
                    ORDER BY id
                    """
                )
            ).fetchall()
            results = []
            for row in rows:
                file_rows = await (
                    await db.execute(
                        "SELECT * FROM pico_files WHERE import_id = ? ORDER BY ordinal",
                        (row["id"],),
                    )
                ).fetchall()
                results.append(_import_from_rows(row, file_rows))
        return tuple(results)

    async def claim_upload(
        self, import_id: int, album_name: str | None
    ) -> tuple[bool, PicoImport | None]:
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute("SELECT * FROM pico_imports WHERE id = ?", (import_id,))
            ).fetchone()
            if row is None:
                await db.rollback()
                return False, None
            locked_name = row["album_name"]
            if locked_name is None:
                if not album_name or not album_name.strip():
                    await db.rollback()
                    return False, await self.get_import(import_id)
                locked_name = album_name.strip()
            elif album_name is not None and album_name.strip() != locked_name:
                await db.rollback()
                return False, await self.get_import(import_id)
            cursor = await db.execute(
                """
                UPDATE pico_imports
                SET state = 'uploading', album_name = ?, last_error = NULL, updated_at = ?
                WHERE id = ? AND state IN ('ready', 'failed')
                """,
                (locked_name, _now_text(), import_id),
            )
            await db.commit()
        return cursor.rowcount == 1, await self.get_import(import_id)

    async def set_import_album(self, import_id: int, album: PhotosAlbum) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE pico_imports
                SET album_id = ?, album_url = ?, updated_at = ?
                WHERE id = ? AND state = 'uploading' AND album_name = ?
                """,
                (album.id, album.product_url, _now_text(), import_id, album.title),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def get_album(self, name: str) -> PhotosAlbum | None:
        async with self._connect() as db:
            row = await (
                await db.execute("SELECT * FROM pico_albums WHERE name = ?", (name,))
            ).fetchone()
        if row is None:
            return None
        return PhotosAlbum(str(row["album_id"]), str(row["name"]), str(row["product_url"]))

    async def save_album(self, album: PhotosAlbum) -> None:
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO pico_albums(name, album_id, product_url, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    album_id = excluded.album_id,
                    product_url = excluded.product_url,
                    updated_at = excluded.updated_at
                """,
                (album.title, album.id, album.product_url, _now_text()),
            )
            await db.commit()

    async def save_upload_token(
        self, file_id: int, token: str, created_at: datetime | None = None
    ) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE pico_files
                SET upload_token = ?, upload_token_at = ?, state = 'tokenized',
                    last_error = NULL
                WHERE id = ? AND media_item_id IS NULL
                """,
                (token, (created_at or datetime.now(UTC)).isoformat(), file_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def mark_file_uploaded(
        self, file_id: int, media_item_id: str, media_item_url: str | None
    ) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE pico_files
                SET state = 'uploaded', media_item_id = ?, media_item_url = ?, last_error = NULL
                WHERE id = ? AND media_item_id IS NULL
                """,
                (media_item_id, media_item_url, file_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def mark_file_failed(self, file_id: int, error: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE pico_files SET state = 'failed', last_error = ?
                WHERE id = ? AND media_item_id IS NULL
                """,
                (_bounded_error(error), file_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def mark_upload_failed(self, import_id: int, error: str) -> bool:
        return await self._transition(
            import_id, ("uploading",), "failed", error=_bounded_error(error)
        )

    async def complete_import_if_ready(self, import_id: int) -> bool:
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            unresolved = await (
                await db.execute(
                    """
                    SELECT COUNT(*) AS count FROM pico_files
                    WHERE import_id = ? AND media_item_id IS NULL
                    """,
                    (import_id,),
                )
            ).fetchone()
            assert unresolved is not None
            if int(unresolved["count"]) != 0:
                await db.rollback()
                return False
            cursor = await db.execute(
                """
                UPDATE pico_imports
                SET state = 'uploaded', last_error = NULL, updated_at = ?
                WHERE id = ? AND state = 'uploading'
                """,
                (_now_text(), import_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def recover_startup(self, media_path: Path) -> tuple[int, int]:
        now = _now_text()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            preparing = await db.execute(
                """
                UPDATE pico_imports
                SET state = 'expired', last_error = 'Preparation was interrupted; paste the URL again.',
                    updated_at = ?
                WHERE state = 'preparing'
                """,
                (now,),
            )
            uploading = await db.execute(
                """
                UPDATE pico_imports
                SET state = 'failed', last_error = 'Upload was interrupted; press Add to Google Photos to retry.',
                    updated_at = ?
                WHERE state = 'uploading'
                """,
                (now,),
            )
            await db.commit()
        root = Path(media_path)
        if root.exists():
            staging = tuple(
                item for item in root.iterdir() if item.name.startswith(".staging-")
            )
            await asyncio.gather(
                *(asyncio.to_thread(shutil.rmtree, item, True) for item in staging)
            )
        return preparing.rowcount, uploading.rowcount

    async def expire_old_imports(
        self, now: datetime | None = None
    ) -> tuple[PicoImport, ...]:
        cutoff = (now or datetime.now(UTC)) - timedelta(days=7)
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await (
                await db.execute(
                    """
                    SELECT * FROM pico_imports
                    WHERE state IN ('ready', 'failed') AND created_at < ?
                    ORDER BY id
                    """,
                    (cutoff.isoformat(),),
                )
            ).fetchall()
            if rows:
                await db.executemany(
                    """
                    UPDATE pico_imports
                    SET state = 'expired', last_error = 'Expired; paste the URL again.', updated_at = ?
                    WHERE id = ? AND state IN ('ready', 'failed')
                    """,
                    ((_now_text(), row["id"]) for row in rows),
                )
            results = []
            for row in rows:
                file_rows = await (
                    await db.execute(
                        "SELECT * FROM pico_files WHERE import_id = ? ORDER BY ordinal",
                        (row["id"],),
                    )
                ).fetchall()
                results.append(
                    _import_from_rows(
                        row, file_rows, state="expired", last_error="Expired; paste the URL again."
                    )
                )
            await db.commit()
        for item in results:
            await _delete_import_bytes(item)
        return tuple(results)

    async def delete_local_bytes(self, import_id: int) -> None:
        item = await self.get_import(import_id)
        if item is not None:
            await _delete_import_bytes(item)

    async def _transition(
        self,
        import_id: int,
        old_states: tuple[str, ...],
        new_state: str,
        *,
        error: str | None,
    ) -> bool:
        placeholders = ",".join("?" for _ in old_states)
        async with self._connect() as db:
            cursor = await db.execute(
                f"""
                UPDATE pico_imports SET state = ?, last_error = ?, updated_at = ?
                WHERE id = ? AND state IN ({placeholders})
                """,
                (new_state, error, _now_text(), import_id, *old_states),
            )
            await db.commit()
            return cursor.rowcount == 1


def _file_from_row(row: aiosqlite.Row) -> PicoFile:
    return PicoFile(
        id=int(row["id"]),
        import_id=int(row["import_id"]),
        ordinal=int(row["ordinal"]),
        path=Path(str(row["path"])),
        filename=str(row["filename"]),
        mime_type=str(row["mime_type"]),
        size=int(row["size"]),
        state=row["state"],
        upload_token=row["upload_token"],
        upload_token_at=_datetime(row["upload_token_at"]),
        media_item_id=row["media_item_id"],
        media_item_url=row["media_item_url"],
        last_error=row["last_error"],
    )


def _import_from_rows(
    row: aiosqlite.Row,
    file_rows: list[aiosqlite.Row],
    *,
    state: str | None = None,
    last_error: str | None = None,
) -> PicoImport:
    return PicoImport(
        id=int(row["id"]),
        submitter_id=int(row["submitter_id"]),
        source_kind=row["source_kind"],
        source_url=str(row["source_url"]),
        attribution_label=str(row["attribution_label"]),
        attribution_sentence=str(row["attribution_sentence"]),
        state=state or row["state"],
        album_name=row["album_name"],
        album_id=row["album_id"],
        album_url=row["album_url"],
        control_channel_id=row["control_channel_id"],
        control_message_id=row["control_message_id"],
        last_error=last_error if last_error is not None else row["last_error"],
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        files=tuple(_file_from_row(file_row) for file_row in file_rows),
    )


def _datetime(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _bounded_error(error: str) -> str:
    return " ".join(error.split())[:1_000] or "Operation failed"


def _now_text() -> str:
    return datetime.now(UTC).isoformat()


async def _delete_import_bytes(item: PicoImport) -> None:
    parents = {file.path.parent for file in item.files}
    for file in item.files:
        await asyncio.to_thread(file.path.unlink, missing_ok=True)
    for parent in parents:
        await asyncio.to_thread(_remove_empty_directory, parent)


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except (FileNotFoundError, OSError):
        pass
