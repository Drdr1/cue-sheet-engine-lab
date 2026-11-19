"""Live365 preset: format, validation and marker limits."""
import pytest

from cuelab import live365
from cuelab.assemble import Entry


def music(t, title="Song", artist="Artist", **kw):
    return Entry(offset_s=t, media_type="music", title=title, artist=artist, end_s=t + 200, **kw)


def talk(t, length=60):
    return Entry(offset_s=t, media_type="talk", title="Talk", end_s=t + length)


def test_offset_format_is_three_decimal():
    assert live365.fmt_offset(0) == "00:00:00.000"
    assert live365.fmt_offset(3.1245) == "00:00:03.125"
    assert live365.fmt_offset(3 * 3600 + 61.5) == "03:01:01.500"
    assert live365.parse_offset("01:02:03.004") == pytest.approx(3723.004)


def test_csv_has_exact_header_and_lowercase_media_type():
    text = live365.export_csv([talk(0), music(30, album="Album 1", isrc="USX1")], 600)
    lines = text.splitlines()
    assert lines[0] == "offset,media_type,title,artist,album,year"
    assert lines[1] == "00:00:00.000,talk,Talk,,,"
    assert lines[2] == "00:00:30.000,music,Song,Artist,Album 1,"


def test_csv_quotes_commas_in_titles():
    text = live365.export_csv([music(0, title="Hello, Goodbye", artist="Smith, J")], 600)
    assert '"Hello, Goodbye","Smith, J"' in text
    assert live365.validate_csv(text, 600).ok


def test_cue_uses_millisecond_timing_and_round_trips():
    text = live365.export_cue(
        [music(0, title='Say "Hi"'), talk(200)], performer="KDLT", title="Mix", filename="mix.mp3", duration_s=600
    )
    assert 'FILE "mix.mp3" MP3' in text
    assert "    INDEX 01 00:00:00.000" in text and "    INDEX 01 00:03:20.000" in text
    assert "TITLE \"Say 'Hi'\"" in text  # embedded quote cannot break the field
    assert "REM MEDIA_TYPE talk" in text


def test_standard_cue_uses_frames():
    text = live365.export_standard_cue([music(0), music(61.5, isrc="US-X12-24-00001")], performer="P", title="T", filename="f.mp3")
    assert "INDEX 01 01:01:38" in text  # 61.5s = 1 min, 1 s, 37.5 -> 38 frames
    assert "ISRC USX122400001" in text


def test_duplicate_timestamps_rejected():
    rows = [
        {"offset": "00:00:00.000", "media_type": "music", "title": "A", "artist": "B"},
        {"offset": "00:00:00.000", "media_type": "talk", "title": "T", "artist": ""},
    ]
    r = live365.validate_rows(rows, 600)
    assert any("duplicate timestamp" in e for e in r.errors)


def test_uppercase_media_type_rejected():
    r = live365.validate_csv("offset,media_type,title,artist,album,year\n00:00:00.000,Music,A,B,,\n", 600)
    assert any("lowercase" in e for e in r.errors)


def test_wrong_header_and_bad_offset_rejected():
    r = live365.validate_csv("Offset,Media_type,title,artist,album,year\n0:00:01,music,A,B,,\n", 600)
    assert any("header" in e for e in r.errors)
    assert any("HH:MM:SS.mmm" in e for e in r.errors)


def test_bom_rejected():
    r = live365.validate_csv("﻿offset,media_type,title,artist,album,year\n", 600)
    assert not r.ok


def test_music_requires_title_and_artist():
    r = live365.validate_csv("offset,media_type,title,artist,album,year\n00:00:00.000,music,A,,,\n", 600)
    assert any("title and artist" in e for e in r.errors)


def test_offset_past_end_rejected():
    r = live365.validate_csv("offset,media_type,title,artist,album,year\n00:20:00.000,talk,T,,,\n", 600)
    assert any("past the end" in e for e in r.errors)


def test_marker_limits_are_cumulative():
    # 6 markers in the first 10 minutes of a 78-minute show: over the 5 limit,
    # even though the show as a whole is far under 33.
    entries = [music(i * 90.0) for i in range(6)]
    with pytest.raises(live365.ValidationError) as e:
        live365.export_csv(entries, 78 * 60)
    assert "first 10 minutes" in str(e.value)


def test_33_markers_allowed_in_78_minutes_but_not_34():
    ok = [music(i * 141.0, title=f"S{i}") for i in range(33)]  # spaced to respect the early windows
    assert live365.validate_rows(live365.to_rows(ok), 78 * 60).ok
    over = ok + [music(33 * 141.0, title="extra")]
    r = live365.validate_rows(live365.to_rows(over), 78 * 60 + 60)
    assert any("first 78 minutes" in e for e in r.errors)


def test_long_show_warns_instead_of_guessing():
    r = live365.validate_rows([{"offset": "00:00:00.000", "media_type": "talk", "title": "T", "artist": ""}], 3 * 3600)
    assert r.ok and any("78 minutes" in w for w in r.warnings)


def test_fit_marker_limits_drops_short_talk_never_music():
    entries = [music(0), talk(100, 20), music(130), talk(250, 90), music(350), talk(460, 15), music(480)]
    fitted, notes = live365.fit_marker_limits(entries)
    assert len([e for e in fitted if e.offset_s < 600]) <= 5
    assert [e.offset_s for e in fitted if e.media_type == "music"] == [0, 130, 350, 480]
    assert len(notes) == 2 and "00:07:40.000" in notes[0]  # shortest talk (15s) removed first
    live365.export_csv(fitted, 3600)


def test_fit_leaves_music_overflow_for_review():
    entries = [music(i * 60.0, title=f"S{i}") for i in range(7)]
    fitted, notes = live365.fit_marker_limits(entries)
    assert fitted == entries and notes == []
    assert not live365.validate_rows(live365.to_rows(fitted), 3600).ok
