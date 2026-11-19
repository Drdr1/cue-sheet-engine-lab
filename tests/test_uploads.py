import io
import os
import stat

import pytest

from cuelab.uploads import UploadRejected, delete_upload, save_upload


def test_stores_privately_and_checksums(tmp_path):
    data = b"ID3" + os.urandom(3 * 1024 * 1024)
    up = save_upload(io.BytesIO(data), str(tmp_path / "up"), "abc123")
    assert up.size == len(data)
    assert stat.S_IMODE(os.stat(up.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(up.path)).st_mode) == 0o700
    delete_upload(up.path)
    assert not os.path.exists(os.path.dirname(up.path))


@pytest.mark.parametrize(
    "payload,sid,msg",
    [
        (b"MZ\x90\x00not audio", "abc", "not an MP3"),
        (b"", "abc", "empty"),
        (b"ID3data", "../etc", "invalid show id"),
    ],
)
def test_rejects_bad_uploads_and_leaves_nothing(tmp_path, payload, sid, msg):
    root = tmp_path / "up"
    with pytest.raises(UploadRejected, match=msg):
        save_upload(io.BytesIO(payload), str(root), sid)
    assert not root.exists() or os.listdir(root) == []


def test_size_limit(tmp_path):
    with pytest.raises(UploadRejected, match="too large"):
        save_upload(io.BytesIO(b"ID3" + b"\0" * 5000), str(tmp_path), "big1", max_bytes=1000)
    assert os.listdir(tmp_path) == []
