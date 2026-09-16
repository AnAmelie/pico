from __future__ import annotations

import asyncio
from dataclasses import dataclass
import html
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from .attribution import PhotographerResearcher, resolve_attributions
from .models import PreparedFile, PreparedImport, SourceImage, SourcePost
from .public_url import (
    PublicUrlError,
    Resolver,
    normalize_public_http_url,
    resolve_host,
    validate_public_http_url,
)
from .webpage import WebpageError, WebpageRenderer

MAX_REDIRECTS = 5
MAX_IMAGE_BYTES = 10_000_000
MAX_IMAGE_PIXELS = 50_000_000
MAX_IMPORT_IMAGES = 20
MAX_IMPORT_BYTES = 100_000_000
_REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "redd.it"}
_COMMENTS_RE = re.compile(r"/(?:r/[^/]+/)?comments/([a-z0-9]+)(?:/|$)", re.IGNORECASE)


class PicoSourceError(ValueError):
    pass


class MetadataWriter(Protocol):
    def write(
        self,
        source: Path,
        destination: Path,
        attribution: str,
        source_url: str,
        archive_search_tag: str,
    ) -> None: ...


def is_reddit_post_url(url: str) -> bool:
    try:
        parsed = urlsplit(normalize_public_http_url(url))
    except PublicUrlError:
        return False
    host = (parsed.hostname or "").casefold()
    if host not in _REDDIT_HOSTS:
        return False
    return bool(
        host == "redd.it"
        or _COMMENTS_RE.search(parsed.path)
        or "/s/" in parsed.path.casefold()
    )


class RedditPostClient:
    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        user_agent: str | None,
        client: Optional[httpx.AsyncClient] = None,
        resolver: Optional[Resolver] = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
        )
        self.resolver = resolver or resolve_host
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def resolve(self, url: str) -> SourcePost:
        if not self.client_id or not self.client_secret or not self.user_agent:
            raise PicoSourceError(
                "Reddit links are unavailable until Pico's Reddit API credentials are configured."
            )
        post_id = await self._resolve_post_id(url)
        token = await self._get_access_token()
        try:
            response = await self.client.get(
                f"https://oauth.reddit.com/comments/{post_id}",
                params={"raw_json": "1", "limit": "1"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": self.user_agent,
                },
            )
            response.raise_for_status()
            payload = response.json()
            post = payload[0]["data"]["children"][0]["data"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise PicoSourceError("Reddit post could not be loaded; check the link and try again.") from exc
        return self._parse_post(post_id, post)

    async def _resolve_post_id(self, url: str) -> str:
        try:
            current = normalize_public_http_url(url)
        except PublicUrlError as exc:
            raise PicoSourceError(str(exc)) from exc
        for redirect_count in range(MAX_REDIRECTS + 1):
            try:
                await validate_public_http_url(current, self.resolver)
            except PublicUrlError as exc:
                raise PicoSourceError(str(exc)) from exc
            parsed = urlsplit(current)
            host = (parsed.hostname or "").casefold()
            if host not in _REDDIT_HOSTS:
                raise PicoSourceError("Only Reddit post links or direct image links are supported.")
            match = _COMMENTS_RE.search(parsed.path)
            if match:
                return match.group(1).casefold()
            if host == "redd.it":
                post_id = parsed.path.strip("/").split("/", 1)[0]
                if post_id and re.fullmatch(r"[A-Za-z0-9]+", post_id):
                    return post_id.casefold()
            if "/s/" not in parsed.path.casefold():
                raise PicoSourceError("Reddit link must point to a post.")
            try:
                response = await self.client.get(
                    current,
                    headers={"User-Agent": self.user_agent},
                    follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                raise PicoSourceError("Reddit share link could not be resolved.") from exc
            if response.status_code not in {301, 302, 303, 307, 308}:
                raise PicoSourceError("Reddit share link did not resolve to a post.")
            location = response.headers.get("location")
            if not location:
                raise PicoSourceError("Reddit share link redirect omitted its destination.")
            if redirect_count >= MAX_REDIRECTS:
                raise PicoSourceError("Reddit share link exceeded 5 redirects.")
            try:
                current = normalize_public_http_url(urljoin(current, location))
            except PublicUrlError as exc:
                raise PicoSourceError(str(exc)) from exc
        raise PicoSourceError("Reddit share link exceeded 5 redirects.")

    async def _get_access_token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expires_at - 60:
            return self._access_token
        async with self._token_lock:
            if self._access_token and time.monotonic() < self._token_expires_at - 60:
                return self._access_token
            try:
                response = await self.client.post(
                    "https://www.reddit.com/api/v1/access_token",
                    data={"grant_type": "client_credentials"},
                    auth=(self.client_id, self.client_secret),
                    headers={"User-Agent": self.user_agent},
                )
                response.raise_for_status()
                payload = response.json()
                token = str(payload["access_token"]).strip()
                expires_in = int(payload["expires_in"])
                if not token or expires_in <= 0:
                    raise ValueError("invalid token response")
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise PicoSourceError("Reddit authorization failed; check Pico's Reddit credentials.") from exc
            self._access_token = token
            self._token_expires_at = time.monotonic() + expires_in
            return token

    @staticmethod
    def _parse_post(post_id: str, post: object) -> SourcePost:
        if not isinstance(post, dict):
            raise PicoSourceError("Reddit post is unavailable or has no supported still images.")
        author = str(post.get("author") or "").strip()
        permalink = str(post.get("permalink") or "").strip()
        if (
            not author
            or author.casefold() == "[deleted]"
            or not permalink
            or post.get("removed_by_category")
        ):
            raise PicoSourceError("Reddit post is unavailable or has no supported still images.")
        canonical_url = normalize_public_http_url(urljoin("https://www.reddit.com", permalink))
        images: list[SourceImage] = []
        gallery = post.get("gallery_data")
        metadata = post.get("media_metadata")
        if isinstance(gallery, dict) and isinstance(metadata, dict):
            items = gallery.get("items")
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    media_id = str(item.get("media_id") or "")
                    media = metadata.get(media_id)
                    if not isinstance(media, dict):
                        continue
                    source = media.get("s")
                    if (
                        media.get("status") != "valid"
                        or media.get("e") != "Image"
                        or not isinstance(source, dict)
                    ):
                        continue
                    source_url = html.unescape(str(source.get("u") or "").strip())
                    if source_url:
                        images.append(SourceImage(len(images) + 1, source_url))
        else:
            destination = html.unescape(
                str(post.get("url_overridden_by_dest") or "").strip()
            )
            if destination and not post.get("is_video"):
                images.append(SourceImage(1, destination))
        if not images:
            raise PicoSourceError("Reddit post is unavailable or has no supported still images.")
        attribution_label = f"u/{author}"
        sentence = f"Posted by {attribution_label} on Reddit — {canonical_url}"
        return SourcePost(
            kind="reddit",
            canonical_url=canonical_url,
            attribution_label=attribution_label,
            attribution_sentence=sentence,
            images=tuple(images),
            source_id=post_id,
        )


@dataclass(frozen=True)
class _DownloadedResource:
    final_url: str
    content_type: str
    data: bytes


class PicoMediaLoader:
    def __init__(
        self,
        reddit: RedditPostClient,
        metadata_writer: MetadataWriter,
        archive_search_tag: str,
        client: Optional[httpx.AsyncClient] = None,
        resolver: Optional[Resolver] = None,
        webpage_renderer: WebpageRenderer | None = None,
        photographer_finder: PhotographerResearcher | None = None,
    ) -> None:
        self.reddit = reddit
        self.metadata_writer = metadata_writer
        self.archive_search_tag = archive_search_tag
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
        )
        self.resolver = resolver or resolve_host
        self.webpage_renderer = webpage_renderer
        self.photographer_finder = photographer_finder

    async def close(self) -> None:
        closers = []
        if self.webpage_renderer is not None:
            closers.append(self.webpage_renderer.close())
        if self.photographer_finder is not None:
            closers.append(self.photographer_finder.close())
        if self._owns_client:
            closers.append(self.client.aclose())
        if closers:
            await asyncio.gather(*closers, return_exceptions=True)

    async def prepare(self, url: str, destination: Path) -> PreparedImport:
        import_key = uuid.uuid4().hex
        destination = Path(destination)
        staging = destination / f".staging-{import_key}"
        final = destination / import_key
        await asyncio.to_thread(staging.mkdir, parents=True, exist_ok=False)
        omitted_count = 0
        skipped_count = 0
        try:
            if is_reddit_post_url(url):
                source = await self.reddit.resolve(url)
                if len(source.images) > MAX_IMPORT_IMAGES:
                    raise PicoSourceError("An import may contain at most 20 images.")
                downloaded: list[
                    tuple[SourceImage, Path, tuple[str, str] | None]
                ] = []
                for image in source.images:
                    raw_path = staging / f"raw-{image.ordinal}"
                    resource = await self._download(image.url)
                    await asyncio.to_thread(raw_path.write_bytes, resource.data)
                    downloaded.append((image, raw_path, None))
            else:
                resource = await self._download(url)
                raw_path = staging / "raw-1"
                await asyncio.to_thread(raw_path.write_bytes, resource.data)
                inspected: tuple[str, str] | None = None
                try:
                    inspected = await asyncio.to_thread(_inspect_image, raw_path)
                except PicoSourceError as invalid_image:
                    if not _looks_like_html(resource):
                        raise invalid_image
                if inspected is None:
                    raw_path.unlink(missing_ok=True)
                    (
                        source,
                        downloaded,
                        omitted_count,
                        skipped_count,
                    ) = await self._prepare_webpage(resource.final_url, staging)
                else:
                    host = (urlsplit(resource.final_url).hostname or "").casefold()
                    sentence = f"Source: {host} — {resource.final_url}"
                    image = SourceImage(1, resource.final_url)
                    source = SourcePost(
                        kind="direct",
                        canonical_url=resource.final_url,
                        attribution_label=host,
                        attribution_sentence=sentence,
                        images=(image,),
                    )
                    downloaded = [(image, raw_path, inspected)]

            prepared: list[PreparedFile] = []
            total_size = 0
            source_stem = (
                f"reddit-{source.source_id}"
                if source.kind == "reddit"
                else source.attribution_label
            )
            stem = f"{self.archive_search_tag}-{_safe_stem(source_stem)}"
            for image, raw_path, inspected in downloaded:
                extension, mime_type = (
                    inspected
                    if inspected is not None
                    else await asyncio.to_thread(_inspect_image, raw_path)
                )
                filename = _attachment_name(stem, image.ordinal, extension)
                output_path = staging / filename
                await asyncio.to_thread(
                    self.metadata_writer.write,
                    raw_path,
                    output_path,
                    image.attribution_sentence or source.attribution_sentence,
                    source.canonical_url,
                    self.archive_search_tag,
                )
                size = output_path.stat().st_size
                if size > MAX_IMAGE_BYTES:
                    raise PicoSourceError(
                        "Prepared image exceeds the 10,000,000-byte limit."
                    )
                total_size += size
                if total_size > MAX_IMPORT_BYTES:
                    raise PicoSourceError("Import exceeds the 100,000,000-byte limit.")
                raw_path.unlink()
                prepared.append(
                    PreparedFile(
                        image.ordinal, final / filename, filename, mime_type, size
                    )
                )
            await asyncio.to_thread(os.replace, staging, final)
            return PreparedImport(
                import_key,
                source,
                tuple(prepared),
                omitted_count=omitted_count,
                skipped_count=skipped_count,
            )
        except Exception:
            await asyncio.to_thread(shutil.rmtree, staging, True)
            raise

    async def _prepare_webpage(
        self, url: str, staging: Path
    ) -> tuple[
        SourcePost,
        list[tuple[SourceImage, Path, tuple[str, str] | None]],
        int,
        int,
    ]:
        if self.webpage_renderer is None:
            raise PicoSourceError(
                "Webpage imports require Pico's optional webpage support; install "
                "pico-photo-bot[webpage] and Chromium."
            )
        try:
            page = await self.webpage_renderer.render(url)
            canonical_url = normalize_public_http_url(page.canonical_url)
            await validate_public_http_url(canonical_url, self.resolver)
        except (WebpageError, PublicUrlError) as exc:
            raise PicoSourceError(str(exc)) from exc
        attributions = await resolve_attributions(page, self.photographer_finder)
        host = (urlsplit(canonical_url).hostname or "webpage").casefold()
        fallback_sentence = f"Source: {host} — {canonical_url}"
        selected = page.candidates[:MAX_IMPORT_IMAGES]
        omitted_count = max(0, page.total_count - len(selected))
        skipped_count = 0
        downloaded: list[
            tuple[SourceImage, Path, tuple[str, str] | None]
        ] = []
        images: list[SourceImage] = []
        for candidate in selected:
            raw_path = staging / f"raw-{candidate.ordinal}"
            try:
                resource = await self._download(candidate.url)
                await asyncio.to_thread(raw_path.write_bytes, resource.data)
                inspected = await asyncio.to_thread(_inspect_image, raw_path)
            except (PicoSourceError, OSError):
                raw_path.unlink(missing_ok=True)
                skipped_count += 1
                continue
            attribution = attributions[candidate.ordinal]
            image = SourceImage(
                candidate.ordinal,
                resource.final_url,
                caption=candidate.caption or None,
                attribution_label=attribution.label,
                attribution_sentence=attribution.sentence,
            )
            images.append(image)
            downloaded.append((image, raw_path, inspected))
        if not downloaded:
            raise PicoSourceError(
                "The webpage did not contain any downloadable JPEG, PNG, or WebP photos."
            )
        source = SourcePost(
            kind="webpage",
            canonical_url=canonical_url,
            attribution_label=host,
            attribution_sentence=fallback_sentence,
            images=tuple(images),
        )
        return source, downloaded, omitted_count, skipped_count

    async def _download(self, url: str) -> _DownloadedResource:
        try:
            current = normalize_public_http_url(url)
        except PublicUrlError as exc:
            raise PicoSourceError(str(exc)) from exc
        for redirect_count in range(MAX_REDIRECTS + 1):
            try:
                await validate_public_http_url(current, self.resolver)
            except PublicUrlError as exc:
                raise PicoSourceError(str(exc)) from exc
            try:
                async with self.client.stream(
                    "GET", current, follow_redirects=False
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise PicoSourceError("Image redirect omitted its destination.")
                        if redirect_count >= MAX_REDIRECTS:
                            raise PicoSourceError("Image URL exceeded 5 redirects.")
                        current = normalize_public_http_url(urljoin(current, location))
                        continue
                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if (
                        content_length
                        and content_length.isdigit()
                        and int(content_length) > MAX_IMAGE_BYTES
                    ):
                        raise PicoSourceError(
                            "Image exceeds the 10,000,000-byte limit."
                        )
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_IMAGE_BYTES:
                            raise PicoSourceError(
                                "Image exceeds the 10,000,000-byte limit."
                            )
                    return _DownloadedResource(
                        current,
                        response.headers.get("content-type", "")
                        .partition(";")[0]
                        .strip()
                        .casefold(),
                        bytes(data),
                    )
            except PublicUrlError as exc:
                raise PicoSourceError(str(exc)) from exc
            except httpx.HTTPError as exc:
                raise PicoSourceError(
                    "Image download failed; check the link and try again."
                ) from exc
        raise PicoSourceError("Image URL exceeded 5 redirects.")


def _looks_like_html(resource: _DownloadedResource) -> bool:
    if resource.content_type in {"text/html", "application/xhtml+xml"}:
        return True
    prefix = resource.data.lstrip()[:256].lower()
    return prefix.startswith((b"<!doctype html", b"<html"))


def _inspect_image(path: Path) -> tuple[str, str]:
    try:
        with Image.open(path) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
            frames = getattr(image, "n_frames", 1)
            image.verify()
        with Image.open(path) as image:
            image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise PicoSourceError("Source is not a valid JPEG, PNG, or WebP image.") from exc
    formats = {
        "JPEG": ("jpg", "image/jpeg"),
        "PNG": ("png", "image/png"),
        "WEBP": ("webp", "image/webp"),
    }
    if image_format not in formats:
        raise PicoSourceError("Source is not a valid JPEG, PNG, or WebP image.")
    if width * height > MAX_IMAGE_PIXELS:
        raise PicoSourceError("Image exceeds the 50,000,000-pixel limit.")
    if frames != 1:
        raise PicoSourceError("Animated images are not supported.")
    return formats[image_format]


def _safe_stem(value: str) -> str:
    clean = re.sub(r"[^a-z0-9.-]+", "-", value.casefold()).strip("-.")
    return clean[:80] or "image"


def _attachment_name(stem: str, ordinal: int, extension: str) -> str:
    suffix = f"-{ordinal:02d}.{extension}"
    return f"{stem[: 100 - len(suffix)]}{suffix}"
