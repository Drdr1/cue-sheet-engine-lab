"""The shared limiter holds across threads and processes."""
import multiprocessing as mp
import threading
import time

from cuelab.ratelimit import SharedRateLimiter


def test_bucket_refills_at_rate(db, clock):
    lim = SharedRateLimiter(db, "acr", rate_per_s=2, capacity=2, clock=clock)
    assert lim.try_acquire() == 0 and lim.try_acquire() == 0
    assert lim.try_acquire() > 0  # empty
    clock.advance(0.5)
    assert lim.try_acquire() == 0  # one token back after 0.5s at 2/s


def test_threads_never_exceed_shared_budget(db):
    rate, cap, dur = 20.0, 5.0, 1.5
    SharedRateLimiter(db, "acr", rate, cap)
    stamps, lock = [], threading.Lock()
    stop = time.time() + dur

    def hammer():
        lim = SharedRateLimiter(db, "acr", rate, cap)
        while time.time() < stop:
            lim.acquire()
            with lock:
                stamps.append(time.time())

    ts = [threading.Thread(target=hammer) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    elapsed = max(stamps) - min(stamps)
    assert len(stamps) <= cap + rate * elapsed + 2


def _proc(db, stop, q):
    lim = SharedRateLimiter(db, "shazam", 10.0, 2.0)
    n = 0
    while time.time() < stop:
        lim.acquire()
        n += 1
    q.put(n)


def test_processes_share_one_budget(db):
    SharedRateLimiter(db, "shazam", 10.0, 2.0)
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    stop = time.time() + 3.0
    ps = [ctx.Process(target=_proc, args=(db, stop, q)) for _ in range(4)]
    for p in ps:
        p.start()
    for p in ps:
        p.join()
    total = sum(q.get() for _ in ps)
    # 4 processes together stay at ~10/s, not 40/s.
    assert total <= 2 + 10 * 3.0 + 3
