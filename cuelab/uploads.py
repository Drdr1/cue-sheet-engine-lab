"""Temporary upload storage.

Large MP3s are streamed to disk in chunks (never held in memory), stored
under a per-show directory readable only by the service user, checksummed,
and deleted as soon as the show finishes, whether it succeeded or needs
review. Nothing about a customer's audio outlives its processing.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from typing import BinaryIO

CHUNK = 1024 * 1024
MP3_MAGIC = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")


class UploadRejected(Exception):
    pass


@dataclass
class StoredUpload:
    path: str
    size: int
    sha256: str


def save_upload(stream: BinaryIO, root: str, show_id: str, *, max_bytes: int = 2 * 1024**3) -> StoredUpload:
    if not show_id.isalnum():
        raise UploadRejected("invalid show id")
    os.makedirs(root, mode=0o700, exist_ok=True)
    show_dir = os.path.join(root, show_id)
    os.mkdir(show_dir, 0o700)
    path = os.path.join(show_dir, "audio.mp3")
    digest, size = hashlib.sha256(), 0
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            first = True
            while chunk := stream.read(CHUNK):
                if first:
                    if not chunk.startswith(MP3_MAGIC):
                        raise UploadRejected("not an MP3 file")
                    first = False
                size += len(chunk)
                if size > max_bytes:
                    raise UploadRejected("file too large")
                digest.update(chunk)
                f.write(chunk)
            if first:
                raise UploadRejected("empty upload")
    except BaseException:
        shutil.rmtree(show_dir, ignore_errors=True)
        raise
    return StoredUpload(path=path, size=size, sha256=digest.hexdigest())


def delete_upload(path: str | None) -> None:
    if path:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
