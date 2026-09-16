from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class PicoMetadataError(RuntimeError):
    pass


class PicoMetadataWriter:
    def __init__(self, executable: str = "exiftool") -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise PicoMetadataError(
                "ExifTool is required for Pico; install exiftool and ensure it is on PATH."
            )
        self.executable = resolved

    def write(
        self,
        source: Path,
        destination: Path,
        attribution: str,
        source_url: str,
        archive_search_tag: str,
    ) -> None:
        source = Path(source)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        arguments = [
            self.executable,
            "-overwrite_original",
            "-P",
            f"-XMP-dc:Description={attribution}",
            f"-EXIF:ImageDescription={attribution}",
            f"-EXIF:UserComment={attribution}",
            f"-XMP-dc:Source={source_url}",
            f"-XMP-dc:Subject={archive_search_tag}",
        ]
        if destination.suffix.casefold() in {".jpg", ".jpeg", ".png"}:
            arguments.extend(
                (
                    f"-IPTC:Caption-Abstract={attribution}",
                    f"-IPTC:Source={source_url}",
                    f"-IPTC:Keywords={archive_search_tag}",
                )
            )
        arguments.append(str(destination))
        try:
            result = subprocess.run(
                arguments,
                check=True,
                capture_output=True,
                text=True,
            )
            if "1 image files updated" not in result.stdout:
                raise PicoMetadataError("ExifTool did not confirm the metadata update.")
            readback = subprocess.run(
                [
                    self.executable,
                    "-j",
                    "-G1",
                    "-XMP-dc:Description",
                    "-XMP-dc:Source",
                    "-XMP-dc:Subject",
                    str(destination),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(readback.stdout)
            metadata = payload[0]
            subjects = metadata.get("XMP-dc:Subject")
            normalized_subjects = [subjects] if isinstance(subjects, str) else subjects
            if (
                metadata.get("XMP-dc:Description") != attribution
                or metadata.get("XMP-dc:Source") != source_url
                or normalized_subjects != [archive_search_tag]
            ):
                raise PicoMetadataError("ExifTool metadata verification failed.")
        except (
            subprocess.CalledProcessError,
            json.JSONDecodeError,
            IndexError,
            KeyError,
            TypeError,
        ) as exc:
            destination.unlink(missing_ok=True)
            raise PicoMetadataError(
                "ExifTool could not write and verify Pico attribution metadata."
            ) from exc
        except PicoMetadataError:
            destination.unlink(missing_ok=True)
            raise
