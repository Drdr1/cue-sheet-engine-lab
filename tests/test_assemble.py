from cuelab.assemble import assemble


def w(i, match=None, status="done", step=30):
    return {"idx": i, "start_s": i * step, "end_s": i * step + 12, "status": status, "match": match}


A = {"title": "Aurora", "artist": "Kenai Blue", "album": "North", "label": "Yukon Sound", "isrc": "USX1"}
B = {"title": "Embers", "artist": "Delta Rail", "album": "Cabin", "label": "Coldwater", "isrc": "USX2"}


def test_merges_consecutive_hits_and_marks_talk():
    out = assemble([w(0), w(1, A), w(2, A), w(3, A), w(4), w(5), w(6, B), w(7, B)], host="DJ")
    assert [(e.offset_s, e.media_type, e.title) for e in out] == [
        (0, "talk", "Talk"),
        (30, "music", "Aurora"),
        (120, "talk", "Talk"),
        (180, "music", "Embers"),
    ]
    assert out[1].album == "North" and out[1].label == "Yukon Sound" and out[1].isrc == "USX1"
    assert out[0].artist == "DJ"


def test_single_hit_is_treated_as_false_positive():
    out = assemble([w(0, A), w(1, A), w(2, B), w(3), w(4)])
    assert [e.media_type for e in out] == ["music", "talk"]


def test_dead_window_inside_a_song_is_bridged():
    out = assemble([w(0, A), w(1, A), w(2, status="dead"), w(3, A), w(4, A)])
    assert len(out) == 1 and out[0].hits == 4 and out[0].notes


def test_dead_window_between_different_content_is_flagged():
    out = assemble([w(0, A), w(1, A), w(2, status="dead"), w(3, B), w(4, B)])
    assert [e.needs_review for e in out] == [False, True, False]
    assert out[1].title == "Unidentified (review)"
