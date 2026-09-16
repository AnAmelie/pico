from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

SourceKind = Literal["reddit", "direct", "webpage"]
ImportState = Literal[
    "preparing", "ready", "uploading", "failed", "uploaded", "expired"
]
FileState = Literal["pending", "tokenized", "failed", "uploaded"]


@dataclass(frozen=True)
class SourceImage:
    ordinal: int
    url: str
    caption: str | None = None
    attribution_label: str | None = None
    attribution_sentence: str | None = None


@dataclass(frozen=True)
class SourcePost:
    kind: SourceKind
    canonical_url: str
    attribution_label: str
    attribution_sentence: str
    images: tuple[SourceImage, ...]
    source_id: str | None = None


@dataclass(frozen=True)
class PreparedFile:
    ordinal: int
    path: Path
    filename: str
    mime_type: str
    size: int


@dataclass(frozen=True)
class PreparedImport:
    import_key: str
    source: SourcePost
    files: tuple[PreparedFile, ...]
    omitted_count: int = 0
    skipped_count: int = 0

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)


@dataclass(frozen=True)
class PicoFile:
    id: int
    import_id: int
    ordinal: int
    path: Path
    filename: str
    mime_type: str
    size: int
    state: FileState
    upload_token: str | None = None
    upload_token_at: datetime | None = None
    media_item_id: str | None = None
    media_item_url: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class PicoImport:
    id: int
    submitter_id: int
    source_kind: SourceKind
    source_url: str
    attribution_label: str
    attribution_sentence: str
    state: ImportState
    album_name: str | None
    album_id: str | None
    album_url: str | None
    control_channel_id: int | None
    control_message_id: int | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    files: tuple[PicoFile, ...] = ()

    @property
    def uploaded_count(self) -> int:
        return sum(item.media_item_id is not None for item in self.files)

    @property
    def remaining_count(self) -> int:
        return len(self.files) - self.uploaded_count


@dataclass(frozen=True)
class PhotosAlbum:
    id: str
    title: str
    product_url: str


@dataclass(frozen=True)
class PhotosUploadResult:
    import_id: int
    album: PhotosAlbum
    completed_count: int
    remaining_count: int
    errors: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.remaining_count == 0
