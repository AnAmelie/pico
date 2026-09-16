import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from pico_photo_bot.metadata import PicoMetadataError, PicoMetadataWriter

ATTRIBUTION = "Posted by u/poster on Reddit — https://www.reddit.com/r/pics/comments/abc/title/"
SOURCE_URL = "https://www.reddit.com/r/pics/comments/abc/title/"
ARCHIVE_TAG = "picoarc0123456789ab"


def metadata_values(value):
    return value if isinstance(value, list) else [value]




@pytest.mark.parametrize(
    ("image_format", "extension"),
    [("JPEG", "jpg"), ("PNG", "png"), ("WEBP", "webp")],
)
def test_exiftool_round_trip_preserves_pixels_and_unrelated_metadata(
    tmp_path: Path, image_format: str, extension: str
):
    source = tmp_path / f"source.{extension}"
    destination = tmp_path / f"prepared.{extension}"
    options = {"lossless": True} if image_format == "WEBP" else {}
    Image.new("RGB", (8, 6), (31, 97, 159)).save(source, image_format, **options)
    subprocess.run(
        ["exiftool", "-overwrite_original", "-XMP-xmp:Label=Keep me", str(source)],
        check=True,
        capture_output=True,
    )
    with Image.open(source) as image:
        pixels_before = image.convert("RGB").tobytes()

    PicoMetadataWriter().write(
        source, destination, ATTRIBUTION, SOURCE_URL, ARCHIVE_TAG
    )

    metadata = json.loads(
        subprocess.run(
            [
                "exiftool",
                "-j",
                "-G1",
                "-XMP-dc:Description",
                "-XMP-dc:Source",
                "-XMP-xmp:Label",
                "-XMP-dc:Subject",
                "-EXIF:ImageDescription",
                "-EXIF:UserComment",
                "-IPTC:Caption-Abstract",
                "-IPTC:Source",
                "-IPTC:Keywords",
                str(destination),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )[0]
    with Image.open(destination) as image:
        pixels_after = image.convert("RGB").tobytes()

    assert metadata["XMP-dc:Description"] == ATTRIBUTION
    assert metadata["XMP-dc:Source"] == SOURCE_URL
    assert metadata_values(metadata["XMP-dc:Subject"]) == [ARCHIVE_TAG]
    assert metadata["IFD0:ImageDescription"] == ATTRIBUTION
    assert metadata["ExifIFD:UserComment"] == ATTRIBUTION
    assert metadata["XMP-xmp:Label"] == "Keep me"
    if image_format in {"JPEG", "PNG"}:
        assert metadata["IPTC:Caption-Abstract"] == ATTRIBUTION
        assert metadata_values(metadata["IPTC:Keywords"]) == [ARCHIVE_TAG]
    else:
        assert not any(key.startswith("IPTC:") for key in metadata)
    assert pixels_after == pixels_before


def test_metadata_writer_fails_actionably_without_exiftool(monkeypatch):
    monkeypatch.setattr("pico_photo_bot.metadata.shutil.which", lambda executable: None)
    with pytest.raises(PicoMetadataError, match="install exiftool"):
        PicoMetadataWriter()
