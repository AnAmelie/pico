from pathlib import Path

import pytest

from pico_photo_bot.models import PreparedFile, PreparedImport, SourceImage, SourcePost
from pico_photo_bot.photos import (
    PHOTOS_SCOPES,
    GooglePhotosClient,
    PhotosApiError,
)
from pico_photo_bot.store import PicoStore
ARCHIVE_TAG = "picoarc0123456789ab"




class Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self.payload = payload
        self.text = text

    def json(self):
        return self.payload


async def ready_import(store: PicoStore, root: Path):
    record = await store.create_import(11, "https://example.com/image")
    media = root / "media" / str(record.id)
    media.mkdir(parents=True)
    files = []
    images = []
    for ordinal in (1, 2):
        path = media / f"{ARCHIVE_TAG}-example.com-{ordinal:02d}.png"
        path.write_bytes(f"file-{ordinal}".encode())
        files.append(PreparedFile(ordinal, path, path.name, "image/png", path.stat().st_size))
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
    await store.claim_upload(record.id, "Public Fixture Album")
    return ready


class ReconciliationSession:
    def __init__(self, duplicate=False):
        self.duplicate = duplicate
        self.pages = []

    def get(self, url, **kwargs):
        if "/albums/" in url:
            return Response(200, {"id": "target", "title": "Public Fixture Album", "productUrl": "https://photos/target"})
        token = kwargs["params"].get("pageToken")
        self.pages.append(token)
        if token is None:
            return Response(200, {"albums": [{"id": "other", "title": "Other", "productUrl": "https://photos/other"}], "nextPageToken": "next"})
        albums = [{"id": "target", "title": "Public Fixture Album", "productUrl": "https://photos/target"}]
        if self.duplicate:
            albums.append({"id": "duplicate", "title": "Public Fixture Album", "productUrl": "https://photos/duplicate"})
        return Response(200, {"albums": albums})


@pytest.mark.asyncio
async def test_scopes_and_app_created_album_reconciliation(tmp_path: Path):
    assert PHOTOS_SCOPES == [
        "https://www.googleapis.com/auth/photoslibrary.appendonly",
        "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
    ]
    store = PicoStore(tmp_path / "pico.db")
    await store.initialize()
    session = ReconciliationSession()
    client = GooglePhotosClient(Path("unused"), Path("unused"), store, session=session)

    album = await client.get_or_create_album("Public Fixture Album")
    same = await client.get_or_create_album("Public Fixture Album")

    assert album == same
    assert session.pages == [None, "next"]
    assert (await store.get_album("Public Fixture Album")).id == "target"


@pytest.mark.asyncio
async def test_duplicate_exact_album_titles_require_reconciliation(tmp_path: Path):
    store = PicoStore(tmp_path / "pico.db")
    await store.initialize()
    client = GooglePhotosClient(Path("unused"), Path("unused"), store, session=ReconciliationSession(duplicate=True))
    with pytest.raises(PhotosApiError, match="multiple Pico-created albums"):
        await client.get_or_create_album("Public Fixture Album")


class UploadSession:
    def __init__(self, *, retry=False):
        self.retry = retry
        self.upload_headers = []
        self.batch_bodies = []

    def get(self, url, **kwargs):
        if "/albums/" in url:
            return Response(200, {"id": "album", "title": "Public Fixture Album", "productUrl": "https://photos/album"})
        return Response(200, {"albums": []})

    def post(self, url, **kwargs):
        if url.endswith("/albums"):
            return Response(201, {"id": "album", "title": "Public Fixture Album", "productUrl": "https://photos/album"})
        if url.endswith("/uploads"):
            self.upload_headers.append(kwargs["headers"])
            return Response(200, text=f"token-{len(self.upload_headers)}")
        body = kwargs["json"]
        self.batch_bodies.append(body)
        if self.retry:
            return Response(200, {"newMediaItemResults": [{"uploadToken": "token-2", "mediaItem": {"id": "media-2", "productUrl": "https://photos/media-2"}}]})
        return Response(200, {"newMediaItemResults": [
            {"uploadToken": "token-1", "mediaItem": {"id": "media-1", "productUrl": "https://photos/media-1"}},
            {"uploadToken": "token-2", "status": {"code": 503}},
        ]})


@pytest.mark.asyncio
async def test_raw_upload_partial_result_retries_persisted_token_only(tmp_path: Path):
    store = PicoStore(tmp_path / "pico.db")
    await store.initialize()
    item = await ready_import(store, tmp_path)
    first_session = UploadSession()
    first_client = GooglePhotosClient(Path("unused"), Path("unused"), store, session=first_session)

    first = await first_client.upload_import(item.id, "Public Fixture Album")

    assert (first.completed_count, first.remaining_count) == (1, 1)
    assert len(first_session.upload_headers) == 2
    assert all(headers["X-Goog-Upload-Protocol"] == "raw" for headers in first_session.upload_headers)
    assert all(headers["Content-Type"] == "application/octet-stream" for headers in first_session.upload_headers)
    assert {entry["description"] for entry in first_session.batch_bodies[0]["newMediaItems"]} == {
        "Source: example.com — https://example.com/image"
    }
    assert [
        entry["simpleMediaItem"]["fileName"]
        for entry in first_session.batch_bodies[0]["newMediaItems"]
    ] == [
        f"{ARCHIVE_TAG}-example.com-01.png",
        f"{ARCHIVE_TAG}-example.com-02.png",
    ]

    claimed, retried = await store.claim_upload(item.id, None)
    assert claimed and retried.album_name == "Public Fixture Album"
    retry_session = UploadSession(retry=True)
    retry_client = GooglePhotosClient(Path("unused"), Path("unused"), store, session=retry_session)
    complete = await retry_client.upload_import(item.id, "Public Fixture Album")

    assert complete.complete
    assert retry_session.upload_headers == []
    assert [entry["simpleMediaItem"]["uploadToken"] for entry in retry_session.batch_bodies[0]["newMediaItems"]] == ["token-2"]
    assert [
        entry["simpleMediaItem"]["fileName"]
        for entry in retry_session.batch_bodies[0]["newMediaItems"]
    ] == [f"{ARCHIVE_TAG}-example.com-02.png"]
    assert not item.files[0].path.parent.exists()
