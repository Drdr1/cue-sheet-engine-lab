"""Live365 MultiTrack export and validation.

Rules implemented (from Live365's "MultiTrack Cue Sheet and CSV file
requirements" help article, plus the job's Live365 preset):

* CSV header exactly ``offset,media_type,title,artist,album,year``.
* Offsets in ``HH:MM:SS.mmm`` (".000 timing") - used in both .csv and .cue.
* ``media_type`` entirely lowercase (case-sensitive on Live365's side).
* No two markers at the same timestamp (Live365 rejects the file).
* Marker limits, applied cumulatively from the start of the file:
  at most 5 in the first 10 min, 7 in 15 min, 13 in 30 min, 33 in 78 min.
  (Live365's article gives no figure beyond 78 minutes; we report that as a
  warning for long shows instead of guessing.)
* title + artist required for music rows (Live365 needs them for reporting).

Every export is parsed back and validated before it is offered for download.
"""
from __future__ import annotations

import csv
import io
import re
from decimal import ROUND_HALF_UP, Decimal
from dataclasses import dataclass, field

from .assemble import Entry

CSV_HEADER = ["offset", "media_type", "title", "artist", "album", "year"]
OFFSET_RE = re.compile(r"^\d{2,}:[0-5]\d:[0-5]\d\.\d{3}$")
KNOWN_MEDIA_TYPES = {"music", "talk", "id", "promo"}
# (window in seconds, max markers inside that window measured from 00:00:00)
MARKER_LIMITS = [(10 * 60, 5), (15 * 60, 7), (30 * 60, 13), (78 * 60, 33)]


class ValidationError(Exception):
    def __init__(self, report: "Report"):
        super().__init__("; ".join(report.errors))
        self.report = report


@dataclass
class Report:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _round_units(seconds: float, per_second: int) -> int:
    # Exact half-up rounding: float math would turn 3.1245 s into 3.124 s.
    return int((Decimal(repr(seconds)) * per_second).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fmt_offset(seconds: float) -> str:
    ms_total = _round_units(seconds, 1000)
    h, rem = divmod(ms_total, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def parse_offset(text: str) -> float:
    h, m, rest = text.split(":")
    s, ms = rest.split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _cue_quote(text: str) -> str:
    # .cue strings are double-quoted; an embedded quote would end the field.
    return '"' + text.replace('"', "'").replace("\n", " ").strip() + '"'


# -- validation --------------------------------------------------------------
def validate_rows(rows: list[dict], duration_s: float) -> Report:
    r = Report()
    offsets: list[float] = []
    seen: dict[str, int] = {}
    for n, row in enumerate(rows, start=2):  # row 1 is the header
        off = row.get("offset", "")
        if not OFFSET_RE.match(off):
            r.errors.append(f"row {n}: offset {off!r} is not HH:MM:SS.mmm")
            continue
        if off in seen:
            r.errors.append(f"row {n}: duplicate timestamp {off} (also row {seen[off]})")
        seen[off] = n
        secs = parse_offset(off)
        if offsets and secs < offsets[-1]:
            r.errors.append(f"row {n}: offset {off} is earlier than the previous row")
        if secs >= duration_s:
            r.errors.append(f"row {n}: offset {off} is past the end of the audio ({fmt_offset(duration_s)})")
        offsets.append(secs)
        mt = row.get("media_type", "")
        if mt != mt.lower():
            r.errors.append(f"row {n}: media_type {mt!r} must be lowercase")
        elif mt not in KNOWN_MEDIA_TYPES:
            r.warnings.append(f"row {n}: media_type {mt!r} is not one of {sorted(KNOWN_MEDIA_TYPES)}")
        if mt.lower() == "music" and (not row.get("title") or not row.get("artist")):
            r.errors.append(f"row {n}: music rows need both title and artist")

    for window, limit in MARKER_LIMITS:
        count = sum(1 for o in offsets if o < window)
        if count > limit:
            r.errors.append(
                f"{count} markers in the first {window // 60} minutes; Live365 allows at most {limit}"
            )
    if duration_s > 78 * 60:
        r.warnings.append(
            "show is longer than 78 minutes; Live365's article lists no marker limit past 78 minutes"
        )
    if offsets and offsets[0] != 0:
        r.warnings.append("first marker is not at 00:00:00.000 (recommended by Live365)")
    return r


def parse_csv(text: str) -> tuple[list[str], list[dict]]:
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])
    rows = [dict(zip(CSV_HEADER, line + [""] * (len(CSV_HEADER) - len(line)))) for line in reader if line]
    return header, rows


def validate_csv(text: str, duration_s: float) -> Report:
    if text.startswith("﻿"):
        r = Report(errors=["file starts with a byte-order mark; header must begin at column A"])
        return r
    header, rows = parse_csv(text)
    r = validate_rows(rows, duration_s)
    if header != CSV_HEADER:
        r.errors.insert(0, f"header is {header}, expected {CSV_HEADER}")
    return r


_CUE_INDEX = re.compile(r"^\s+INDEX 01 (\S+)$")


def validate_cue(text: str, duration_s: float) -> Report:
    r = Report()
    lines = text.splitlines()
    if not any(l.startswith("FILE ") and l.rstrip().endswith(" MP3") for l in lines):
        r.errors.append("missing FILE \"...\" MP3 line")
    rows, current = [], {}
    for line in lines:
        s = line.strip()
        if s.startswith("TRACK "):
            current = {"media_type": "music"}
        elif s.startswith("TITLE ") and current is not None and line.startswith("    "):
            current["title"] = s[6:].strip('"')
        elif s.startswith("PERFORMER ") and line.startswith("    "):
            current["artist"] = s[10:].strip('"')
        elif s.startswith("REM MEDIA_TYPE "):
            current["media_type"] = s.split()[-1]
        else:
            m = _CUE_INDEX.match(line)
            if m:
                current["offset"] = m.group(1)
                rows.append(current)
    sub = validate_rows(rows, duration_s)
    r.errors += sub.errors
    r.warnings += sub.warnings
    return r


def fit_marker_limits(entries: list[Entry]) -> tuple[list[Entry], list[str]]:
    """Optionally bring a tracklist inside Live365's marker limits.

    Removes the shortest non-music markers first (their time folds into the
    previous entry). Music markers are never removed, since each song must
    carry its own artist/title for reporting. Returns the new list and a
    note per removed marker; if music alone breaks a limit, that is left for
    the review screen and validation will still fail.
    """
    out = list(entries)
    notes: list[str] = []

    def over_limit() -> tuple[int, int] | None:
        for window, limit in MARKER_LIMITS:
            if sum(1 for e in out if e.offset_s < window) > limit:
                return window, limit
        return None

    while (hit := over_limit()) is not None:
        window, _ = hit
        candidates = [
            (i, e) for i, e in enumerate(out) if i > 0 and e.media_type != "music" and e.offset_s < window
        ]
        if not candidates:
            break
        i, e = min(candidates, key=lambda ie: (ie[1].end_s - ie[1].offset_s, -ie[0]))
        out[i - 1].end_s = max(out[i - 1].end_s, e.end_s)
        del out[i]
        notes.append(f"removed {e.media_type} marker at {fmt_offset(e.offset_s)} to meet Live365 marker limits")
    return out, notes


# -- export ------------------------------------------------------------------
def to_rows(entries: list[Entry]) -> list[dict]:
    return [
        {
            "offset": fmt_offset(e.offset_s),
            "media_type": e.media_type.lower(),
            "title": e.title,
            "artist": e.artist,
            "album": e.album,
            "year": e.year,
        }
        for e in entries
    ]


def export_csv(entries: list[Entry], duration_s: float) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADER)
    for row in to_rows(entries):
        w.writerow([row[k] for k in CSV_HEADER])
    text = buf.getvalue()
    report = validate_csv(text, duration_s)
    if not report.ok:
        raise ValidationError(report)
    return text


def export_cue(entries: list[Entry], *, performer: str, title: str, filename: str, duration_s: float) -> str:
    out = [
        f"PERFORMER {_cue_quote(performer)}",
        f"TITLE {_cue_quote(title)}",
        f"FILE {_cue_quote(filename)} MP3",
    ]
    for n, e in enumerate(entries, start=1):
        out += [
            f"  TRACK {n:02d} AUDIO",
            f"    TITLE {_cue_quote(e.title)}",
            f"    PERFORMER {_cue_quote(e.artist)}",
        ]
        if e.media_type != "music":
            out.append(f"    REM MEDIA_TYPE {e.media_type.lower()}")
        out.append(f"    INDEX 01 {fmt_offset(e.offset_s)}")
    text = "\n".join(out) + "\n"
    report = validate_cue(text, duration_s)
    if not report.ok:
        raise ValidationError(report)
    return text


def export_standard_cue(entries: list[Entry], *, performer: str, title: str, filename: str) -> str:
    """Classic CDRWIN-style cue: INDEX in MM:SS:FF (75 frames per second)."""

    def mmssff(seconds: float) -> str:
        frames = _round_units(seconds, 75)
        m, rem = divmod(frames, 75 * 60)
        s, f = divmod(rem, 75)
        return f"{m:02d}:{s:02d}:{f:02d}"

    out = [f"PERFORMER {_cue_quote(performer)}", f"TITLE {_cue_quote(title)}", f"FILE {_cue_quote(filename)} MP3"]
    for n, e in enumerate(entries, start=1):
        out += [
            f"  TRACK {n:02d} AUDIO",
            f"    TITLE {_cue_quote(e.title)}",
            f"    PERFORMER {_cue_quote(e.artist)}",
        ]
        if e.isrc:
            out.append(f"    ISRC {e.isrc.replace('-', '')}")
        out.append(f"    INDEX 01 {mmssff(e.offset_s)}")
    return "\n".join(out) + "\n"
