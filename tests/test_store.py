"""Leases, resume, fencing, retries, dead-lettering, usage metering."""
import pytest

from cuelab.store import JobStore, StaleLease


def test_show_is_split_into_windows(db, clock):
    s = JobStore(db, clock=clock)
    sid = s.enqueue_show("kdlt", "show", 125, window_s=12, step_s=30)
    rows = s.show_results(sid)
    assert [r["start_s"] for r in rows] == [0, 30, 60, 90, 120]
    assert rows[-1]["end_s"] == 125  # last window clipped to the audio length


def test_expired_lease_is_reclaimed_and_finished_work_is_kept(db, clock):
    s = JobStore(db, clock=clock)
    sid = s.enqueue_show("kdlt", "show", 90, step_s=30)  # 3 windows
    a = s.claim_next("w1", lease_s=10)
    s.complete(a, {"title": "Song", "artist": "A"})
    crashed = s.claim_next("w1", lease_s=10)  # w1 dies holding this lease
    clock.advance(11)
    # Resume: the crashed window is handed to w2; the finished one is not redone.
    b = s.claim_next("w2", lease_s=10)
    assert b.segment_id == crashed.segment_id and b.attempt == 2
    c = s.claim_next("w2", lease_s=10)
    assert c.segment_id not in (a.segment_id, crashed.segment_id)
    assert s.claim_next("w3") is None


def test_stale_worker_cannot_overwrite_newer_result(db, clock):
    s = JobStore(db, clock=clock)
    s.enqueue_show("kdlt", "show", 30, step_s=30)
    old = s.claim_next("slow", lease_s=5)
    clock.advance(6)
    new = s.claim_next("fast", lease_s=5)
    s.complete(new, {"title": "Right", "artist": "A"})
    with pytest.raises(StaleLease):
        s.complete(old, {"title": "Wrong", "artist": "B"})
    rows = s.query("SELECT worker_id, payload FROM results")
    assert len(rows) == 1 and rows[0]["worker_id"] == "fast"


def test_heartbeat_keeps_lease_alive(db, clock):
    s = JobStore(db, clock=clock)
    s.enqueue_show("kdlt", "show", 30, step_s=30)
    c = s.claim_next("w1", lease_s=10)
    clock.advance(8)
    s.heartbeat(c, lease_s=10)
    clock.advance(8)
    assert s.claim_next("w2") is None  # still owned by w1
    s.complete(c, None)


def test_transient_failure_backs_off_then_retries(db, clock):
    s = JobStore(db, clock=clock, max_attempts=4)
    s.enqueue_show("kdlt", "show", 30, step_s=30)
    c = s.claim_next("w1")
    assert s.fail(c, "429", base_backoff_s=2) == "retry"
    assert s.claim_next("w1") is None  # backing off
    clock.advance(2)
    c2 = s.claim_next("w1")
    assert c2.attempt == 2
    assert s.fail(c2, "429", base_backoff_s=2) == "retry"
    clock.advance(3)
    assert s.claim_next("w1") is None  # second backoff is 4s
    clock.advance(1)
    assert s.claim_next("w1").attempt == 3


def test_gives_up_after_max_attempts_and_flags_show_for_review(db, clock):
    finished = []
    s = JobStore(db, clock=clock, max_attempts=2, on_show_finished=lambda *a: finished.append(a))
    sid = s.enqueue_show("kdlt", "show", 60, step_s=30, upload_path="/tmp/x/audio.mp3")
    ok = s.claim_next("w1")
    s.complete(ok, None)
    c = s.claim_next("w1")
    s.fail(c, "boom", base_backoff_s=0)
    c = s.claim_next("w1")
    assert s.fail(c, "boom", base_backoff_s=0) == "dead"
    assert s.show(sid)["status"] == "needs_review"
    assert finished == [(sid, "needs_review", "/tmp/x/audio.mp3")]


def test_crash_on_last_attempt_is_dead_lettered(db, clock):
    s = JobStore(db, clock=clock, max_attempts=1)
    sid = s.enqueue_show("kdlt", "show", 30, step_s=30)
    s.claim_next("w1", lease_s=5)
    clock.advance(6)
    assert s.claim_next("w2") is None
    assert s.show(sid)["status"] == "needs_review"


def test_usage_is_metered_once_per_show(db, clock):
    s = JobStore(db, clock=clock)
    sid = s.enqueue_show("kdlt", "show", 1800, step_s=600)
    while (c := s.claim_next("w")) is not None:
        s.complete(c, None)
    assert s.show(sid)["status"] == "done"
    assert s.usage_hours("kdlt") == pytest.approx(0.5)
    assert s.usage_hours("other") == 0


def test_progress_for_live_work_page(db, clock):
    s = JobStore(db, clock=clock)
    sid = s.enqueue_show("kdlt", "show", 120, step_s=30)
    s.complete(s.claim_next("w"), None)
    p = s.progress(sid)
    assert p["total"] == 4 and p["finished"] == 1 and p["percent"] == 25.0
