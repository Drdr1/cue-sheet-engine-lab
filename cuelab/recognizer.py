"""Recognition providers.

`Recognizer` is the interface the engine depends on. The lab runs a
deterministic `SimulatedRecognizer` driven by a show manifest (what plays
when), with injectable latency and transient failures, so retries, resume
and fairness can be tested without API keys or real audio.

`ACRCloudRecognizer` shows how the real provider plugs in behind the same
interface: request signing and response mapping (title, artist, album,
label, ISRC). It has NOT been run against the live ACRCloud API in this lab.
Keys are read from the server environment and never sent to the browser.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from typing import Optional, Protocol


class TransientError(Exception):
    """Retryable failure: timeout, 5xx, or provider rate limit (429)."""


@dataclass
class Match:
    title: str
    artist: str
    album: str = ""
    label: str = ""
    isrc: str = ""
    provider: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class Recognizer(Protocol):
    name: str

    def recognize(self, show_id: str, start_s: float, end_s: float) -> Optional[Match]:
        ...


class SimulatedRecognizer:
    """Answers from a manifest: [{start, end, kind, title, artist, album, label, isrc}].

    Music items are "recognised" when the sample window's midpoint falls in
    them; talk/id items return no match, like a real provider would.
    """

    name = "simulated"

    def __init__(
        self,
        manifest_for,
        *,
        latency_s: float = 0.0,
        transient_rate: float = 0.0,
        seed: int = 7,
    ):
        self.manifest_for = manifest_for
        self.latency_s = latency_s
        self.transient_rate = transient_rate
        self._rng = random.Random(seed)

    def recognize(self, show_id: str, start_s: float, end_s: float) -> Optional[Match]:
        if self.latency_s:
            time.sleep(self.latency_s)
        if self.transient_rate and self._rng.random() < self.transient_rate:
            raise TransientError("simulated 429 / timeout")
        mid = (start_s + end_s) / 2
        for item in self.manifest_for(show_id) or []:
            if item["start"] <= mid < item["end"]:
                if item.get("kind", "music") != "music":
                    return None
                return Match(
                    title=item["title"],
                    artist=item["artist"],
                    album=item.get("album", ""),
                    label=item.get("label", ""),
                    isrc=item.get("isrc", ""),
                    provider=self.name,
                )
        return None


class ACRCloudRecognizer:
    """ACRCloud Identification API adapter (signature v1). Not run live in this lab.

    `audio_slice(show_id, start_s, end_s) -> bytes` supplies the sample
    (for example an ffmpeg cut of the uploaded MP3).
    """

    name = "acrcloud"

    def __init__(self, audio_slice, host: Optional[str] = None, timeout_s: float = 15.0):
        self.audio_slice = audio_slice
        self.host = host or os.environ["ACRCLOUD_HOST"]
        self.access_key = os.environ["ACRCLOUD_ACCESS_KEY"]
        self.access_secret = os.environ["ACRCLOUD_ACCESS_SECRET"]
        self.timeout_s = timeout_s

    @staticmethod
    def sign(access_key: str, access_secret: str, timestamp: str) -> str:
        string_to_sign = "\n".join(["POST", "/v1/identify", access_key, "audio", "1", timestamp])
        digest = hmac.new(access_secret.encode(), string_to_sign.encode(), hashlib.sha1).digest()
        return base64.b64encode(digest).decode()

    @staticmethod
    def parse(body: dict) -> Optional[Match]:
        code = body.get("status", {}).get("code")
        if code == 1001:  # no result
            return None
        if code in (3000, 3002, 3003, 3015):  # http error / 5xx / limit / QPS: retry later
            raise TransientError(f"acrcloud status {code}")
        if code != 0:
            raise RuntimeError(f"acrcloud status {code}: {body.get('status', {}).get('msg')}")
        music = (body.get("metadata", {}).get("music") or [None])[0]
        if not music:
            return None
        return Match(
            title=music.get("title", ""),
            artist=", ".join(a.get("name", "") for a in music.get("artists", [])),
            album=(music.get("album") or {}).get("name", ""),
            label=music.get("label", ""),
            isrc=(music.get("external_ids") or {}).get("isrc", ""),
            provider="acrcloud",
        )

    def recognize(self, show_id: str, start_s: float, end_s: float) -> Optional[Match]:
        sample = self.audio_slice(show_id, start_s, end_s)
        ts = str(int(time.time()))
        fields = {
            "access_key": self.access_key,
            "data_type": "audio",
            "signature_version": "1",
            "signature": self.sign(self.access_key, self.access_secret, ts),
            "timestamp": ts,
            "sample_bytes": str(len(sample)),
        }
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="sample"; filename="s.mp3"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode()
            + sample
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            f"https://{self.host}/v1/identify",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return self.parse(json.loads(resp.read()))
        except (TimeoutError, ConnectionError) as e:
            raise TransientError(str(e)) from e
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                raise TransientError(f"http {e.code}") from e
            raise
