"""One large upload must not block other stations."""
from cuelab.store import JobStore


def drain_order(store):
    order = []
    while (c := store.claim_next("w")) is not None:
        order.append(c.station_id)
        store.complete(c, None)
    return order


def test_small_show_is_not_stuck_behind_four_hour_upload(db, clock):
    s = JobStore(db, clock=clock)
    s.enqueue_show("big", "4h show", 4 * 3600, step_s=30)  # 480 windows, queued first
    small = s.enqueue_show("small", "10 min show", 600, step_s=30)  # 20 windows
    order = drain_order(s)
    # Round robin: the small station's 20 windows are done within the first 40 claims,
    # not after the big station's 480.
    last_small = max(i for i, st in enumerate(order) if st == "small")
    assert last_small < 40
    assert s.show(small)["status"] == "done"


def test_three_stations_take_turns(db, clock):
    s = JobStore(db, clock=clock)
    for st in ("a", "b", "c"):
        s.enqueue_show(st, "show", 300, step_s=30)  # 10 windows each
    order = drain_order(s)
    for i in range(0, 30, 3):
        assert sorted(order[i : i + 3]) == ["a", "b", "c"]


def test_late_arrival_gets_served_immediately(db, clock):
    s = JobStore(db, clock=clock)
    s.enqueue_show("big", "show", 3600, step_s=30)
    for _ in range(50):
        s.complete(s.claim_next("w"), None)
    s.enqueue_show("late", "show", 120, step_s=30)
    nxt = [s.claim_next("w") for _ in range(2)]
    assert "late" in {c.station_id for c in nxt}


def test_within_a_station_shows_run_in_upload_order(db, clock):
    s = JobStore(db, clock=clock)
    first = s.enqueue_show("kdlt", "monday", 90, step_s=30)
    s.enqueue_show("kdlt", "tuesday", 90, step_s=30)
    claims = [s.claim_next("w") for _ in range(3)]
    assert all(c.show_id == first for c in claims)
