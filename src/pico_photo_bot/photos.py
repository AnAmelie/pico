from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials

from .oauth import write_private_token
from .models import PhotosAlbum, PhotosUploadResult, PicoFile
from .store import PicoStore

PHOTOS_SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
]
PHOTOS_API = "https://photoslibrary.googleapis.com/v1"
UPLOAD_TOKEN_MAX_AGE = timedelta(hours=23)
UTC = timezone.utc


class PhotosAuthorizationError(RuntimeError):
    pass


class PhotosApiError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class GooglePhotosClient:
    def __init__(
        self,
        credentials_path: Path,
        token_path: Path,
        store: PicoStore,
        *,
        session: object | None = None,
        session_factory: Callable[[Credentials], object] = AuthorizedSession,
    ) -> None:
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self.store = store
        self._owns_session = session is None
        self.session = session or self._build_authenticated_session(session_factory)
        self._batch_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_session:
            await asyncio.to_thread(self.session.close)

    async def get_or_create_album(self, name: str) -> PhotosAlbum:
        mapped = await self.store.get_album(name)
        if mapped is not None:
            response = await asyncio.to_thread(
                self.session.get,
                f"{PHOTOS_API}/albums/{quote(mapped.id, safe='')}",
                timeout=30,
            )
            if response.status_code == 200:
                payload = _json_object(response, "Google Photos album lookup failed.")
                album = _album_from_payload(payload)
                if album.title != name:
                    raise PhotosApiError(
                        f"Stored Google Photos album for {name!r} has a different title; reconcile Pico's album mapping."
                    )
                await self.store.save_album(album)
                return album
            if response.status_code != 404:
                raise _response_error(response, "Google Photos album lookup failed.")

        matches: list[PhotosAlbum] = []
        page_token: str | None = None
        while True:
            params: dict[str, object] = {"pageSize": 50}
            if page_token:
                params["pageToken"] = page_token
            response = await asyncio.to_thread(
                self.session.get,
                f"{PHOTOS_API}/albums",
                params=params,
                timeout=30,
            )
            if response.status_code != 200:
                raise _response_error(response, "Google Photos album listing failed.")
            payload = _json_object(response, "Google Photos album listing failed.")
            albums = payload.get("albums", [])
            if not isinstance(albums, list):
                raise PhotosApiError("Google Photos returned an invalid album list.")
            for raw_album in albums:
                if isinstance(raw_album, dict) and raw_album.get("title") == name:
                    matches.append(_album_from_payload(raw_album))
            page_token = str(payload.get("nextPageToken") or "").strip() or None
            if page_token is None:
                break
        if len(matches) > 1:
            raise PhotosApiError(
                f"Google Photos has multiple Pico-created albums named {name!r}; rename duplicates before retrying."
            )
        if matches:
            album = matches[0]
        else:
            response = await asyncio.to_thread(
                self.session.post,
                f"{PHOTOS_API}/albums",
                json={"album": {"title": name}},
                timeout=30,
            )
            if response.status_code not in {200, 201}:
                raise _response_error(response, "Google Photos album creation failed.")
            album = _album_from_payload(
                _json_object(response, "Google Photos album creation failed.")
            )
            if album.title != name:
                raise PhotosApiError("Google Photos returned an unexpected album title.")
        await self.store.save_album(album)
        return album

    async def upload_import(
        self, import_id: int, album_name: str
    ) -> PhotosUploadResult:
        item = await self.store.get_import(import_id)
        if item is None:
            raise PhotosApiError("Pico import no longer exists.")
        if item.state == "uploaded" and item.album_id and item.album_url:
            album = PhotosAlbum(item.album_id, item.album_name or album_name, item.album_url)
            return PhotosUploadResult(import_id, album, item.uploaded_count, 0)
        if item.state != "uploading" or item.album_name != album_name:
            raise PhotosApiError("Pico import is not locked for this album upload.")
        try:
            album = await self.get_or_create_album(album_name)
            if not await self.store.set_import_album(import_id, album):
                raise PhotosApiError("Pico import changed state before its album was saved.")
            errors: list[str] = []
            unresolved = tuple(file for file in item.files if file.media_item_id is None)
            tokenized: list[PicoFile] = []
            now = datetime.now(UTC)
            for file in unresolved:
                token = file.upload_token
                if (
                    token is None
                    or file.upload_token_at is None
                    or now - file.upload_token_at >= UPLOAD_TOKEN_MAX_AGE
                ):
                    token = await asyncio.to_thread(self._upload_file_bytes, file)
                    if not await self.store.save_upload_token(file.id, token, now):
                        raise PhotosApiError("Pico file changed state before its upload token was saved.")
                    file = PicoFile(
                        id=file.id,
                        import_id=file.import_id,
                        ordinal=file.ordinal,
                        path=file.path,
                        filename=file.filename,
                        mime_type=file.mime_type,
                        size=file.size,
                        state="tokenized",
                        upload_token=token,
                        upload_token_at=now,
                        media_item_id=file.media_item_id,
                        media_item_url=file.media_item_url,
                        last_error=None,
                    )
                tokenized.append(file)
            for offset in range(0, len(tokenized), 50):
                group = tokenized[offset : offset + 50]
                group_errors = await self._batch_create(album, item.attribution_sentence, group)
                errors.extend(group_errors)
            complete = await self.store.complete_import_if_ready(import_id)
            refreshed = await self.store.get_import(import_id)
            assert refreshed is not None
            if complete:
                await self.store.delete_local_bytes(import_id)
            else:
                await self.store.mark_upload_failed(
                    import_id,
                    errors[0] if errors else "Some Google Photos items were not created; retry the import.",
                )
                refreshed = await self.store.get_import(import_id)
                assert refreshed is not None
            return PhotosUploadResult(
                import_id,
                album,
                refreshed.uploaded_count,
                refreshed.remaining_count,
                tuple(errors),
            )
        except PhotosApiError as exc:
            await self.store.mark_upload_failed(import_id, str(exc))
            raise
        except Exception as exc:
            error = PhotosApiError(
                "Google Photos request failed; retry the existing Pico upload.",
                retryable=True,
            )
            await self.store.mark_upload_failed(import_id, str(error))
            raise error from exc

    def _upload_file_bytes(self, file: PicoFile) -> str:
        try:
            with file.path.open("rb") as source:
                response = self.session.post(
                    f"{PHOTOS_API}/uploads",
                    data=source,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-Goog-Upload-Content-Type": file.mime_type,
                        "X-Goog-Upload-File-Name": file.filename,
                        "X-Goog-Upload-Protocol": "raw",
                    },
                    timeout=30,
                )
        except OSError as exc:
            raise PhotosApiError(f"Prepared file {file.filename} is unavailable.") from exc
        if response.status_code != 200:
            raise _response_error(response, "Google Photos byte upload failed.")
        token = response.text.strip()
        if not token:
            raise PhotosApiError("Google Photos returned an empty upload token.")
        return token

    async def _batch_create(
        self,
        album: PhotosAlbum,
        description: str,
        files: list[PicoFile],
    ) -> list[str]:
        body = {
            "albumId": album.id,
            "newMediaItems": [
                {
                    "description": description,
                    "simpleMediaItem": {
                        "uploadToken": file.upload_token,
                        "fileName": file.filename,
                    },
                }
                for file in files
            ],
        }
        async with self._batch_lock:
            response = await asyncio.to_thread(
                self.session.post,
                f"{PHOTOS_API}/mediaItems:batchCreate",
                json=body,
                timeout=30,
            )
        if response.status_code != 200:
            raise _response_error(response, "Google Photos media creation failed.")
        payload = _json_object(response, "Google Photos media creation failed.")
        results = payload.get("newMediaItemResults")
        if not isinstance(results, list):
            raise PhotosApiError("Google Photos returned an invalid media creation result.")
        by_token = {file.upload_token: file for file in files}
        seen: set[str] = set()
        errors: list[str] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            token = str(result.get("uploadToken") or "")
            file = by_token.get(token)
            if file is None:
                continue
            seen.add(token)
            media_item = result.get("mediaItem")
            status = result.get("status") or {}
            if isinstance(media_item, dict) and media_item.get("id"):
                await self.store.mark_file_uploaded(
                    file.id,
                    str(media_item["id"]),
                    str(media_item.get("productUrl") or "") or None,
                )
                continue
            code = int(status.get("code") or 0) if isinstance(status, dict) else 0
            message = "Google Photos rejected one prepared image."
            if code == 429 or code >= 500:
                message = "Google Photos temporarily rejected one image; retry the import."
            await self.store.mark_file_failed(file.id, message)
            errors.append(message)
        for token, file in by_token.items():
            if token in seen:
                continue
            message = "Google Photos omitted one image result; retry the import."
            await self.store.mark_file_failed(file.id, message)
            errors.append(message)
        return errors

    def _build_authenticated_session(
        self, session_factory: Callable[[Credentials], object]
    ) -> object:
        if not self.credentials_path.is_file():
            raise PhotosAuthorizationError(
                f"Google OAuth credentials are missing at {self.credentials_path}; "
                "download a desktop OAuth client and run pico-photo-bot-auth"
            )
        if not self.token_path.is_file():
            raise PhotosAuthorizationError(
                f"Google Photos token is missing at {self.token_path}; run pico-photo-bot-auth"
            )
        try:
            client_config = json.loads(self.credentials_path.read_text())
            if not isinstance(client_config.get("installed"), dict):
                raise ValueError("expected an installed desktop OAuth client")
            credentials = Credentials.from_authorized_user_file(
                str(self.token_path), PHOTOS_SCOPES
            )
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                write_private_token(self.token_path, credentials.to_json())
            if not credentials.valid:
                raise ValueError("cached credentials are invalid or cannot be refreshed")
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise PhotosAuthorizationError(
                "Google Photos OAuth credentials are unusable; run pico-photo-bot-auth"
            ) from exc
        return session_factory(credentials)


def _album_from_payload(payload: dict[str, Any]) -> PhotosAlbum:
    album_id = str(payload.get("id") or "").strip()
    title = str(payload.get("title") or "").strip()
    product_url = str(payload.get("productUrl") or "").strip()
    if not album_id or not title or not product_url:
        raise PhotosApiError("Google Photos returned incomplete album information.")
    return PhotosAlbum(album_id, title, product_url)


def _json_object(response: object, message: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise PhotosApiError(message) from exc
    if not isinstance(payload, dict):
        raise PhotosApiError(message)
    return payload


def _response_error(response: object, message: str) -> PhotosApiError:
    status = int(getattr(response, "status_code", 0) or 0)
    return PhotosApiError(message, retryable=status == 429 or status >= 500)
