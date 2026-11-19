"""Turn per-window recognition results into a tracklist.

Consecutive windows that matched the same recording become one track that
starts at the first matching window. Runs of unmatched windows become talk
segments. A recording seen in fewer than `min_hits` windows is treated as a
false positive. Windows that were given up on ("dead") become a gap marked
for review instead of silently disappearing.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Entry:
    offset_s: float
    media_type: str  # music | talk | id | promo
    title: str
    artist: str = ""
    album: str = ""
    label: str = ""
    isrc: str = ""
    year: str = ""
    needs_review: bool = False
    hits: int = 0
    end_s: float = 0.0
    notes: list = field(default_factory=list)


def _key(m: dict) -> tuple:
    return (m.get("isrc") or "", m["title"].casefold(), m["artist"].casefold())


def assemble(windows: list[dict], *, min_hits: int = 2, talk_title: str = "Talk", host: str = "") -> list[Entry]:
    entries: list[Entry] = []
    for w in windows:
        m = w.get("match")
        if w.get("status") == "dead":
            kind, key = "review", None
        elif m:
            kind, key = "music", _key(m)
        else:
            kind, key = "talk", None
        last = entries[-1] if entries else None
        if last and kind == "music" and last.media_type == "music" and _key(last.__dict__) == key:
            last.hits += 1
            last.end_s = w["end_s"]
            continue
        if last and kind == "talk" and last.media_type == "talk" and not last.needs_review:
            last.end_s = w["end_s"]
            continue
        if last and kind == "review" and last.needs_review:
            last.end_s = w["end_s"]
            continue
        if kind == "music":
            entries.append(
                Entry(
                    offset_s=w["start_s"],
                    media_type="music",
                    title=m["title"],
                    artist=m["artist"],
                    album=m.get("album", ""),
                    label=m.get("label", ""),
                    isrc=m.get("isrc", ""),
                    hits=1,
                    end_s=w["end_s"],
                )
            )
        elif kind == "talk":
            entries.append(Entry(offset_s=w["start_s"], media_type="talk", title=talk_title, artist=host, end_s=w["end_s"]))
        else:
            entries.append(
                Entry(
                    offset_s=w["start_s"],
                    media_type="talk",
                    title="Unidentified (review)",
                    needs_review=True,
                    end_s=w["end_s"],
                    notes=["recognition failed after all retries"],
                )
            )

    # Drop one-off matches (likely false positives) and fold them into neighbours.
    cleaned: list[Entry] = []
    for e in entries:
        if e.media_type == "music" and e.hits < min_hits:
            e = Entry(offset_s=e.offset_s, media_type="talk", title=talk_title, artist=host, end_s=e.end_s)
        prev = cleaned[-1] if cleaned else None
        if prev and e.media_type == "talk" and prev.media_type == "talk" and not e.needs_review and not prev.needs_review:
            prev.end_s = e.end_s
            continue
        if (
            prev
            and e.media_type == "music"
            and prev.media_type == "music"
            and _key(prev.__dict__) == _key(e.__dict__)
        ):
            prev.end_s, prev.hits = e.end_s, prev.hits + e.hits
            continue
        cleaned.append(e)

    # A failed window between two halves of the same recording is bridged:
    # the song clearly kept playing, so it stays one track (with a note).
    bridged: list[Entry] = []
    i = 0
    while i < len(cleaned):
        e = cleaned[i]
        if (
            e.needs_review
            and bridged
            and i + 1 < len(cleaned)
            and bridged[-1].media_type == "music"
            and cleaned[i + 1].media_type == "music"
            and _key(bridged[-1].__dict__) == _key(cleaned[i + 1].__dict__)
        ):
            prev, nxt = bridged[-1], cleaned[i + 1]
            prev.end_s, prev.hits = nxt.end_s, prev.hits + nxt.hits
            prev.notes.append("bridged a failed sample inside this track")
            i += 2
            continue
        bridged.append(e)
        i += 1
    return bridged
